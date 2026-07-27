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

Follow-up (not done here): ``resolve_member_emails`` still resolves
member emails only — extending agent-SEND resolution to manual contacts
or phone numbers is a separate workstream. The voicebot's directory
*browse* (``list_contacts``) already surfaces contacts via
``list_directory``; agent-initiated sends to a contact are future work.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

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
    """All directory entries for one org, name-sorted, addresses omitted."""
    member_stmt = (
        select(User.name, User.phone_number)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == organization_id)
    )
    member_rows = (await session.execute(member_stmt)).all()
    entries = [
        DirectoryEntry(
            name=name, has_email=True, has_phone=phone is not None, source="member"
        )
        for name, phone in member_rows
    ]

    try:
        contact_stmt = select(Contact.name, Contact.email, Contact.phone_e164).where(
            Contact.organization_id == organization_id
        )
        contact_rows = (await session.execute(contact_stmt)).all()
    except ProgrammingError as exc:
        # Self-host posture: a deployment that hasn't run the contacts
        # migration (0036) has no `contacts` table at all. Its absence
        # means "no manual contacts to show", not a server error — degrade
        # to members-only instead of raising UndefinedTable on every call.
        logger.warning(
            "directory contacts lookup failed (schema missing or drifted): %s",
            exc,
        )
        # Load-bearing: the failed statement leaves the session's
        # transaction aborted; roll back so the session is safe to reuse
        # (mirrors build_agent_tools' availability-check posture).
        await session.rollback()
        contact_rows = []

    entries.extend(
        DirectoryEntry(
            name=name,
            has_email=email is not None,
            has_phone=phone is not None,
            source="contact",
        )
        for name, email, phone in contact_rows
    )

    entries.sort(key=lambda e: e.name)
    return entries


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
