"""Request-scoped FastAPI dependencies.

Auth picks a path based on what's in the env (see ``docs/operations.md``):
``HAIL_API_KEY`` triggers the shared-key path; otherwise we look the bearer up
in the auth backend's ``apikey`` table and resolve the user's org through
Better Auth's ``member`` table.
"""

from __future__ import annotations

import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import cast, func, select, text, update
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.auth import hash_key
from hailhq.core.config import settings
from hailhq.core.db import get_session, session_scope
from hailhq.core.models import ApiKey, OrganizationMember

# Throttle ``lastRequest`` writes so chatty agent traffic doesn't generate one
# no-op UPDATE per request.
_LAST_USED_THROTTLE = timedelta(seconds=60)

SHARED_PRINCIPAL_KEY_ID = "shared"
# Nil UUID — sentinel organization for every shared-key (HAIL_API_KEY) request.
# No row in ``organizations``; the balance gate is short-circuited for it.
SELF_HOSTED_ORG_ID = uuid.UUID(int=0)


@dataclass
class _Caches:
    # True iff the auth backend's apikey table exists. Self-host operators
    # don't run the auth backend's migrations, so this stays False there.
    apikey_table_present: bool | None = None

    def reset(self) -> None:
        self.apikey_table_present = None


_caches = _Caches()


def reset_caches() -> None:
    """Public hook for tests to clear process-wide caches between runs."""
    _caches.reset()


class Principal(BaseModel):
    """The authenticated caller, exposed to route handlers."""

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
    """Turn the auth backend's ``permissions`` JSON into a flat scopes list.

    Missing/unparseable permissions imply full-wildcard scope (the auth
    backend stores NULL when the key wasn't created with explicit scopes).
    A *parsed but empty* permissions object means "no scopes" — return [].
    """
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
    return out


async def _apikey_table_exists(db: AsyncSession) -> bool:
    """Probe ``information_schema`` once and cache the result."""
    if _caches.apikey_table_present is None:
        result = await db.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'api_keys' LIMIT 1"
            )
        )
        _caches.apikey_table_present = result.scalar_one_or_none() is not None
    return _caches.apikey_table_present


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
    # api_keys.reference_id is TEXT (opaque to Better Auth); members.user_id is UUID.
    stmt = (
        select(ApiKey, OrganizationMember.organization_id)
        .outerjoin(
            OrganizationMember,
            OrganizationMember.user_id == cast(ApiKey.reference_id, PG_UUID),
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
        # The website's signup hook creates a `members` row alongside every new
        # user; if it's missing here, signup either failed or hasn't run for
        # this user yet. Send them to the dashboard rather than fabricating one.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "user not provisioned with an organization; "
                "sign in to the dashboard to complete setup"
            ),
        )

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
        return Principal(
            api_key_id=SHARED_PRINCIPAL_KEY_ID,
            organization_id=SELF_HOSTED_ORG_ID,
            scopes=["*"],
        )

    if await _apikey_table_exists(db):
        return await _principal_from_apikey_table(token, db)

    raise _unauthorized("invalid API key")
