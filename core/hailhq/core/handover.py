"""Human handover: which contacts an agent may hand a live call to.

Spec: docs/superpowers/specs/2026-10-08-human-handover-design.md. Numbers
stay server-side: ``handover_targets`` (what reaches the LLM via dispatch
metadata) carries names and notes only.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import phonenumbers
from hailhq.core.compliance_gate import check_call_allowed
from hailhq.core.models import AgentHandoverContact, Contact
from hailhq.core.telephony_catalog import sells_in
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

MAX_HANDOVER_CONTACTS = 10


@dataclass(frozen=True)
class HandoverItem:
    contact_id: UUID
    note: str


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


async def validate_handover(
    db: AsyncSession, org_id: UUID, items: list[HandoverItem]
) -> None:
    if len(items) > MAX_HANDOVER_CONTACTS:
        raise HandoverInvalid(f"at most {MAX_HANDOVER_CONTACTS} handover contacts")
    ids = [i.contact_id for i in items]
    if len(set(ids)) != len(ids):
        raise HandoverInvalid("a contact is listed twice")
    rows = {
        c.id: c
        for c in (
            await db.execute(
                select(Contact).where(
                    Contact.organization_id == org_id, Contact.id.in_(ids)
                )
            )
        ).scalars()
    }
    for index, item in enumerate(items):
        contact = rows.get(item.contact_id)
        if contact is None:
            raise HandoverInvalid("contact not found", index)
        if not contact.phone_e164:
            raise HandoverInvalid(f"{contact.name} has no phone number", index)
        country = country_of(contact.phone_e164)
        if country is None or not sells_in(country):
            raise HandoverInvalid(
                f"{contact.name}'s number is in a country Hail does not call", index
            )
        gate = await check_call_allowed(db, org_id, contact.phone_e164)
        if not gate.allowed:
            raise HandoverInvalid(f"{contact.name}'s number cannot be called", index)


async def replace_handover(
    db: AsyncSession, agent_id: UUID, items: list[HandoverItem]
) -> None:
    await db.execute(
        delete(AgentHandoverContact).where(AgentHandoverContact.agent_id == agent_id)
    )
    for position, item in enumerate(items):
        db.add(
            AgentHandoverContact(
                agent_id=agent_id,
                contact_id=item.contact_id,
                note=item.note,
                position=position,
            )
        )
    await db.flush()


async def load_handover(
    db: AsyncSession, agent_ids: list[UUID]
) -> dict[UUID, list[dict]]:
    if not agent_ids:
        return {}
    result = await db.execute(
        select(AgentHandoverContact, Contact)
        .join(Contact, Contact.id == AgentHandoverContact.contact_id)
        .where(AgentHandoverContact.agent_id.in_(agent_ids))
        .order_by(AgentHandoverContact.agent_id, AgentHandoverContact.position)
    )
    out: dict[UUID, list[dict]] = {}
    for link, contact in result.all():
        out.setdefault(link.agent_id, []).append(
            {
                "contact_id": contact.id,
                "name": contact.name,
                "phone_e164": contact.phone_e164,
                "note": link.note,
            }
        )
    return out


async def handover_targets(db: AsyncSession, agent_id: UUID | None) -> list[dict]:
    """Dispatch-metadata shape. No numbers. Labels unique per call."""
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
            {"contact_id": str(row["contact_id"]), "label": label, "note": row["note"]}
        )
    return targets
