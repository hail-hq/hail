"""Browser voice accounting. Calls own the session; the ledger owns money.

A free minute is reserved in full before provisioning. Only finalization can
return its unused part. A crashed session therefore never reopens its budget.
Prices arrive from the authenticated website, never a browser or public API.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
from uuid import UUID

from hailhq.core.models import AccountCredit, Call, UsageEvent
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

MESSAGE_PREVIEW_LIMIT = 3
TRIAL_MS = 60_000
TRIAL_CONCURRENCY = 3
BROWSER_CONCURRENCY = 10
SETUP_SECONDS = 45


def trial_remaining_ms(calls) -> int:
    used = sum(
        max(
            0,
            int(
                c.metadata_.get(
                    "browser_used_ms", (c.max_duration_seconds or 60) * 1000
                )
            ),
        )
        for c in calls
        if c.metadata_.get("browser_trial")
    )
    return max(0, TRIAL_MS - used)


def browser_budget(calls, *, current=None, now=None) -> dict:
    """Public counters, with live elapsed media rather than reserved time."""
    now = now or datetime.now(timezone.utc)
    relevant = (
        [current]
        if current is not None and not current.metadata_.get("browser_trial")
        else [c for c in calls if c.metadata_.get("browser_trial")]
    )
    used = 0
    counts = {"sms": 0, "email": 0}
    for call in relevant:
        if call.status in TERMINAL_CALL_STATUSES:
            used += max(
                0,
                int(
                    call.metadata_.get(
                        "browser_used_ms", (call.max_duration_seconds or 60) * 1000
                    )
                ),
            )
        elif call.answered_at is not None:
            used += min(
                (call.max_duration_seconds or 60) * 1000,
                max(0, int((now - call.answered_at).total_seconds() * 1000)),
            )
        for action in call.metadata_.get("browser_actions", []):
            channel = action.get("channel")
            if channel in counts:
                counts[channel] += 1
    limit = (
        current.max_duration_seconds * 1000
        if current is not None and not current.metadata_.get("browser_trial")
        else TRIAL_MS
    )
    return {
        "voice": {"used": min(limit, used), "limit": limit},
        **{
            channel: {
                "used": min(MESSAGE_PREVIEW_LIMIT, count),
                "limit": MESSAGE_PREVIEW_LIMIT,
            }
            for channel, count in counts.items()
        },
    }


async def read_browser_budget(db, organization_id, current=None):
    if current is not None and not current.metadata_.get("browser_trial"):
        return browser_budget([], current=current)
    # Polling needs trial accounting fields, never paid history or transcripts.
    calls = (
        await db.execute(
            select(
                Call.metadata_, Call.status, Call.answered_at, Call.max_duration_seconds
            ).where(
                Call.organization_id == organization_id,
                Call.transport == "browser",
                Call.metadata_["browser_trial"].as_boolean().is_(True),
            )
        )
    ).all()
    return browser_budget(calls, current=current)


async def reserve_browser_preview(db, call_id, action):
    """Serialize independent modality limits across the workspace's trial."""
    from hailhq.core.db import org_lock

    call = await db.get(Call, call_id)
    if call is None:
        return False
    await org_lock(db, call.organization_id)
    call = (
        await db.execute(
            select(Call)
            .where(Call.id == call_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if call.status in TERMINAL_CALL_STATUSES:
        return False
    budget = await read_browser_budget(db, call.organization_id, call)
    if (
        action["channel"] not in ("sms", "email")
        or budget[action["channel"]]["used"] >= MESSAGE_PREVIEW_LIMIT
    ):
        return False
    meta = dict(call.metadata_)
    meta["browser_actions"] = [*meta.get("browser_actions", []), action]
    call.metadata_ = meta
    await db.flush()
    return True


def session_charge_cents(duration_ms: int, rate: str, seconds: int) -> Decimal:
    duration = max(0, min(duration_ms, seconds * 1000))
    return (Decimal(duration) * Decimal(rate) / 60_000).quantize(
        Decimal("0.1"), rounding=ROUND_CEILING
    )


async def finish_browser_call(
    db: AsyncSession,
    call_id: UUID,
    *,
    failed: bool = False,
    ended_at: datetime | None = None,
    started_at: datetime | None = None,
    waive_paid_charge: bool = False,
) -> None:
    """Lock and finalize once, including refunds. Caller commits the transaction."""
    call = (
        await db.execute(
            select(Call)
            .where(Call.id == call_id, Call.transport == "browser")
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if call is None or call.status in TERMINAL_CALL_STATUSES:
        return
    if started_at is not None and call.answered_at is None:
        call.started_at = call.answered_at = started_at
    now = ended_at or datetime.now(timezone.utc)
    duration = (
        min(
            call.max_duration_seconds * 1000,
            max(0, int((now - call.answered_at).total_seconds() * 1000)),
        )
        if call.answered_at
        else 0
    )
    meta = dict(call.metadata_)
    meta["browser_used_ms"] = duration
    if not meta.get("browser_trial"):
        reserved = Decimal(meta["browser_reserved_cents"])
        charge = (
            Decimal(0)
            if waive_paid_charge
            else session_charge_cents(
                duration, meta["browser_rate_cents"], call.max_duration_seconds
            )
        )
        refund = reserved - charge
        if refund > 0:
            db.add(
                AccountCredit(
                    organization_id=call.organization_id,
                    kind="credit",
                    channel="voice",
                    amount_cents=refund,
                    source="browser_voice_refund",
                    qty=1,
                    ref=f"browser-refund:{call.id}",
                )
            )
        if duration:
            db.add(
                UsageEvent(
                    organization_id=call.organization_id,
                    channel="voice",
                    units=duration,
                    ref=f"browser:{call.id}",
                    priced_at=now,
                )
            )
    if waive_paid_charge:
        meta["browser_charge_waived"] = True
    call.metadata_ = meta
    call.status = "failed" if failed else "completed"
    call.end_reason = "agent_error" if failed else "normal_hangup"
    call.ended_at = now
