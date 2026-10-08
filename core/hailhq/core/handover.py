"""Human handover: which people (manual contacts or org members) an agent
may hand a live call to.

Spec: docs/superpowers/specs/2026-10-08-human-handover-design.md. Numbers
stay server-side: ``handover_targets`` (what reaches the LLM via dispatch
metadata) carries names and notes only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

import phonenumbers
from hailhq.core.compliance_gate import check_handover_allowed
from hailhq.core.contact_ids import contact_wire_id, parse_contact_id
from hailhq.core.models import (
    Agent,
    AgentHandoverContact,
    Call,
    CallEvent,
    Contact,
    OrganizationMember,
    User,
)
from hailhq.core.telephony_catalog import sells_in
from sqlalchemy import Exists, delete, exists, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("hailhq.core.handover")

MAX_HANDOVER_CONTACTS = 10

# How long a call may run after a handover contact answers, counted from the
# answer, when the agent sets no ``handover_max_duration_seconds``.
HANDOVER_DEFAULT_MAX_SECONDS = 30 * 60

# Once a handover contact answers, max_duration_seconds no longer applies and
# the call runs until someone hangs up. The sweep then waits this long after
# COALESCE(started_at, requested_at) before closing the row. It only closes
# the DB row of a crashed worker; the voicebot does not enforce it.
HANDOVER_BACKSTOP_SECONDS = 12 * 60 * 60

# The one definition of "a handover contact answered": a call_events row with
# kind HANDOVER_EVENT_KIND and payload outcome HANDOVER_ANSWERED. The SQL and
# ORM forms below and the voicebot's result post all read these two values.
HANDOVER_EVENT_KIND = "handover"
HANDOVER_ANSWERED = "answered"

# SQL: true when call ``c`` has a handover the contact answered. Shared with
# :func:`hailhq.core.pool.sweep_pool_reservations` and
# :func:`hailhq.core.reconcile.sweep_stale_calls`.
ANSWERED_HANDOVER_SQL = f"""EXISTS (
                SELECT 1 FROM call_events e
                 WHERE e.call_id = c.id
                   AND e.kind = '{HANDOVER_EVENT_KIND}'
                   AND e.payload->>'outcome' = '{HANDOVER_ANSWERED}'
              )"""


def answered_handover_exists() -> Exists:
    """ORM form of :data:`ANSWERED_HANDOVER_SQL` for a query over ``Call``."""
    return exists().where(
        CallEvent.call_id == Call.id,
        CallEvent.kind == HANDOVER_EVENT_KIND,
        CallEvent.payload["outcome"].astext == HANDOVER_ANSWERED,
    )


async def has_answered_handover(db: AsyncSession, call_id: UUID) -> bool:
    return (
        await db.execute(
            select(Call.id).where(Call.id == call_id, answered_handover_exists())
        )
    ).first() is not None


@dataclass(frozen=True)
class HandoverItem:
    # Wire id, normalized: a manual contact's uuid or ``member:<user uuid>``
    # (:mod:`hailhq.core.contact_ids`).
    contact_id: str
    note: str


@dataclass(frozen=True)
class HandoverPerson:
    """A resolved handover target: a manual contact or an org member."""

    contact_id: str  # wire id
    name: str
    phone_e164: str | None


class HandoverInvalid(ValueError):
    def __init__(self, message: str, index: int | None = None) -> None:
        super().__init__(message)
        self.index = index


def country_of(e164: str) -> str | None:
    try:
        number = phonenumbers.parse(e164)
    except phonenumbers.NumberParseException:
        return None
    region = phonenumbers.region_code_for_number(number)
    return region if region and region != "001" else None


async def _member_people(
    db: AsyncSession, org_id: UUID, user_ids: list[UUID]
) -> dict[UUID, HandoverPerson]:
    """Members of ``org_id`` among ``user_ids``. Empty when the website-owned
    ``users``/``members`` tables are missing (pure self-host); a savepoint
    keeps the caller's transaction usable."""
    if not user_ids:
        return {}
    try:
        async with db.begin_nested():
            rows = (
                await db.execute(
                    select(User.id, User.name, User.phone_number)
                    .join(OrganizationMember, OrganizationMember.user_id == User.id)
                    .where(
                        OrganizationMember.organization_id == org_id,
                        User.id.in_(user_ids),
                    )
                )
            ).all()
    except ProgrammingError as exc:
        _log.warning("handover member lookup failed (no member tables?): %s", exc)
        return {}
    return {
        uid: HandoverPerson(contact_wire_id("member", uid), name, phone)
        for uid, name, phone in rows
    }


async def resolve_people(
    db: AsyncSession, org_id: UUID, wire_ids: list[str]
) -> dict[str, HandoverPerson]:
    """Wire id -> person, for ids that are contacts or members of ``org_id``.
    Unknown, malformed or foreign ids are left out."""
    contact_ids: list[UUID] = []
    user_ids: list[UUID] = []
    for wire in wire_ids:
        try:
            kind, value = parse_contact_id(wire)
        except ValueError:
            continue
        (user_ids if kind == "member" else contact_ids).append(value)
    out: dict[str, HandoverPerson] = {}
    if contact_ids:
        for c in (
            await db.execute(
                select(Contact).where(
                    Contact.organization_id == org_id, Contact.id.in_(contact_ids)
                )
            )
        ).scalars():
            out[str(c.id)] = HandoverPerson(str(c.id), c.name, c.phone_e164)
    for person in (await _member_people(db, org_id, user_ids)).values():
        out[person.contact_id] = person
    return out


