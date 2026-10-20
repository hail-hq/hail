"""/internal/orgs/{org}/providers: hail-website console → API.

Per-org BYO provider keys (cloud console feature; see the design spec in
the hail-website repo). Shared-secret HMAC auth like the rest of
``routes/internal/``. Deliberately NOT in the public OpenAPI spec and has
no CLI surface. Self-host operators configure providers via env vars.
Keys are write-only: stored Fernet-encrypted (HAIL_PROVIDER_SECRET_KEY),
surfaced only as last4 + set-at timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.languages import SUPPORTED_LANGUAGES
from hailhq.core.models import OrgProviderConfig
from hailhq.core.provider_config import (
    LAYERS,
    PARAMS_BY_LAYER,
    last4,
    load_org_provider_configs,
    provider_cipher,
)
from hailhq.core.provider_validation import validate_provider_key
from hailhq.core.secret_cipher import SecretKeyMissing
from hailhq.core.voice_catalog import validate_voice_language
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

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
    verify_voice_language: bool = False


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

    Deactivates the currently-active row first, then activates the target,
    in that order, so the partial-unique index (``UNIQUE(org, layer) WHERE
    is_active``) never sees two active rows for the same layer at once. The
    caller commits; both statements land in the caller's transaction.

    The deactivate is scoped to ``is_active`` rows only (never the whole
    sibling set): ``updated_at`` is ``onupdate=now()``, so touching an
    already-inactive row would reset its timestamp and destroy the recency
    ordering that DELETE-promotion depends on. Only the row genuinely
    leaving active (and the target row gaining it) should move.
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


@dataclass(frozen=True)
class _LayerState:
    """The provider, params and fallback flag a layer will have after a change."""

    provider: str
    params: dict
    fallback_enabled: bool


_KEEP = object()


async def _require_servable_language(
    db: AsyncSession,
    organization_id: UUID,
    *,
    tts: object = _KEEP,
    stt: object = _KEEP,
) -> None:
    """Reject a change that leaves the saved default language unservable.

    The workspace language lives on the active TTS row. ``tts`` and ``stt``
    are the layer states after the change: a row, a ``_LayerState``, or
    None when no row will be active. A layer left as ``_KEEP`` uses the
    current active row. Same rule as the POST /calls gate: a provider that
    cannot serve the language needs fallback consent.
    """
    if tts is _KEEP or stt is _KEEP:
        active = await load_org_provider_configs(db, organization_id)
        tts = active.get("tts") if tts is _KEEP else tts
        stt = active.get("stt") if stt is _KEEP else stt
    language = (tts.params or {}).get("language") if tts is not None else None
    if not language:
        return
    caps = SUPPORTED_LANGUAGES[language]
    if tts.provider not in caps.tts and not tts.fallback_enabled:
        raise HTTPException(
            422,
            "This voice provider does not support the saved language. Enable fallback or choose another provider.",
        )
    if stt is not None and stt.provider not in caps.stt and not stt.fallback_enabled:
        raise HTTPException(
            422,
            "Your speech recognition provider does not support the saved language. Change it in Advanced or enable fallback.",
        )


async def _next_active_row(
    db: AsyncSession, organization_id: UUID, layer: str, provider: str
) -> OrgProviderConfig | None:
    """The sibling DELETE promotes when ``provider``'s active row goes."""
    return (
        await db.execute(
            select(OrgProviderConfig)
            .where(
                OrgProviderConfig.organization_id == organization_id,
                OrgProviderConfig.layer == layer,
                OrgProviderConfig.provider != provider,
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

    if layer in ("tts", "stt"):
        incoming = _LayerState(body.provider, params, body.fallback_enabled)
        await _require_servable_language(db, organization_id, **{layer: incoming})

    row = await _get_row(db, organization_id, layer, body.provider)
    # A provider outside the language's TTS set only passed the gate above
    # through fallback consent: the house chain speaks, so there is nothing
    # to verify on this provider.
    if (
        layer == "tts"
        and body.verify_voice_language
        and params.get("language")
        and body.provider in SUPPORTED_LANGUAGES[params["language"]].tts
    ):
        provider = body.provider
        key = body.api_key
        if key is None and row is not None and row.encrypted_api_key is not None:
            try:
                key = provider_cipher().decrypt(row.encrypted_api_key)
            except (InvalidToken, SecretKeyMissing) as exc:
                raise HTTPException(
                    503,
                    "Your stored voice key could not be read. Update it in Advanced.",
                ) from exc
        if key is None:
            key = (
                settings.cartesia_api_key
                if provider == "cartesia"
                else settings.eleven_api_key
            )
        model = params.get("model") or (
            settings.cartesia_model
            if provider == "cartesia"
            else settings.elevenlabs_model
        )
        voice_id = params.get("voice_id") or (
            settings.cartesia_voice_id
            if provider == "cartesia"
            else settings.elevenlabs_voice_id
        )
        try:
            await validate_voice_language(
                provider, voice_id, model, key, params["language"]
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            # A rejected key (401, 403) can be Hail's house key: not the
            # user's error. Rate limits and provider faults are transient.
            status = exc.response.status_code
            if status >= 500 or status in (401, 403, 429):
                raise HTTPException(
                    503,
                    "Voice support could not be checked. Try again. Your settings were not changed.",
                ) from exc
            raise HTTPException(
                422,
                "This voice could not be found with this key. Choose a suggested voice or update Advanced.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                503,
                "Voice support could not be checked. Try again. Your settings were not changed.",
            ) from exc
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
    # the identity map) see this row, then flip it active in the same
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
    remaining = None
    if was_active:
        remaining = await _next_active_row(db, organization_id, layer, provider)
        if layer in ("tts", "stt"):
            await _require_servable_language(db, organization_id, **{layer: remaining})
    await db.delete(row)

    try:
        if remaining is not None:
            # Flush the pending delete first, so the DELETE hits Postgres
            # before the promote UPDATE. Otherwise activating a sibling while
            # the old active row still exists trips the partial-unique index.
            # A concurrent delete of the same row raises StaleDataError here.
            await db.flush()
            remaining.is_active = True
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
    if layer in ("tts", "stt"):
        await _require_servable_language(db, organization_id, **{layer: row})

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
