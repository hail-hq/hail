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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

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


def _serialize(row: OrgProviderConfig) -> dict:
    return {
        "layer": row.layer,
        "provider": row.provider,
        "key_last4": row.key_last4,
        "key_set_at": row.key_set_at.isoformat() if row.key_set_at else None,
        "params": row.params,
        "fallback_enabled": row.fallback_enabled,
    }


def _check_layer(layer: str) -> None:
    if layer not in LAYERS:
        raise HTTPException(status_code=404, detail=f"unknown layer '{layer}'")


def _validated_params(layer: str, provider: str, params: dict) -> dict:
    try:
        model = PARAMS_BY_LAYER[layer](provider=provider, **params)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return model.model_dump(exclude={"provider"}, exclude_none=True)


async def _get_row(
    db: AsyncSession, organization_id: UUID, layer: str
) -> OrgProviderConfig | None:
    return (
        await db.execute(
            select(OrgProviderConfig).where(
                OrgProviderConfig.organization_id == organization_id,
                OrgProviderConfig.layer == layer,
            )
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
                .order_by(OrgProviderConfig.layer)
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

    row = await _get_row(db, organization_id, layer)
    if row is None:
        row = OrgProviderConfig(
            organization_id=organization_id, layer=layer, provider=body.provider
        )
        db.add(row)
    row.provider = body.provider
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

    await db.commit()
    await db.refresh(row)
    return _serialize(row)


@router.delete("/orgs/{organization_id}/providers/{layer}", status_code=204)
async def delete_provider_config(
    organization_id: UUID,
    layer: str,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    _check_layer(layer)
    await db.execute(
        delete(OrgProviderConfig).where(
            OrgProviderConfig.organization_id == organization_id,
            OrgProviderConfig.layer == layer,
        )
    )
    await db.commit()


@router.post("/orgs/{organization_id}/providers/{layer}/validate")
async def validate_provider_config(
    organization_id: UUID,
    layer: str,
    body: ValidateIn,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    _check_layer(layer)
    row = await _get_row(db, organization_id, layer)

    api_key = body.api_key
    provider = row.provider if row else None
    params = dict(row.params) if row else {}
    if api_key is None:
        if row is None or row.encrypted_api_key is None:
            raise HTTPException(
                status_code=422, detail="no api_key given and none stored"
            )
        try:
            api_key = provider_cipher().decrypt(row.encrypted_api_key)
        except SecretKeyMissing as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    if provider is None:
        raise HTTPException(
            status_code=422, detail="no stored config for this layer; save first"
        )

    ok, message = await validate_provider_key(layer, provider, api_key, params)
    return {"ok": ok, "message": message}
