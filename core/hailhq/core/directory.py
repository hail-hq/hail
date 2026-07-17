"""Recipient directory for voicebot agent tools.

One lookup used by BOTH the voicebot (``list_contacts``) and the API's
internal agent-send routes (recipient resolution), so org-scoping rules
live in exactly one place.

Sources: org members today (website-mirrored ``users`` joined through
``members``); the contacts table (separate workstream) joins as a second
source when it lands.

Cross-org isolation rule (load-bearing): every query starts from the
call's ``organization_id`` — a user is reachable only via membership in
that org. Raw addresses leave this module only through
``resolve_member_emails``, which only the API service calls; the voicebot
sees names and channel presence, never addresses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import OrganizationMember, User


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    has_email: bool
    has_phone: bool
    source: str  # "member" (future: "contact")


async def list_directory(
    session: AsyncSession, organization_id: uuid.UUID
) -> list[DirectoryEntry]:
    """All directory entries for one org, name-sorted, addresses omitted."""
    stmt = (
        select(User.name)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == organization_id)
        .order_by(User.name)
    )
    names = (await session.execute(stmt)).scalars().all()
    # Members come from the better-auth users table, which has no phone
    # column — members are email-only recipients until the contacts source.
    return [
        DirectoryEntry(name=n, has_email=True, has_phone=False, source="member")
        for n in names
    ]


async def resolve_member_emails(
    session: AsyncSession, organization_id: uuid.UUID, name: str
) -> list[str]:
    """Emails of members matching ``name`` case-insensitively.

    Returns every match — the caller owns the 0-match and >1-match
    policies (the internal route refuses ambiguous sends).
    """
    stmt = (
        select(User.email)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(
            OrganizationMember.organization_id == organization_id,
            func.lower(User.name) == name.strip().lower(),
        )
    )
    return list((await session.execute(stmt)).scalars().all())


__all__ = ["DirectoryEntry", "list_directory", "resolve_member_emails"]
