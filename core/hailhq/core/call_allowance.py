"""How long a call may run on the organization's balance.

Hail charges a call after it ends, so the balance does not drop while the
call runs. Without a cap, a call that starts with 1 cent left runs to its
time limit (up to 60 minutes) and the carrier bills Hail for all of it.

So each billed phone call sets money aside (a ``call_balance_holds`` row)
when it starts: its price per minute, from hail-website's private rater,
times the minutes it may run. The next call sees the balance less every
live hold. A handover dial adds the second line's price and grows the hold.

Billing rounds carrier minutes up, so the cap is whole minutes less
``SAFETY_SECONDS`` for the goodbye the voicebot says when the cap fires.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hailhq.core.billing import RECORDING_USAGE_REF_SUFFIX, get_ledger_cents
from hailhq.core.models import CallBalanceHold
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from sqlalchemy import bindparam, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

# Left for the goodbye after the cap fires, so the billed minutes still fit.
SAFETY_SECONDS = 10
# The handover rings before it is answered (the voicebot gives up after
# HANDOVER_RING_SECONDS = 30), after the agent speaks and the dial is set
# up; the call runs meanwhile. Two minutes cover both.
HANDOVER_RING_MINUTES = 2
# A handover shorter than this is refused: the pickup check and the goodbye
# would leave the two people almost no time.
HANDOVER_MIN_SECONDS = 60
# After pickup, both lines run before the handover limit starts: the
# voicebot listens for speech (HANDOVER_SILENCE_SECONDS = 15) and waits for
# the line to settle (HANDOVER_SETTLE_SECONDS = 10), plus a margin. A
# voicebot test keeps those two within this.
HANDOVER_PICKUP_SECONDS = 30
# How long a hold outlives the call's planned end (a stuck call), and how
# long a billed call's hold waits for its usage row after the call ends.
HOLD_GRACE_SECONDS = 300
# A billed call whose usage row the rater never prices stops holding this
# long after hold_until: the age at which _PRUNE_SQL deletes the hold.
UNPRICED_HOLD_DAYS = 1


def round_up_tenth(cents: Decimal) -> Decimal:
    """Round up to the tenth of a cent the hold column keeps."""
    return Decimal(math.ceil(cents * 10)) / 10


def minutes_affordable(
    available_cents: Decimal, cents_per_minute: Decimal, cents_per_call: Decimal
) -> int:
    """Whole minutes ``available_cents`` pays for, after the per-call fee,
    such that the hold ``hold()`` stores (fee + minutes, rounded up to a
    tenth) still fits. ``cents_per_minute`` is above 0 (quote_call_price
    refuses anything else)."""
    available = Decimal(math.floor(available_cents * 10)) / 10
    if available <= cents_per_call:
        return 0
    minutes = math.floor((available - cents_per_call) / cents_per_minute)
    while minutes > 0 and (
        round_up_tenth(cents_per_call + cents_per_minute * minutes) > available
    ):
        minutes -= 1
    return minutes


def cap_seconds(minutes: int, extra: int = 0) -> int:
    """The time limit that ``minutes`` paid minutes allow, less the goodbye
    and ``extra`` seconds billed outside the limit (0 when none left)."""
    return max(0, minutes * 60 - SAFETY_SECONDS - extra) if minutes > 0 else 0


def paid_minutes(seconds: int, extra: int = 0) -> int:
    """The billed minutes of a call limited to ``seconds``: the goodbye
    after the limit and ``extra`` seconds outside it can add one."""
    return math.ceil((seconds + SAFETY_SECONDS + extra) / 60)


def hold_until(now: datetime, seconds: int) -> datetime:
    """When a hold for a call that may run ``seconds`` more stops counting."""
    return now + timedelta(seconds=seconds + SAFETY_SECONDS + HOLD_GRACE_SECONDS)


# A hold counts while:
# - the call is live and the hold has not expired (a stuck call stops at
#   hold_until);
# - the call ended billable (the voicebot bills a call that was answered or
#   completed) and its usage row is not priced yet. The rater writes the
#   debit and stamps priced_at in one transaction, and stamps a 0-cent row
#   with no debit. A row it never prices stops counting UNPRICED_HOLD_DAYS
#   after hold_until;
# - the call ended billable with no usage row, within the grace. The
#   voicebot writes the row with the final status, so a call still without
#   one (0 ms, or closed by another writer) is not billed.
# A call in a final status with no end time counts from its last update.
# The ref match is exact so it uses usage_events_voice_ref_idx.
_HELD_SQL = text("""
    SELECT COALESCE(SUM(h.cents), 0)
    FROM call_balance_holds h
    JOIN calls c ON c.id = h.call_id
    WHERE c.organization_id = :org
      AND h.call_id IS DISTINCT FROM :exclude
      AND (
        (c.ended_at IS NULL AND c.status NOT IN :terminal
         AND h.hold_until > :now)
        OR (
          (c.ended_at IS NOT NULL OR c.status IN :terminal)
          AND (c.answered_at IS NOT NULL OR c.status = 'completed')
          AND CASE (
            SELECT bool_or(u.priced_at IS NULL)
            FROM usage_events u
            WHERE u.organization_id = :org
              AND u.channel = 'voice'
              AND u.ref IN (c.id::text, c.id::text || :recording_suffix)
          )
            WHEN true THEN h.hold_until > :unpriced_until
            WHEN false THEN false
            ELSE h.hold_until > :now
              AND COALESCE(c.ended_at, c.updated_at) > :debit_window
          END
        )
      )
