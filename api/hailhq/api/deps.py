"""Request-scoped FastAPI dependencies.

``get_current_principal`` resolves the ``Authorization: Bearer <key>``
header to a :class:`Principal` and stamps ``api_keys.last_used_at``,
committing eagerly so the timestamp persists even if the caller's
handler later rolls back its own work.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.auth import hash_key
from hailhq.api.db import get_session
from hailhq.core.models import ApiKey


class Principal(BaseModel):
    """The authenticated caller, exposed to route handlers."""

    api_key_id: uuid.UUID
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


async def get_current_principal(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_session),
) -> Principal:
    token = _parse_bearer(authorization)
    _, hex_digest = hash_key(token)

    result = await db.execute(select(ApiKey).where(ApiKey.key_hash == hex_digest))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise _unauthorized("invalid API key")

    now = datetime.now(timezone.utc)
    if api_key.expires_at is not None and api_key.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key has expired",
        )

    # Commit eagerly so last_used_at survives a later rollback by the handler.
    await db.execute(
        update(ApiKey).where(ApiKey.id == api_key.id).values(last_used_at=now)
    )
    await db.commit()

    return Principal(
        api_key_id=api_key.id,
        organization_id=api_key.organization_id,
        scopes=list(api_key.scopes or []),
    )
