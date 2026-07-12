"""Org contacts: computed union of members (users.phone_number) and manual rows.

Single source for the API routes AND the voicebot lookup tool — keep this the
only place that knows what "a contact" is.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import Contact, OrganizationMember, User
from hailhq.core.schemas import ContactEntry


async def search_contacts(
    session: AsyncSession,
    org_id: UUID,
    q: str | None = None,
    limit: int = 100,
) -> list[ContactEntry]:
    like = f"%{q.strip()}%" if q and q.strip() else None

    member_stmt = (
        select(
            User.id, User.name, User.email, User.phone_number, OrganizationMember.role
        )
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == org_id)
        .order_by(User.name.asc())
        .limit(limit)
    )
    if like is not None:
        member_stmt = member_stmt.where(
            or_(
                User.name.ilike(like),
                User.email.ilike(like),
                User.phone_number.ilike(like),
            )
        )

    manual_stmt = (
        select(Contact)
        .where(Contact.organization_id == org_id)
        .order_by(Contact.name.asc())
        .limit(limit)
    )
    if like is not None:
        manual_stmt = manual_stmt.where(
            or_(
                Contact.name.ilike(like),
                Contact.email.ilike(like),
                Contact.phone_e164.ilike(like),
            )
        )

    entries: list[ContactEntry] = [
        ContactEntry(
            id=f"member:{uid}",
            kind="member",
            name=name,
            phone_e164=phone,
            email=email,
            role=role,
        )
        for uid, name, email, phone, role in (await session.execute(member_stmt)).all()
    ]
    entries.extend(
        ContactEntry(
            id=str(c.id),
            kind="manual",
            name=c.name,
            phone_e164=c.phone_e164,
            email=c.email,
            role=None,
        )
        for c in (await session.execute(manual_stmt)).scalars()
    )
    return entries[:limit]
