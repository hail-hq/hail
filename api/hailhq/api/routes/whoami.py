"""GET /whoami — who the caller is.

Agents need this to address mail as a person: an MCP client that wants
the human's address in ``Reply-To`` has no other way to learn it, since
the bearer token carries an organization, not a mailbox.

The identity comes from ``Principal`` (deps.py), so the answer follows
the auth path: an api-key call resolves the key owner, a JWT call
resolves its ``sub``, and a shared-key (``HAIL_API_KEY``) call resolves
nobody — ``user_id``/``email``/``name`` come back ``None``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.core.db import get_session
from hailhq.core.models import User
from hailhq.core.schemas import WhoamiResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["whoami"], responses=GENERAL_RATE_LIMITED_RESPONSES)


@router.get(
    "/whoami",
    response_model=WhoamiResponse,
)
async def get_whoami(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WhoamiResponse:
    """Identify the caller: auth kind, organization, and (if resolvable) user.

    Useful for an agent that needs the human's address to put in
    Reply-To, since the bearer token itself only carries an organization.
    user_id/email/name come back null for a shared-key
    (HAIL_API_KEY) call, which has no individual user behind it.
    """
    if principal.user_id is None:
        return WhoamiResponse(
            auth_kind=principal.auth_kind,
            organization_id=principal.organization_id,
        )

    row = (
        await db.execute(
            select(User.email, User.name).where(User.id == principal.user_id)
        )
    ).one_or_none()

    return WhoamiResponse(
        auth_kind=principal.auth_kind,
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        # The website owns ``users``; a JWT ``sub`` with no row there is a
        # live session for a deleted user. Report the id we have rather
        # than 404 — the caller asked who it is, not for a user record.
        email=row.email if row is not None else None,
        name=row.name if row is not None else None,
    )


__all__ = ["router"]
