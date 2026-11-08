"""Carrier legs of a call and what each carrier charged for them.

The voicebot calls :func:`record_call_legs` when a call ends. The api runs
:func:`fetch_leg_costs` every few minutes: it reads each pending leg's call
record at its carrier and stores the cost. Carriers write those records a
few minutes after the call, so a leg is asked about again every
``RECHECK_EVERY`` until ``GIVE_UP_AFTER``, then marked ``missing``.

Legs are internal: carrier costs never reach a customer API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from hailhq.core.carrier_routing import carrier
from hailhq.core.db import session_scope
from hailhq.core.models import Call, CallLeg
from hailhq.core.providers.voice.base import CarrierNotConfigured, LegRef
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

logger = logging.getLogger(__name__)

# Wait this long after a leg ends before the first look: carriers write the
# record after the call.
SETTLE = timedelta(minutes=2)
RECHECK_EVERY = timedelta(minutes=15)
GIVE_UP_AFTER = timedelta(hours=48)
BATCH = 200


@dataclass(frozen=True)
class LegRecord:
    """One SIP participant of a call, as the voicebot saw it."""

    role: Literal["call", "handover"]
    direction: Literal["inbound", "outbound"]
    sip_call_id: str | None
    carrier_call_id: str | None
    to_e164: str | None
    joined_at: datetime
    answered_at: datetime | None
    left_at: datetime | None


def _priceable(provider: str, leg: LegRecord) -> bool:
    """False when the carrier cannot look this leg up, so it is never asked."""
    if provider == "twilio":
        return bool(leg.carrier_call_id)
    if provider == "didww":
        return leg.direction == "outbound" and bool(leg.sip_call_id)
    return bool(leg.sip_call_id)


async def record_call_legs(call_id: UUID, legs: list[LegRecord]) -> None:
    """Store a call's legs once. Fails open: a lost leg only loses its cost."""
    if not legs:
        return
    try:
        async with session_scope() as session:
            provider = await session.scalar(
                select(Call.provider).where(Call.id == call_id)
            )
            if provider is None:
                return
            stmt = insert(CallLeg).values(
                [
                    {
                        "call_id": call_id,
                        "role": leg.role,
                        "provider": provider,
                        "direction": leg.direction,
                        "sip_call_id": leg.sip_call_id,
                        "carrier_call_id": leg.carrier_call_id,
                        "to_e164": leg.to_e164,
                        "joined_at": leg.joined_at,
                        "answered_at": leg.answered_at,
                        "left_at": leg.left_at,
                        "cost_status": (
                            "pending" if _priceable(provider, leg) else "missing"
                        ),
                    }
                    for leg in legs
                ]
            )
            await session.execute(
                stmt.on_conflict_do_nothing(
                    index_elements=["call_id", "sip_call_id"],
                    index_where=CallLeg.sip_call_id.is_not(None),
                )
            )
            await session.commit()
    except Exception:
        logger.warning("call_id=%s could not record call legs", call_id, exc_info=True)


async def fetch_leg_costs(now: datetime | None = None) -> int:
    """One pass: look up pending legs at their carriers. Returns how many
    legs got a cost. Safe to run on several api instances at once: each
    pass claims its legs (``cost_checked_at``) before asking a carrier, so
    no leg is asked about twice in one ``RECHECK_EVERY``."""
    now = now or datetime.now(timezone.utc)
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(CallLeg)
                    .where(
                        CallLeg.cost_status == "pending",
                        func.coalesce(CallLeg.left_at, CallLeg.joined_at)
                        < now - SETTLE,
                        or_(
                            CallLeg.cost_checked_at.is_(None),
                            CallLeg.cost_checked_at < now - RECHECK_EVERY,
                        ),
                    )
                    .order_by(CallLeg.joined_at)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        by_provider: dict[str, list[LegRef]] = {}
        for row in rows:
            by_provider.setdefault(row.provider, []).append(
                LegRef(
                    row.id,
                    row.direction,  # type: ignore[arg-type]
                    row.sip_call_id,
                    row.carrier_call_id,
                    row.joined_at,
                    row.left_at,
                )
            )
        if rows:
            await session.execute(
                update(CallLeg)
                .where(CallLeg.id.in_([row.id for row in rows]))
                .values(cost_checked_at=now)
            )
            await session.commit()

    priced = 0
    for provider, refs in by_provider.items():
        # A skipped or failed lookup leaves the legs untouched: an outage or a
        # missing key must not turn into lost costs.
        try:
            found = await carrier(provider).leg_costs(refs)
        except CarrierNotConfigured as exc:
            # Our gap, not the carrier's: keep the legs pending, no give-up.
            logger.info("leg costs: %s skipped, %s", provider, exc)
            continue
        except Exception:
            logger.warning("leg costs: %s lookup failed", provider, exc_info=True)
            continue
        async with session_scope() as session:
            for leg_id, cost in found.items():
                result = await session.execute(
                    update(CallLeg)
                    .where(CallLeg.id == leg_id, CallLeg.cost_status == "pending")
                    .values(
                        cost_status="found",
                        cost_usd=cost.usd,
                        billed_seconds=cost.billed_seconds,
                        usd_per_minute=cost.usd_per_minute,
                        cost_checked_at=now,
                    )
                )
                priced += result.rowcount or 0
            rest = [ref.id for ref in refs if ref.id not in found]
            if rest:
                await session.execute(
                    update(CallLeg)
                    .where(
                        CallLeg.id.in_(rest),
                        CallLeg.cost_status == "pending",
                        func.coalesce(CallLeg.left_at, CallLeg.joined_at)
                        < now - GIVE_UP_AFTER,
                    )
                    .values(cost_status="missing")
                )
            await session.commit()
    return priced
