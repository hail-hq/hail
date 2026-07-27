"""Org contacts: computed union of members (users.phone_number) and manual rows.

Single source for the API routes AND the voicebot lookup tool — keep this the
only place that knows what "a contact" is.
"""

from __future__ import annotations

import re
from uuid import UUID

from hailhq.core.models import Contact, OrganizationMember, User
from hailhq.core.schemas import ContactEntry
from sqlalchemy import Text, cast, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

MEMBER_ID_PREFIX = "member:"

# Backslash-escape ILIKE metacharacters (\, %, _) in a user-supplied search
# fragment so they match literally instead of acting as SQL wildcards —
# unescaped, q='%' matches every row in the org and q='j_hn' over-matches
# any single character in place of '_'.
_ILIKE_ESCAPE = "\\"
_ILIKE_METACHARS = re.compile(r"([\\%_])")


def _escaped_like_pattern(fragment: str) -> str:
    escaped = _ILIKE_METACHARS.sub(r"\\\1", fragment)
    return f"%{escaped}%"


def contact_to_entry(row: Contact) -> ContactEntry:
    """Manual-contact ORM row -> wire shape. Used by the CRUD routes
    (create/patch return the row they just wrote) — the union-query paths
    below build ``ContactEntry`` straight from the unioned Row instead,
    since they never hydrate a standalone ``Contact`` object."""
    return ContactEntry(
        id=str(row.id),
        kind="manual",
        name=row.name,
        phone_e164=row.phone_e164,
        email=row.email,
        role=None,
    )


def contacts_union_stmt(org_id: UUID, q: str | None = None):
    """One ``union_all`` statement over aligned columns for members ∪ manual
    contacts — the shared query definition for both ``search_contacts``
    (voicebot/MCP top-N lookups) and the cursor-paginated ``GET /contacts``
    route. No ORDER BY / LIMIT here; callers apply their own.

    Columns:
    * ``id_text`` — wire id (``member:<uuid>`` or the manual row's uuid, as
      a string) — what ``ContactEntry.id`` gets built from.
    * ``id_uuid`` — the row's real uuid (``User.id`` / ``Contact.id``),
      kept separate from ``id_text`` because ``fetch_cursor_page``'s cursor
      codec (``core.schemas.decode_cursor``) does a raw ``UUID(...)`` parse
      on the id half of the cursor — that fails on a ``"member:"``-prefixed
      string. Only the paginated route touches this column.
    * ``kind``, ``name``, ``phone_e164``, ``email``, ``role`` — the
      ``ContactEntry`` fields.
    * ``kind_rank`` — 0 for members, 1 for manual; lets a listing (no ``q``)
      order members first without a second sort key.
    * ``created_at`` — real timestamp (``OrganizationMember.created_at`` /
      ``Contact.created_at``), included solely so the paginated route can
      cursor-walk via ``fetch_cursor_page``, which mints each cursor with
      ``ts.isoformat()`` — a requirement ``kind_rank``/``name`` can't
      satisfy. ``search_contacts`` selects the same union and ignores it.
      (This one extra column, and ``id_uuid`` above, are the one deviation
      from a stricter reading of the spec's column list — needed to let
      search's fairness ordering and the route's cursor pagination share
      one statement; see max-fix-report.md.)
    """
    pattern = _escaped_like_pattern(q.strip()) if q and q.strip() else None

    member_stmt = (
        select(
            (literal(MEMBER_ID_PREFIX, type_=Text) + cast(User.id, Text)).label(
                "id_text"
            ),
            User.id.label("id_uuid"),
            literal("member").label("kind"),
            User.name.label("name"),
            User.phone_number.label("phone_e164"),
            User.email.label("email"),
            OrganizationMember.role.label("role"),
            literal(0).label("kind_rank"),
            OrganizationMember.created_at.label("created_at"),
        )
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == org_id)
    )
    if pattern is not None:
        member_stmt = member_stmt.where(
            or_(
                User.name.ilike(pattern, escape=_ILIKE_ESCAPE),
                User.email.ilike(pattern, escape=_ILIKE_ESCAPE),
                User.phone_number.ilike(pattern, escape=_ILIKE_ESCAPE),
            )
        )

    manual_stmt = select(
        cast(Contact.id, Text).label("id_text"),
        Contact.id.label("id_uuid"),
        literal("manual").label("kind"),
        Contact.name.label("name"),
        Contact.phone_e164.label("phone_e164"),
        Contact.email.label("email"),
        literal(None, type_=Text).label("role"),
        literal(1).label("kind_rank"),
        Contact.created_at.label("created_at"),
    ).where(Contact.organization_id == org_id)
    if pattern is not None:
        manual_stmt = manual_stmt.where(
            or_(
                Contact.name.ilike(pattern, escape=_ILIKE_ESCAPE),
                Contact.email.ilike(pattern, escape=_ILIKE_ESCAPE),
                Contact.phone_e164.ilike(pattern, escape=_ILIKE_ESCAPE),
            )
        )

    return union_all(member_stmt, manual_stmt)


async def search_contacts(
    session: AsyncSession,
    org_id: UUID,
    q: str | None = None,
    limit: int = 100,
) -> list[ContactEntry]:
    """Top-``limit`` contacts for a query or listing, from one union query.

    Ordering:
    * ``q`` provided: ``(name asc, id asc)`` — members and manual rows
      compete fairly for the limit, so a matching manual contact isn't
      starved off the page by many matching members (empirically: 12
      members + 1 matching vendor, ``limit=10`` -> vendor missing under a
      members-first order).
    * ``q`` absent (listing): ``(kind_rank asc, name asc, id asc)`` — org
      members first, manual rows after, per spec.

    One ``LIMIT`` on the outer statement — no per-branch limiting or
    post-hoc slicing.
    """
    u = contacts_union_stmt(org_id, q).subquery()
    stmt = select(u)
    if q and q.strip():
        stmt = stmt.order_by(u.c.name.asc(), u.c.id_uuid.asc())
    else:
        stmt = stmt.order_by(u.c.kind_rank.asc(), u.c.name.asc(), u.c.id_uuid.asc())
    stmt = stmt.limit(limit)

    rows = (await session.execute(stmt)).all()
    return [
        ContactEntry(
            id=row.id_text,
            kind=row.kind,
            name=row.name,
            phone_e164=row.phone_e164,
            email=row.email,
            role=row.role,
        )
        for row in rows
    ]


__all__ = [
    "MEMBER_ID_PREFIX",
    "contact_to_entry",
    "contacts_union_stmt",
    "search_contacts",
]
