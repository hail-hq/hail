"""/internal/orgs/{org}/providers — hail-website console → API.

Per-org BYO provider keys (cloud console feature; see the design spec in
the hail-website repo). Shared-secret HMAC auth like the rest of
``routes/internal/``. Deliberately NOT in the public OpenAPI spec and has
no CLI surface — self-host operators configure providers via env vars.
Keys are write-only: stored Fernet-encrypted (HAIL_PROVIDER_SECRET_KEY),
surfaced only as last4 + set-at timestamp.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.core.db import get_session
from hailhq.core.models import OrgProviderConfig
from hailhq.core.provider_config import (
    LAYERS,
    PARAMS_BY_LAYER,
    last4,
    provider_cipher,
)
from hailhq.core.provider_validation import validate_provider_key
from hailhq.core.secret_cipher import SecretKeyMissing

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)


class ProviderConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    api_key: str | None = None
    params: dict = {}
    fallback_enabled: bool = False


class ValidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = None
    provider: str | None = None
    params: dict = {}


class ActivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str


def _serialize(row: OrgProviderConfig) -> dict:
    return {
        "layer": row.layer,
        "provider": row.provider,
        "key_last4": row.key_last4,
        "key_set_at": row.key_set_at.isoformat() if row.key_set_at else None,
        "params": row.params,
        "fallback_enabled": row.fallback_enabled,
        "is_active": row.is_active,
    }


def _check_layer(layer: str) -> None:
    if layer not in LAYERS:
        raise HTTPException(status_code=404, detail=f"unknown layer '{layer}'")


def _validated_params(layer: str, provider: str, params: dict) -> dict:
    if "provider" in params:
        raise HTTPException(
            status_code=422, detail="params must not include 'provider'"
        )
    try:
        model = PARAMS_BY_LAYER[layer](provider=provider, **params)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return model.model_dump(exclude={"provider"}, exclude_none=True)


async def _get_active_row(
    db: AsyncSession, organization_id: UUID, layer: str
) -> OrgProviderConfig | None:
    return (
        await db.execute(
            select(OrgProviderConfig).where(
                OrgProviderConfig.organization_id == organization_id,
                OrgProviderConfig.layer == layer,
                OrgProviderConfig.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def _get_row(
    db: AsyncSession, organization_id: UUID, layer: str, provider: str
) -> OrgProviderConfig | None:
    return (
        await db.execute(
            select(OrgProviderConfig).where(
                OrgProviderConfig.organization_id == organization_id,
                OrgProviderConfig.layer == layer,
                OrgProviderConfig.provider == provider,
            )
        )
    ).scalar_one_or_none()


async def _set_active(
    db: AsyncSession, organization_id: UUID, layer: str, provider: str
) -> None:
    """Make ``provider``'s row the sole active row for (org, layer).

    Deactivates the currently-active row first, then activates the target —
    in that order, so the partial-unique index (``UNIQUE(org, layer) WHERE
    is_active``) never sees two active rows for the same layer at once. The
    caller commits; both statements land in the caller's transaction.

    The deactivate is scoped to ``is_active`` rows only (never the whole
    sibling set): ``updated_at`` is ``onupdate=now()``, so touching an
    already-inactive row would reset its timestamp and destroy the recency
    ordering that DELETE-promotion depends on. Only the row genuinely
    leaving active — and the target row gaining it — should move.
    """
    await db.execute(
        update(OrgProviderConfig)
        .where(
            OrgProviderConfig.organization_id == organization_id,
            OrgProviderConfig.layer == layer,
            OrgProviderConfig.provider != provider,
            OrgProviderConfig.is_active.is_(True),
        )
        .values(is_active=False)
    )
    await db.execute(
        update(OrgProviderConfig)
        .where(
            OrgProviderConfig.organization_id == organization_id,
            OrgProviderConfig.layer == layer,
            OrgProviderConfig.provider == provider,
        )
        .values(is_active=True)
    )


@router.get("/orgs/{organization_id}/providers")
async def list_provider_configs(
    organization_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    rows = (
        (
            await db.execute(
                select(OrgProviderConfig)
                .where(OrgProviderConfig.organization_id == organization_id)
                .order_by(OrgProviderConfig.layer, OrgProviderConfig.provider)
            )
        )
        .scalars()
        .all()
    )
    return {"providers": [_serialize(r) for r in rows]}


@router.put("/orgs/{organization_id}/providers/{layer}")
async def upsert_provider_config(
    organization_id: UUID,
    layer: str,
    body: ProviderConfigIn,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _check_layer(layer)
    params = _validated_params(layer, body.provider, body.params)

    row = await _get_row(db, organization_id, layer, body.provider)
    if row is None:
        row = OrgProviderConfig(
            organization_id=organization_id, layer=layer, provider=body.provider
        )
        db.add(row)
    row.params = params
    row.fallback_enabled = body.fallback_enabled

    if body.api_key is not None:
        try:
            cipher = provider_cipher()
        except SecretKeyMissing as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        row.encrypted_api_key = cipher.encrypt(body.api_key)
        row.key_last4 = last4(body.api_key)
        row.key_set_at = datetime.now(timezone.utc)

    # Flush the upsert first so _set_active's UPDATEs (raw SQL, bypassing
    # the identity map) see this row — then flip it active in the same
    # transaction as the upsert. Postgres checks the partial-unique index
    # per-statement (not deferred), so a concurrent activation can raise
    # IntegrityError out of _set_active itself, not just out of commit().
    try:
        await db.flush()
        await _set_active(db, organization_id, layer, body.provider)
        await db.commit()
    except (IntegrityError, StaleDataError) as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"provider config for layer {layer!r} changed concurrently; retry",
        ) from exc
    await db.refresh(row)
    return _serialize(row)


@router.delete("/orgs/{organization_id}/providers/{layer}/{provider}", status_code=204)
async def delete_provider_config(
    organization_id: UUID,
    layer: str,
    provider: str,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    _check_layer(layer)
    row = await _get_row(db, organization_id, layer, provider)
    if row is None:
        return
    was_active = row.is_active
    await db.delete(row)

    if was_active:
        # The select below autoflushes the pending delete, so the DELETE hits
        # Postgres before the promote UPDATE — required, else activating a
        # sibling while the old active row still exists trips the partial-unique
        # index. Do NOT reorder the promote before db.delete/this query.
        remaining = (
            await db.execute(
                select(OrgProviderConfig)
                .where(
                    OrgProviderConfig.organization_id == organization_id,
                    OrgProviderConfig.layer == layer,
                )
                # provider is the deterministic tiebreak: two siblings written
                # in one transaction share updated_at (Postgres now() is
                # tx-start), so ORDER BY updated_at alone would promote an
                # arbitrary row.
                .order_by(
                    OrgProviderConfig.updated_at.desc(),
                    OrgProviderConfig.provider.asc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if remaining is not None:
            remaining.is_active = True

    try:
        await db.commit()
    except (IntegrityError, StaleDataError) as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"provider config for layer {layer!r} changed concurrently; retry",
        ) from exc


@router.post("/orgs/{organization_id}/providers/{layer}/activate")
async def activate_provider_config(
    organization_id: UUID,
    layer: str,
    body: ActivateIn,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _check_layer(layer)
    row = await _get_row(db, organization_id, layer, body.provider)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"no saved config for provider {body.provider!r} in layer {layer!r}",
        )

    try:
        await _set_active(db, organization_id, layer, body.provider)
        await db.commit()
    except (IntegrityError, StaleDataError) as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"provider config for layer {layer!r} changed concurrently; retry",
        ) from exc
    # Re-fetch rather than refresh(): if the target row was deleted between the
    # existence check and commit, _set_active's UPDATE matched 0 rows (no error)
    # and refresh() would raise ObjectDeletedError (500). A None result here is
    # the concurrent-delete case → 404.
    row = await _get_row(db, organization_id, layer, body.provider)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"no saved config for provider {body.provider!r} in layer {layer!r}",
        )
    return _serialize(row)


@router.post("/orgs/{organization_id}/providers/{layer}/validate")
async def validate_provider_config(
    organization_id: UUID,
    layer: str,
    body: ValidateIn,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _check_layer(layer)
    # Honor an explicit provider (test THAT provider's saved key/config, even
    # if it isn't the active one); fall back to the active row otherwise.
    row = (
        await _get_row(db, organization_id, layer, body.provider)
        if body.provider
        else await _get_active_row(db, organization_id, layer)
    )

    if body.api_key is not None:
        # In-flight: validate the typed key against the drawer's provider+params.
        provider = body.provider or (row.provider if row else None)
        if provider is None:
            raise HTTPException(status_code=422, detail="pick a provider to test")
        params = _validated_params(layer, provider, body.params)
        api_key = body.api_key
    else:
        # Stored: validate the saved key against its saved provider.
        if row is None or row.encrypted_api_key is None:
            raise HTTPException(
                status_code=422, detail="no api_key given and none stored"
            )
        try:
            api_key = provider_cipher().decrypt(row.encrypted_api_key)
        except (SecretKeyMissing, InvalidToken) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        provider = row.provider
        params = dict(row.params)

    status, message = await validate_provider_key(layer, provider, api_key, params)
    return {"status": status, "message": message}
