"""Recipient directory for voicebot agent tools.

One lookup used by BOTH the voicebot (``list_contacts``) and the API's
internal agent-send routes (recipient resolution), so org-scoping rules
live in exactly one place.

Sources: org members (website-mirrored ``users`` joined through
``members``) and manual org contacts (the ``contacts`` table). Members
now carry an optional ``phone_number`` (written via the Hail API only),
so ``has_phone`` reflects the real column instead of a hardcoded False.

Cross-org isolation rule (load-bearing): every query starts from the
call's ``organization_id`` — a user/contact is reachable only via
membership in (or ownership by) that org. Raw addresses leave this
module only through ``resolve_member_emails``, which only the API
service calls; the voicebot sees names and channel presence, never
addresses.

Agent sends reach members only, so ``list_directory`` (what the agent
reads in ``list_contacts``) lists members only. Manual contacts are used
to name the other party on a call (``find_by_phone``), not as recipients.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from hailhq.core.models import Contact, OrganizationMember, User
from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("hailhq.core.directory")


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    has_email: bool
    has_phone: bool
    source: str  # "member" or "contact"


async def list_directory(
    session: AsyncSession, organization_id: uuid.UUID
) -> list[DirectoryEntry]:
    """The org's members, name-sorted, addresses omitted.

    Members only: agent sends cannot reach manual contacts, so listing them
    would offer the agent names it cannot message.
    """
    stmt = (
        select(User.name, User.phone_number)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == organization_id)
    )
    entries = [
        DirectoryEntry(
            name=name, has_email=True, has_phone=phone is not None, source="member"
        )
        for name, phone in (await session.execute(stmt)).all()
    ]
    entries.sort(key=lambda e: e.name)
    return entries


async def resolve_member_emails(
    session: AsyncSession, organization_id: uuid.UUID, name: str
) -> list[str]:
    """Emails of members matching ``name`` case-insensitively.

    Returns every match — the caller owns the 0-match and >1-match
    policies (the internal route refuses ambiguous sends).
    """
    return await _members_named(session, organization_id, name, User.email)


async def resolve_member_phones(
    session: AsyncSession, organization_id: uuid.UUID, name: str
) -> list[str | None]:
    """Phone numbers of members matching ``name``, for agent sends. Same
    policy as :func:`resolve_member_emails`. A member with no phone stays in
    the list as None, so a shared name stays ambiguous."""
    return await _members_named(session, organization_id, name, User.phone_number)


async def _members_named(
    session: AsyncSession, organization_id: uuid.UUID, name: str, column: Any
) -> list[Any]:
    """``column`` of every member named ``name``, case-insensitively."""
    stmt = (
        select(column)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(
            OrganizationMember.organization_id == organization_id,
            func.lower(User.name) == name.strip().lower(),
        )
    )
    return list((await session.execute(stmt)).scalars().all())


async def is_verified_member(
    session: AsyncSession, organization_id: uuid.UUID, address: str
) -> bool:
    """True when ``address`` is the verified login email of an org member.

    False when the website-owned tables do not exist (a pure self-host): the
    savepoint keeps the caller's transaction usable, and no member means no
    trust. A member whose login is unverified does not count: anyone can
    register an address they do not own.
    """
    stmt = (
        select(User.id)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(
            OrganizationMember.organization_id == organization_id,
            func.lower(User.email) == address.strip().lower(),
            User.email_verified.is_(True),
        )
        .limit(1)
    )
    try:
        async with session.begin_nested():
            return (await session.execute(stmt)).first() is not None
    except ProgrammingError as exc:
        logger.warning("member lookup failed (schema missing or drifted): %s", exc)
        return False


async def find_by_phone(
    session: AsyncSession, organization_id: uuid.UUID, phone_e164: str
) -> DirectoryEntry | None:
    """The member or contact with this number, members first. Names the
    person on a call so the voicebot knows who "me" is. A number shared by
    two members names nobody: "me" would be a guess. Contacts are unique
    per org and number."""
    member_stmt = (
        select(User.name)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(
            OrganizationMember.organization_id == organization_id,
            User.phone_number == phone_e164,
        )
        .limit(2)
    )
    names = (await session.execute(member_stmt)).scalars().all()
    if len(names) > 1:
        return None
    if names and names[0]:
        return DirectoryEntry(
            name=names[0], has_email=True, has_phone=True, source="member"
        )
    contact_stmt = select(Contact.name, Contact.email).where(
        Contact.organization_id == organization_id,
        Contact.phone_e164 == phone_e164,
    )
    row = (await session.execute(contact_stmt)).one_or_none()
    if row is None or not row[0]:
        return None
    name, email = row
    return DirectoryEntry(
        name=name, has_email=email is not None, has_phone=True, source="contact"
    )


__all__ = [
    "DirectoryEntry",
    "find_by_phone",
    "is_verified_member",
    "list_directory",
    "resolve_member_emails",
    "resolve_member_phones",
]
