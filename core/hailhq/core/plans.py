"""Subscription plans: what a workspace's plan includes and lets through.

The billing site owns Stripe and writes ``organization_plans`` (one row per
subscription) and ``plan_allowances`` (one row per plan month, with what it
includes and what it used). hail reads them, and stores amounts only, never
prices. Spec: docs/superpowers/specs/2026-11-25-subscription-plans-design.md.

A plan is on only while its open row's ``status`` is ``active``: a failed
payment (``past_due``) turns plan use and plan features off at once.

Self-host: with billing off (no billing site configured), every feature is
allowed and no plan row exists.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, Literal

from hailhq.core.carrier_routing import PBX
from hailhq.core.internal_webhook import quotes_enabled
from hailhq.core.models import (
    AccountCredit,
    OrganizationPlan,
    PhoneNumber,
    PlanAllowance,
)
from sqlalchemy import Text, and_, cast, exists, func, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "FEATURE_TIERS",
    "OVER_PLAN_REF_SUFFIX",
    "PLAN_NUMBER_SOURCE",
    "Feature",
    "PlanUse",
    "active_plan",
    "billing_enabled",
    "current_allowance",
    "plan_allows",
    "plan_covers",
    "plan_number_cents",
]

Feature = Literal["porting", "connected_phone_systems"]
PlanUse = Literal["call", "text", "email"]

# The tiers that include each paid feature. A new feature is one entry here.
FEATURE_TIERS: Final[dict[str, frozenset[str]]] = {
    "porting": frozenset({"pro", "business", "enterprise"}),
    "connected_phone_systems": frozenset({"business", "enterprise"}),
}

# account_credits.source of a number fee the plan pays (kind = 'plan'). The
# billing site's raters write usage rows with source 'plan' (spec).
PLAN_NUMBER_SOURCE: Final = "plan.monthly_fee"
# A number's monthly fee above the plan's per-number cap is a debit with the
# month's fee ref plus this; the plan row takes the fee ref itself, so the
# billing site's renewal rater sees the month as billed.
OVER_PLAN_REF_SUFFIX: Final = ":over_plan"


def billing_enabled() -> bool:
    """True when the billing site prices usage (Hail's cloud). Self-host:
    False, so plan gates allow everything."""
    return quotes_enabled()


def _on():
    return (OrganizationPlan.ended_at.is_(None), OrganizationPlan.status == "active")


async def active_plan(
    db: AsyncSession, organization_id: uuid.UUID
) -> OrganizationPlan | None:
    """The workspace's plan while it is on, else None."""
    return (
        await db.execute(
            select(OrganizationPlan).where(
                OrganizationPlan.organization_id == organization_id, *_on()
            )
        )
    ).scalar_one_or_none()


async def current_allowance(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    now: datetime | None = None,
    for_update: bool = False,
) -> PlanAllowance | None:
    """The plan month covering ``now`` while the plan is on, else None.
    ``for_update`` locks the row (a writer that raises a used amount)."""
    now = now or datetime.now(UTC)
    stmt = (
        select(PlanAllowance)
        .join(OrganizationPlan, OrganizationPlan.id == PlanAllowance.plan_id)
        .where(
            PlanAllowance.organization_id == organization_id,
            PlanAllowance.period_start <= now,
            PlanAllowance.period_end > now,
            *_on(),
        )
        .order_by(PlanAllowance.period_start.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update(of=PlanAllowance)
    return (await db.execute(stmt)).scalar_one_or_none()


def _covers(row: PlanAllowance, use: PlanUse, country: str | None) -> bool:
    if use == "call":
        return Decimal(row.used_minutes) < Decimal(row.included_minutes)
    if use == "text":
        return (
            country is not None
            and country.upper() in (row.text_countries or [])
            and row.used_texts < row.included_texts
        )
    return row.used_emails < row.included_emails


async def plan_covers(
    db: AsyncSession,
    organization_id: uuid.UUID,
    use: PlanUse,
    *,
    country: str | None = None,
    now: datetime | None = None,
) -> bool:
    """True when the plan is on and this month still includes ``use``: call
    minutes, texts to ``country`` (only the month's ``text_countries``), or
    emails. The raters raise the used amounts on a schedule, so this reads
    them a few minutes late; a small overrun is paid from credits."""
    row = await current_allowance(db, organization_id, now=now)
    return row is not None and _covers(row, use, country)


async def plan_allows(
    db: AsyncSession, organization_id: uuid.UUID, feature: Feature
) -> bool:
    """True when the workspace's plan includes ``feature`` (FEATURE_TIERS),
    or when billing is off (self-host)."""
    if not billing_enabled():
        return True
    plan = await active_plan(db, organization_id)
    return plan is not None and plan.tier in FEATURE_TIERS[feature]


async def plan_number_cents(
    db: AsyncSession,
    organization_id: uuid.UUID,
    monthly_cents: int,
    number_id: uuid.UUID | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """Whole cents the plan pays toward one month's fee of a number: the
    plan pays the fees of the workspace's ``included_numbers`` oldest
    numbers with a monthly fee (not pool or connected numbers; active or
    being ordered; ranked by ``(created_at, id)``), up to
    ``number_fee_cap_cents`` each, while the plan is on. A number the plan
    already paid this calendar month keeps its place until the month ends
    and counts against every other number, whatever its rank or state.
    ``number_id=None`` asks for a number about to be ordered (the
    newest). 0 when the plan does not pay. The billing site's renewal rater
    uses the same rule each month. Take ``org_lock`` first, so two orders
    do not both take the last included number."""
    row = await current_allowance(db, organization_id, now=now)
    if row is None or row.included_numbers <= 0:
        return 0
    now = now or datetime.now(UTC)
    held = [
        PhoneNumber.is_pool.is_(False),
        PhoneNumber.provider != PBX,
        PhoneNumber.provisioning_state.in_(("active", "pending")),
    ]
    others = [PhoneNumber.organization_id == organization_id]
    if number_id is not None:
        me = await db.get(PhoneNumber, number_id)
        if me is None or me.organization_id != organization_id:
            return 0
        others.append(PhoneNumber.id != number_id)
        # Held numbers count only when ranked before it; one the plan paid
        # this month counts whatever its rank.
        held.append(
            tuple_(PhoneNumber.created_at, PhoneNumber.id)
            < tuple_(literal(me.created_at), literal(me.id))
        )
    # A number the plan already paid this calendar month keeps its place
    # until the month ends, even once released: a release and a new
    # purchase never get a second included first month. Same ref format as
    # hailhq.core.billing.monthly_fee_ref.
    fee_ref = (
        literal(f"monthly_fee:{organization_id}:")
        + cast(PhoneNumber.id, Text)
        + literal(f":dedicated_number:{now:%Y-%m}")
    )
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    paid_this_month = and_(
        # Only rows that can hold this month's ref: not failed orders, not
        # numbers released before the month began.
        PhoneNumber.provisioning_state != "failed",
        or_(PhoneNumber.released_at.is_(None), PhoneNumber.released_at >= month_start),
        exists().where(
            AccountCredit.organization_id == organization_id,
            AccountCredit.kind == "plan",
            AccountCredit.ref == fee_ref,
        ),
    )
    older = (
        await db.execute(
            select(func.count())
            .select_from(PhoneNumber)
            .where(*others, or_(and_(*held), paid_this_month))
        )
    ).scalar_one()
    if older >= row.included_numbers:
        return 0
    return max(0, min(monthly_cents, int(Decimal(row.number_fee_cap_cents))))
