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

A workspace's plan pays too (hailhq.core.plans). The quote says how many
plan minutes a call minute uses and what part of its price they pay
(``CallQuote``). A call first runs on the plan minutes left (the month's
less what it used less every live call's plan hold), paying only the rest
of the price from the balance, then on the balance alone. The hold keeps
both parts: ``cents`` from the balance and ``plan_minutes`` from the plan.
With no plan, or a quote without the plan fields, it is the balance alone.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hailhq.core.internal_webhook import CallQuote
from hailhq.core.models import CallBalanceHold
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from sqlalchemy import bindparam, case, func, null, or_, select, text
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


def _floor_tenth(cents: Decimal) -> Decimal:
    return Decimal(math.floor(cents * 10)) / 10


def plan_split(
    available_cents: Decimal,
    plan_minutes: Decimal,
    quote: CallQuote,
    fee: Decimal,
) -> tuple[int, int]:
    """Whole minutes a call may run on ``plan_minutes`` (plan minutes left)
    and ``available_cents`` together, and how many of them the plan pays.
    The balance pays ``fee`` once, ``cents_per_minute -
    plan_cents_per_minute`` for each plan-paid minute, and the full price
    after them; ``hold_parts`` stores what this allows."""
    weight = quote.plan_minutes_per_minute
    per_minute = quote.cents_per_minute
    on_plan = int(plan_minutes // weight) if weight > 0 and plan_minutes > 0 else 0
    if on_plan == 0:
        return minutes_affordable(available_cents, per_minute, fee), 0
    rest_price = per_minute - quote.plan_cents_per_minute
    if rest_price > 0:
        covered = min(on_plan, minutes_affordable(available_cents, rest_price, fee))
    elif fee == 0 or round_up_tenth(fee) <= _floor_tenth(available_cents):
        covered = on_plan
    else:
        covered = 0
    if covered < on_plan:
        return covered, covered
    after = minutes_affordable(available_cents, per_minute, fee + rest_price * on_plan)
    return on_plan + after, on_plan


def hold_parts(
    quote: CallQuote, fee: Decimal, billed: int, on_plan: int
) -> tuple[Decimal, Decimal]:
    """What ``billed`` minutes set aside, the first ``on_plan`` of them paid
    by the plan: ``(cents from the balance, plan minutes)``."""
    paid = min(billed, on_plan)
    rest_price = quote.cents_per_minute - quote.plan_cents_per_minute
    cents = fee + rest_price * paid + quote.cents_per_minute * (billed - paid)
    return cents, quote.plan_minutes_per_minute * paid


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


# What each hold counts:
# - a live call: the full hold, until hold_until (a stuck call stops there);
# - a call that ended billable (the voicebot bills a call that was answered
#   or completed) whose usage row is not priced yet: what it really cost, at
#   most the hold. That is the highest price per minute held for it times
#   its billed minutes, plus its fees; each handover dial adds a minute and
#   a fee (it may have billed the second line), and one more minute covers
#   carrier legs billed a little longer than the voicebot measured. With no
#   price stored (a handover ran at a price Hail could not get), the full
#   hold (LEAST skips the NULL). The rater writes the debit and stamps
#   priced_at in one transaction, and stamps a 0-cent row with no debit.
#   A row it never prices stops counting UNPRICED_HOLD_DAYS after
#   hold_until;
# - a call that ended billable with no usage row: the full hold, within the
#   grace. The voicebot writes the row with the final status, so a call
#   still without one (0 ms, or closed by another writer) is not billed;
# - anything else (unbilled, priced): nothing.
# A call in a final status with no end time counts from its last update.
# A voice ref is the call id, plus an optional ':<suffix>' (the rater
# parses it the same way); usage_events_voice_ref_idx serves the match.
# The plan minutes a hold counts follow the same branches, except that an
# ended, unpriced call counts all of them (the rater settles them).
_HELD_TEMPLATE = """
    SELECT COALESCE(SUM(
      CASE
        WHEN c.ended_at IS NULL AND c.status NOT IN :terminal THEN
          CASE WHEN h.hold_until > :now THEN h.{col} ELSE 0 END
        WHEN NOT (c.answered_at IS NOT NULL OR c.status = 'completed') THEN 0
        WHEN u.unpriced THEN
          CASE WHEN h.hold_until > :unpriced_until THEN {unpriced} ELSE 0 END
        WHEN u.unpriced IS NULL
          AND h.hold_until > :now
          AND COALESCE(c.ended_at, c.updated_at) > :debit_window THEN h.{col}
        ELSE 0
      END
    ), 0)
    FROM call_balance_holds h
    JOIN calls c ON c.id = h.call_id
    CROSS JOIN LATERAL (
      SELECT bool_or(u.priced_at IS NULL) AS unpriced,
             SUM(u.units) FILTER (WHERE u.priced_at IS NULL) AS ms
      FROM usage_events u
      WHERE u.organization_id = :org
        AND u.channel = 'voice'
        AND split_part(u.ref, ':', 1) = c.id::text
    ) u
    WHERE c.organization_id = :org
      AND h.call_id IS DISTINCT FROM :exclude
      -- No branch counts a hold this old; skip its usage lookup.
      AND h.hold_until > :unpriced_until
"""
_HELD_QUERY = _HELD_TEMPLATE.format(
    col="cents",
    unpriced="""LEAST(
            h.cents,
            h.cents_per_minute * (CEIL(u.ms / 60000.0) + 1 + h.dials)
              + h.cents_per_call * (1 + h.dials)
          )""",
)
_HELD_PLAN_QUERY = (
    _HELD_TEMPLATE.format(col="plan_minutes", unpriced="h.plan_minutes")
    + "      AND h.plan_minutes > 0\n"
)
_HELD_SQL = text(_HELD_QUERY).bindparams(bindparam("terminal", expanding=True))

# The ledger and the holds in one statement, so both come from the same
# snapshot: a debit the rater commits between two reads (it takes no org
# lock) would otherwise be counted in neither.
_AVAILABLE_SQL = text(
    "SELECT (SELECT COALESCE(SUM(amount_cents), 0) FROM account_credits"
    " WHERE organization_id = :org) - (" + _HELD_QUERY + ")"
).bindparams(bindparam("terminal", expanding=True))


# The plan minutes left this month: included less used less every live
# call's plan hold, in one statement, so a rater commit (it raises
# used_minutes and stamps the usage priced together) is counted once. NULL
# when the plan is off or no month covers now.
_PLAN_LEFT_SQL = text(
    "SELECT a.included_minutes - a.used_minutes - (" + _HELD_PLAN_QUERY + """)
    FROM plan_allowances a
    JOIN organization_plans p ON p.id = a.plan_id
    WHERE a.organization_id = :org
      AND a.period_start <= :now AND a.period_end > :now
      AND a.period_start < p.current_period_end
      AND p.ended_at IS NULL AND p.status = 'active'
    ORDER BY a.period_start DESC
    LIMIT 1
    """
).bindparams(bindparam("terminal", expanding=True))


def _held_params(
    organization_id: uuid.UUID, exclude: uuid.UUID | None, now: datetime
) -> dict:
    return {
        "org": organization_id,
        "exclude": exclude,
        "now": now,
        "debit_window": now - timedelta(seconds=HOLD_GRACE_SECONDS),
        "unpriced_until": now - timedelta(days=UNPRICED_HOLD_DAYS),
        "terminal": sorted(TERMINAL_CALL_STATUSES),
    }


async def held_cents(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    exclude: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Decimal:
    """What the organization's live calls have set aside, in cents."""
    now = now or datetime.now(UTC)
    result = await db.execute(_HELD_SQL, _held_params(organization_id, exclude, now))
    return Decimal(result.scalar_one())


async def available_cents(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    exclude: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Decimal:
    """The balance less every live call's hold, read in one statement. Take
    ``org_lock`` first, so two calls starting at once do not both spend the
    same money."""
    now = now or datetime.now(UTC)
    result = await db.execute(
        _AVAILABLE_SQL, _held_params(organization_id, exclude, now)
    )
    return Decimal(result.scalar_one())


async def plan_minutes_left(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    exclude: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Decimal:
    """The plan minutes the workspace's live calls have not set aside yet
    this month (0 with no plan). Take ``org_lock`` first, as for
    ``available_cents``."""
    now = now or datetime.now(UTC)
    left = (
        await db.execute(_PLAN_LEFT_SQL, _held_params(organization_id, exclude, now))
    ).scalar_one_or_none()
    return max(Decimal(left or 0), Decimal(0))


async def hold(
    db: AsyncSession,
    call_id: uuid.UUID,
    cents: Decimal,
    until: datetime,
    *,
    cents_per_minute: Decimal | None,
    cents_per_call: Decimal | None,
    dial: bool = False,
    plan_minutes: Decimal = Decimal(0),
) -> None:
    """Set aside ``cents`` for a call until ``until``, or grow its hold to
    at least that: a hold never shrinks, even when two handovers race. The
    column keeps tenths of a cent, so the amount is rounded up. The prices
    (the highest held for the call is kept) set what the hold counts once
    the call ends (_HELD_SQL). None means a price Hail does not know: the
    full hold counts, and the prices stay None from then on. ``dial``
    counts one more approved handover dial. ``plan_minutes`` sets aside
    plan minutes the same way (only grows). Also deletes up to 100 holds a
    day past their end, skipping rows another transaction has locked, so no
    call waits on another's cleanup."""
    await db.execute(_PRUNE_SQL, {"days": UNPRICED_HOLD_DAYS})
    stmt = insert(CallBalanceHold).values(
        call_id=call_id,
        cents=round_up_tenth(cents),
        hold_until=until,
        cents_per_minute=cents_per_minute,
        cents_per_call=cents_per_call,
        dials=1 if dial else 0,
        plan_minutes=plan_minutes,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[CallBalanceHold.call_id],
            set_={
                "cents": func.greatest(CallBalanceHold.cents, stmt.excluded.cents),
                "hold_until": func.greatest(
                    CallBalanceHold.hold_until, stmt.excluded.hold_until
                ),
                "cents_per_minute": _kept_price(
                    CallBalanceHold.cents_per_minute, stmt.excluded.cents_per_minute
                ),
                "cents_per_call": _kept_price(
                    CallBalanceHold.cents_per_call, stmt.excluded.cents_per_call
                ),
                "dials": CallBalanceHold.dials + stmt.excluded.dials,
                "plan_minutes": func.greatest(
                    CallBalanceHold.plan_minutes, stmt.excluded.plan_minutes
                ),
            },
        )
    )


def _kept_price(old, new):
    """The higher price, or NULL once either is unknown (GREATEST would skip
    the NULL)."""
    return case(
        (or_(old.is_(None), new.is_(None)), null()), else_=func.greatest(old, new)
    )


async def forget_price(db: AsyncSession, call_id: uuid.UUID, until: datetime) -> None:
    """Mark the call's hold as run at an unknown price (a handover whose
    quote failed), so the full hold counts after the call ends, and count
    the approved dial, so a later handover's hold covers it. A call that
    holds nothing (inbound, or started at an unknown price) gets a 0-cent
    row, so a later priced handover keeps the prices unknown and counts
    this dial too. ``until`` is when the handover can end at the latest: the
    call runs on past its own limit, so its hold must keep counting."""
    await hold(
        db,
        call_id,
        Decimal(0),
        until,
        cents_per_minute=None,
        cents_per_call=None,
        dial=True,
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
      WHERE hold_until < now() - make_interval(days => :days)
      LIMIT 100
      FOR UPDATE SKIP LOCKED
    )
""")
