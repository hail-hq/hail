"""Request-scoped FastAPI dependencies.

Auth picks a path based on what's in the env (see ``docs/operations.md``):
``HAIL_API_KEY`` triggers the shared-key path; otherwise we look the bearer up
in the auth backend's ``apikey`` table.
"""

from __future__ import annotations

import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.auth import hash_key
from hailhq.core.config import settings
from hailhq.core.db import get_session, session_scope
from hailhq.core.models import ApiKey, Organization

# Throttle ``lastRequest`` writes so chatty agent traffic doesn't generate one
# no-op UPDATE per request.
_LAST_USED_THROTTLE = timedelta(seconds=60)

_SHARED_PRINCIPAL_KEY_ID = "shared"
_SELF_HOSTED_ORG_SLUG = "self-hosted"
_SELF_HOSTED_ORG_NAME = "Self-hosted"

# True if the auth backend's apikey table exists. Self-host operators don't
# run the auth backend's migrations, so this stays False there.
_apikey_table_present: bool | None = None
# Cached for the lifetime of the process — slug is unique and immutable.
_self_hosted_org_id: uuid.UUID | None = None


class Principal(BaseModel):
    """The authenticated caller, exposed to route handlers.

    ``api_key_id`` is the auth backend's ``apikey.id`` for managed-cloud
    requests, or the literal ``"shared"`` sentinel for shared-key requests.
    """

    api_key_id: str
    organization_id: uuid.UUID
    scopes: list[str]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _parse_bearer(authorization: str | None) -> str:
    if not authorization:
        raise _unauthorized("missing Authorization header; expected 'Bearer <api-key>'")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise _unauthorized("invalid Authorization header; expected 'Bearer <api-key>'")
    return parts[1].strip()


def _scopes_from_permissions(perms_json: str | None) -> list[str]:
    """Turn the auth backend's ``permissions`` JSON into a flat scopes list."""
    if not perms_json:
        return ["*"]
    try:
        parsed = json.loads(perms_json)
    except json.JSONDecodeError:
        return ["*"]
    if not isinstance(parsed, dict):
        return ["*"]
    out: list[str] = []
    for resource, actions in parsed.items():
        if isinstance(actions, list):
            for a in actions:
                out.append(f"{resource}:{a}")
    return out or ["*"]


async def _apikey_table_exists(db: AsyncSession) -> bool:
    """Probe ``information_schema`` once and cache the result."""
    global _apikey_table_present
    if _apikey_table_present is None:
        result = await db.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'apikey' LIMIT 1"
            )
        )
        _apikey_table_present = result.scalar_one_or_none() is not None
    return _apikey_table_present


async def _get_or_create_self_hosted_org(db: AsyncSession) -> uuid.UUID:
    """Return the implicit org id for shared-key mode, creating it once."""
    global _self_hosted_org_id
    if _self_hosted_org_id is not None:
        return _self_hosted_org_id

    # Race-safe upsert in a fresh session so we don't commit the request txn.
    async with session_scope() as fresh:
        stmt = (
            pg_insert(Organization)
            .values(name=_SELF_HOSTED_ORG_NAME, slug=_SELF_HOSTED_ORG_SLUG)
            .on_conflict_do_nothing(index_elements=["slug"])
            .returning(Organization.id)
        )
        org_id = (await fresh.execute(stmt)).scalar_one_or_none()
        if org_id is None:
            org_id = (
                await fresh.execute(
                    select(Organization.id).where(
                        Organization.slug == _SELF_HOSTED_ORG_SLUG
                    )
                )
            ).scalar_one()
        await fresh.commit()

    _self_hosted_org_id = org_id
    return org_id


async def _ensure_org_for_user(reference_id: str) -> uuid.UUID:
    """Lazy-bridge: get-or-create the Organization tied to an auth user id."""
    async with session_scope() as fresh:
        stmt = (
            pg_insert(Organization)
            .values(
                name="Personal workspace",
                slug=reference_id,
                auth_user_id=reference_id,
            )
            .on_conflict_do_nothing(
                index_elements=["auth_user_id"],
                index_where=Organization.auth_user_id.isnot(None),
            )
            .returning(Organization.id)
        )
        org_id = (await fresh.execute(stmt)).scalar_one_or_none()
        if org_id is None:
            org_id = (
                await fresh.execute(
                    select(Organization.id).where(
                        Organization.auth_user_id == reference_id
                    )
                )
            ).scalar_one()
        await fresh.commit()
    return org_id


async def _stamp_last_used(api_key_id: str, ts: datetime) -> None:
    """Update ``lastRequest`` and bump ``requestCount`` in a fresh session.

    ``requestCount`` is nullable in the apikey schema; treat NULL as 0 so the
    first hit becomes 1 instead of staying NULL forever.
    """
    async with session_scope() as session:
        await session.execute(
            update(ApiKey)
            .where(ApiKey.id == api_key_id)
            .values(
                last_request=ts,
                request_count=func.coalesce(ApiKey.request_count, 0) + 1,
            )
        )
        await session.commit()


def _check_shared_key(token: str) -> bool:
    """Constant-time compare against ``HAIL_API_KEY``. Empty config never matches."""
    configured = settings.hail_api_key
    if not configured:
        return False
    return hmac.compare_digest(configured, token)


async def _principal_from_apikey_table(token: str, db: AsyncSession) -> Principal:
    hashed = hash_key(token)
    stmt = (
        select(ApiKey, Organization.id)
        .outerjoin(
            Organization,
            Organization.auth_user_id == ApiKey.reference_id,
        )
        .where(ApiKey.key == hashed)
    )
    row = (await db.execute(stmt)).first()
    # The auth backend stores ``enabled`` as nullable with an application-level
    # default of true, so NULL means "allowed" — only reject when explicitly False.
    if row is None or row[0].enabled is False:
        raise _unauthorized("invalid API key")

    api_key, organization_id = row

    now = datetime.now(timezone.utc)
    if api_key.expires_at is not None and api_key.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key has expired",
        )

    if organization_id is None:
        organization_id = await _ensure_org_for_user(api_key.reference_id)

    if api_key.last_request is None or now - api_key.last_request > _LAST_USED_THROTTLE:
        await _stamp_last_used(api_key.id, now)

    return Principal(
        api_key_id=api_key.id,
        organization_id=organization_id,
        scopes=_scopes_from_permissions(api_key.permissions),
    )


async def get_current_principal(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_session),
) -> Principal:
    token = _parse_bearer(authorization)

    if _check_shared_key(token):
        org_id = await _get_or_create_self_hosted_org(db)
        return Principal(
            api_key_id=_SHARED_PRINCIPAL_KEY_ID,
            organization_id=org_id,
            scopes=["*"],
        )

    if await _apikey_table_exists(db):
        return await _principal_from_apikey_table(token, db)

    raise _unauthorized("invalid API key")
