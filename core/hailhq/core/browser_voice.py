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
        charge = session_charge_cents(
            duration, meta["browser_rate_cents"], call.max_duration_seconds
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
    call.metadata_ = meta
    call.status = "failed" if failed else "completed"
    call.end_reason = "agent_error" if failed else "normal_hangup"
    call.ended_at = now