""").bindparams(bindparam("terminal", expanding=True))


async def held_cents(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    exclude: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Decimal:
    """What the organization's live calls have set aside, in cents."""
    now = now or datetime.now(UTC)
    result = await db.execute(
        _HELD_SQL,
        {
            "org": organization_id,
            "exclude": exclude,
            "now": now,
            "debit_window": now - timedelta(seconds=HOLD_GRACE_SECONDS),
            "unpriced_until": now - timedelta(days=UNPRICED_HOLD_DAYS),
            "recording_suffix": RECORDING_USAGE_REF_SUFFIX,
            "terminal": sorted(TERMINAL_CALL_STATUSES),
        },
    )
    return Decimal(result.scalar_one())


async def available_cents(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    exclude: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Decimal:
    """The balance less every live call's hold. Take ``org_lock`` first, so
    two calls starting at once do not both spend the same money."""
    ledger = await get_ledger_cents(db, organization_id)
    return ledger - await held_cents(db, organization_id, exclude=exclude, now=now)


async def hold(
    db: AsyncSession,
    call_id: uuid.UUID,
    cents: Decimal,
    until: datetime,
    *,
    dial: bool = False,
) -> None:
    """Set aside ``cents`` for a call until ``until``, or grow its hold to
    at least that: a hold never shrinks, even when two handovers race. The
    column keeps tenths of a cent, so the amount is rounded up. ``dial``
    counts one more approved handover dial. Also deletes up to 100 holds a
    day past their end, skipping rows another transaction has locked, so no
    call waits on another's cleanup."""
    await db.execute(_PRUNE_SQL)
    stmt = insert(CallBalanceHold).values(
        call_id=call_id,
        cents=round_up_tenth(cents),
        hold_until=until,
        dials=1 if dial else 0,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[CallBalanceHold.call_id],
            set_={
                "cents": func.greatest(CallBalanceHold.cents, stmt.excluded.cents),
                "hold_until": func.greatest(
                    CallBalanceHold.hold_until, stmt.excluded.hold_until
                ),
                "dials": CallBalanceHold.dials + stmt.excluded.dials,
            },
        )
    )


async def handover_dials(db: AsyncSession, call_id: uuid.UUID) -> int:
    """How many handover dials were approved for the call so far."""
    dials = (
        await db.execute(
            select(CallBalanceHold.dials).where(CallBalanceHold.call_id == call_id)
        )
    ).scalar_one_or_none()
    return dials or 0


_PRUNE_SQL = text("""
    DELETE FROM call_balance_holds
    WHERE call_id IN (
      SELECT call_id FROM call_balance_holds
      WHERE hold_until < now() - interval '1 day'
      LIMIT 100
      FOR UPDATE SKIP LOCKED
    )
""")
