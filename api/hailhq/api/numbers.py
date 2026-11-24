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
from hailhq.core.models import Agent, PhoneNumber, SipConnection
from hailhq.core.sms_registration_gate import (
    approved_registration_ids,
    is_us,
    on_approved_registration,
    registered_sms_number,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["AnswerOnlyNumber", "resolve_org_number", "resolve_sms_number"]


class AnswerOnlyNumber(ValueError):
    """The named ``from`` is a connected number whose phone system only
    answers: Hail has no address to place calls through it."""


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
    failure path (calls fall back to the shared pool, SMS 422s). An explicit
    connected number with no outbound trunk raises :class:`AnswerOnlyNumber`.
    """
    stmt = select(PhoneNumber).where(
        PhoneNumber.organization_id == organization_id,
        PhoneNumber.provisioning_state == "active",
    )
    if capability is not None:
        stmt = stmt.where(PhoneNumber.capabilities.any(capability))
    if explicit_e164 is not None:
        number = (
            await db.execute(stmt.where(PhoneNumber.e164 == explicit_e164))
        ).scalar_one_or_none()
        if number is not None and number.provider == "pbx":
            conn = (
                await db.get(SipConnection, number.sip_connection_id)
                if number.sip_connection_id
                else None
            )
            if conn is None or conn.livekit_outbound_trunk_id is None:
                raise AnswerOnlyNumber(explicit_e164)
        return number
    # The default caller ID must be a number Hail can dial from: skip a
    # connected number whose phone system only answers (no outbound trunk).
    stmt = (
        stmt.outerjoin(SipConnection, SipConnection.id == PhoneNumber.sip_connection_id)
        .where(
            or_(
                PhoneNumber.provider != "pbx",
                SipConnection.livekit_outbound_trunk_id.is_not(None),
            )
        )
        .order_by(PhoneNumber.created_at.asc())
        .limit(1)
    )
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
    to_e164: str | None = None,
) -> PhoneNumber | None:
    """The org number an agent texts from. Order:

    1. the number the person dialed, if it can text and is not routed to
       another agent. Routed to this agent: used. Free: bound to the agent when
       the agent is in the org and ``sms_enabled``, else used unbound.
    2. the oldest text number already routed to this agent.
    3. the oldest text number with no agent: bound to the agent when it is
       ``sms_enabled``, else used unbound.
    4. None.

    For a US recipient (``to_e164``) whose dialed number is not on an
    approved US SMS registration, the same order is tried first among
    registered numbers only: the send gate lets a US text leave only those.
    With none, the order above applies to every number.

    A number routed to another agent is never taken. Every automatic bind sets
    ``db.info["auto_bound_sms_number_id"]``; an unbound use sets nothing.
    """
    if (
        to_e164 is not None
        and is_us(to_e164)
        and not await on_approved_registration(db, organization_id, dialed)
    ):
        registered = await _resolve_sms_number(
            db, organization_id, agent_id, None, registered_only=True
        )
        if registered is not None:
            return registered
    return await _resolve_sms_number(db, organization_id, agent_id, dialed)


async def _resolve_sms_number(
    db: AsyncSession,
    organization_id: UUID,
    agent_id: UUID | None,
    dialed: PhoneNumber | None,
    *,
    registered_only: bool = False,
) -> PhoneNumber | None:
    """One pass of :func:`resolve_sms_number`. ``registered_only`` limits
    every step to numbers on an approved US SMS registration."""
    if agent_id is None:
        if (
            dialed is not None
            and dialed.provisioning_state == "active"
            and "sms" in dialed.capabilities
        ):
            return dialed
        if registered_only:
            return await registered_sms_number(db, organization_id)
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
    if registered_only:
        base = base.where(
            PhoneNumber.sms_registration_id.in_(
                approved_registration_ids(organization_id)
            )
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
