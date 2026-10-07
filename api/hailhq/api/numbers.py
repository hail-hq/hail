"""Org from-number resolution shared by the phone-channel create routes.

One query shape for both /calls and /sms: explicit ``from`` → that number,
iff org-owned + active (+ capability); otherwise the org's oldest active
number. Keeping it in one place stops the two routes drifting on number-
selection policy (provisioning states, capability checks, ordering).
"""

from __future__ import annotations

from typing import Literal
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


BindResult = Literal["bound", "cannot", "locked"]


async def _bind_free(
    db: AsyncSession, organization_id: UUID, agent_id: UUID, stmt
) -> tuple[PhoneNumber | None, BindResult]:
    """Bind the number ``stmt`` selects (one row, sms_agent_id still NULL) to
    the agent. Same gates as PATCH /numbers/{id}: the agent is in this org and
    answers texts. Same org lock, and the row is locked, so two calls cannot
    bind one number. Returns ``(number, "bound")``; ``(None, "cannot")`` when
    the agent may not bind (the caller may use a free number unbound); or
    ``(None, "locked")`` when no unlocked free row was found (another call
    holds it or took it: the caller must not use it)."""
    agent = (
        await db.execute(
            select(Agent).where(
                Agent.id == agent_id, Agent.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if agent is None or not agent.sms_enabled:
        return None, "cannot"
    await org_lock(db, organization_id)
    free = (
        await db.execute(
            stmt.where(PhoneNumber.sms_agent_id.is_(None))
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if free is None:
        return None, "locked"
    free.sms_agent_id = agent_id
    await db.flush()
    # Tells the caller to audit the bind once it commits.
    db.info["auto_bound_sms_number_id"] = free.id
    return free, "bound"


async def resolve_sms_number(
    db: AsyncSession,
    organization_id: UUID,
    agent_id: UUID | None,
    dialed: PhoneNumber | None,
) -> PhoneNumber | None:
    """The org number an agent texts from. Order:

    1. the number the person dialed, if it can text and is not routed to
       another agent. Routed to this agent: used. Free: bound to the agent when
       the agent is in the org and ``sms_enabled``, else used unbound.
    2. the oldest text number already routed to this agent.
    3. the oldest text number with no agent: bound to the agent when it is
       ``sms_enabled``, else used unbound.
    4. None.

    A number routed to another agent is never taken. Every automatic bind sets
    ``db.info["auto_bound_sms_number_id"]``; an unbound use sets nothing.
    """
    if agent_id is None:
        if (
            dialed is not None
            and dialed.provisioning_state == "active"
            and "sms" in dialed.capabilities
        ):
            return dialed
        return await resolve_org_number(db, organization_id, None, capability="sms")
    if (
        dialed is not None
        and dialed.organization_id == organization_id
        and dialed.provisioning_state == "active"
        and "sms" in dialed.capabilities
    ):
        if dialed.sms_agent_id == agent_id:
            return dialed
        if dialed.sms_agent_id is None:
            bound, result = await _bind_free(
                db,
                organization_id,
                agent_id,
                select(PhoneNumber).where(PhoneNumber.id == dialed.id),
            )
            if bound is not None:
                return bound
            if result == "cannot":
                return dialed  # free, agent cannot bind: used unbound
            # "locked": another call is binding it; skip it, never use it.
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
    bound, result = await _bind_free(db, organization_id, agent_id, base)
    if bound is not None:
        return bound
    if result == "locked":
        return None
    # The agent cannot bind: use the oldest free number unbound.
    return (
        await db.execute(base.where(PhoneNumber.sms_agent_id.is_(None)))
    ).scalar_one_or_none()
