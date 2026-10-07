"""Org from-number resolution shared by the phone-channel create routes.

One query shape for both /calls and /sms: explicit ``from`` → that number,
iff org-owned + active (+ capability); otherwise the org's oldest active
number. Keeping it in one place stops the two routes drifting on number-
selection policy (provisioning states, capability checks, ordering).
"""

from __future__ import annotations

from uuid import UUID

from hailhq.core.db import org_lock
from hailhq.core.models import Agent, PhoneNumber
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["resolve_org_number"]


async def resolve_org_number(
    db: AsyncSession,
    organization_id: UUID,
    explicit_e164: str | None,
    *,
    capability: str | None = None,
) -> PhoneNumber | None:
    """Resolve the org-owned sending number for an outbound send.

    ``capability`` (e.g. ``"sms"``) additionally requires the number's
    ``capabilities`` array to carry it — a number can legitimately be
    voice-only. Returns ``None`` when nothing matches; the caller owns the
    failure path (calls fall back to the shared pool, SMS 422s).
    """
    stmt = select(PhoneNumber).where(
        PhoneNumber.organization_id == organization_id,
        PhoneNumber.provisioning_state == "active",
    )
    if capability is not None:
        stmt = stmt.where(PhoneNumber.capabilities.any(capability))
    if explicit_e164 is not None:
        stmt = stmt.where(PhoneNumber.e164 == explicit_e164)
    else:
        stmt = stmt.order_by(PhoneNumber.created_at.asc()).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def resolve_sms_number(
    db: AsyncSession,
    organization_id: UUID,
    agent_id: UUID | None,
    dialed: PhoneNumber | None,
) -> PhoneNumber | None:
    """The org number an agent texts from. Order: the number the person dialed
    (if it can text), a text number already routed to this agent, a text
    number with no agent (it is routed to this agent), else None. A number
    routed to another agent is never taken."""
    if (
        dialed is not None
        and dialed.provisioning_state == "active"
        and "sms" in dialed.capabilities
    ):
        return dialed
    if agent_id is None:
        return await resolve_org_number(db, organization_id, None, capability="sms")
    base = (
        select(PhoneNumber)
        .where(
            PhoneNumber.organization_id == organization_id,
            PhoneNumber.provisioning_state == "active",
            PhoneNumber.capabilities.any("sms"),
        )
        .order_by(PhoneNumber.created_at)
        .limit(1)
    )
    routed = (
        await db.execute(base.where(PhoneNumber.sms_agent_id == agent_id))
    ).scalar_one_or_none()
    if routed is not None:
        return routed
    # Binding changes routing, so it takes the gates PATCH /numbers/{id} does:
    # the agent is in this org and answers texts. Same org lock as that route,
    # and the candidate row is locked, so two calls cannot bind one number.
    agent = (
        await db.execute(
            select(Agent).where(
                Agent.id == agent_id, Agent.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if agent is None or not agent.sms_enabled:
        return None
    await org_lock(db, organization_id)
    free = (
        await db.execute(
            base.where(PhoneNumber.sms_agent_id.is_(None)).with_for_update(
                skip_locked=True
            )
        )
    ).scalar_one_or_none()
    if free is not None:
        free.sms_agent_id = agent_id
        await db.flush()
        # Tells the caller to audit the bind once it commits.
        db.info["auto_bound_sms_number_id"] = free.id
    return free