async def validate_handover(
    db: AsyncSession,
    org_id: UUID,
    items: list[HandoverItem],
    unchanged: frozenset[str] = frozenset(),
) -> None:
    """Check the whole list's shape, and each person's number and gate.
    Items are contacts or members (wire ids). Those in ``unchanged`` (already
    linked to the agent) skip the number and gate checks, so editing
    something else never fails on an old link. The runtime /handover route
    re-checks before every dial."""
    if len(items) > MAX_HANDOVER_CONTACTS:
        raise HandoverInvalid(f"at most {MAX_HANDOVER_CONTACTS} handover contacts")
    ids = [i.contact_id for i in items]
    if len(set(ids)) != len(ids):
        raise HandoverInvalid("a contact is listed twice")
    people = await resolve_people(db, org_id, ids)
    for index, item in enumerate(items):
        person = people.get(item.contact_id)
        if person is None:
            raise HandoverInvalid("contact not found", index)
        if item.contact_id in unchanged:
            continue
        if not person.phone_e164:
            raise HandoverInvalid(f"{person.name} has no phone number", index)
        country = country_of(person.phone_e164)
        if country is None or not sells_in(country):
            raise HandoverInvalid(
                f"{person.name}'s number is in a country Hail does not call", index
            )
        gate = await check_handover_allowed(db, org_id, person.phone_e164)
        if not gate.allowed:
            raise HandoverInvalid(f"{person.name}'s number cannot be called", index)


async def replace_handover(
    db: AsyncSession, agent_id: UUID, items: list[HandoverItem]
) -> None:
    await db.execute(
        delete(AgentHandoverContact).where(AgentHandoverContact.agent_id == agent_id)
    )
    for position, item in enumerate(items):
        kind, value = parse_contact_id(item.contact_id)
        db.add(
            AgentHandoverContact(
                agent_id=agent_id,
                contact_id=value if kind == "contact" else None,
                user_id=value if kind == "member" else None,
                note=item.note,
                position=position,
            )
        )
    await db.flush()


async def _member_links(
    db: AsyncSession, agent_ids: list[UUID]
) -> list[tuple[AgentHandoverContact, str, str | None]]:
    """Member links whose user is still a member of the agent's org. Empty
    when the member tables are missing (pure self-host)."""
    try:
        async with db.begin_nested():
            return [
                (link, name, phone)
                for link, name, phone in (
                    await db.execute(
                        select(AgentHandoverContact, User.name, User.phone_number)
                        .join(Agent, Agent.id == AgentHandoverContact.agent_id)
                        .join(User, User.id == AgentHandoverContact.user_id)
                        .where(
                            AgentHandoverContact.agent_id.in_(agent_ids),
                            exists().where(
                                OrganizationMember.user_id == User.id,
                                OrganizationMember.organization_id
                                == Agent.organization_id,
                            ),
                        )
                    )
                ).all()
            ]
    except ProgrammingError as exc:
        _log.warning("handover member lookup failed (no member tables?): %s", exc)
        return []


async def load_handover(
    db: AsyncSession, agent_ids: list[UUID]
) -> dict[UUID, list[dict]]:
    """Each agent's handover people in order, contacts and members mixed.
    ``contact_id`` is the wire id. Members no longer in the org are left
    out."""
    if not agent_ids:
        return {}
    rows: list[tuple[AgentHandoverContact, str, str, str | None]] = [
        (link, str(contact.id), contact.name, contact.phone_e164)
        for link, contact in (
            await db.execute(
                select(AgentHandoverContact, Contact)
                .join(Agent, Agent.id == AgentHandoverContact.agent_id)
                .join(Contact, Contact.id == AgentHandoverContact.contact_id)
                .where(
                    AgentHandoverContact.agent_id.in_(agent_ids),
                    Contact.organization_id == Agent.organization_id,
                )
            )
        ).all()
    ]
    rows += [
        (link, contact_wire_id("member", link.user_id), name, phone)
        for link, name, phone in await _member_links(db, agent_ids)
    ]
    rows.sort(key=lambda r: (str(r[0].agent_id), r[0].position))
    out: dict[UUID, list[dict]] = {}
    for link, wire, name, phone in rows:
        out.setdefault(link.agent_id, []).append(
            {"contact_id": wire, "name": name, "phone_e164": phone, "note": link.note}
        )
    return out


async def handover_targets(db: AsyncSession, agent_id: UUID | None) -> list[dict]:
    """Dispatch-metadata shape. No numbers. Labels unique per call, across
    contacts and members."""
    if agent_id is None:
        return []
    rows = (await load_handover(db, [agent_id])).get(agent_id, [])
    seen: dict[str, int] = {}
    targets = []
    for row in rows:
        if not row["phone_e164"]:
            continue
        name = row["name"]
        seen[name] = seen.get(name, 0) + 1
        label = name if seen[name] == 1 else f"{name} ({seen[name]})"
        targets.append(
            {"contact_id": row["contact_id"], "label": label, "note": row["note"]}
        )
    return targets
