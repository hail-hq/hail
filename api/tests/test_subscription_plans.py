"""Subscription plans: the funds gate, the call cap and number purchases
count the plan month (hailhq.core.plans)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from hailhq.core.billing import get_balance_cents, has_funds, monthly_fee_ref
from hailhq.core.call_allowance import hold_parts, plan_minutes_left, plan_split
from hailhq.core.config import settings
from hailhq.core.internal_webhook import CallQuote
from hailhq.core.models import (
    AccountCredit,
    Call,
    CallBalanceHold,
    NumberOffer,
    OrganizationCallSettings,
    OrganizationPlan,
    PlanAllowance,
)
from hailhq.core.number_offers import CarrierOffer
from hailhq.core.plans import (
    OVER_PLAN_REF_SUFFIX,
    PLAN_NUMBER_SOURCE,
    current_allowance,
    plan_allows,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .conftest import insert_org_and_key
from .test_verifications_api import pinned_catalog  # noqa: F401 (autouse fixture)

SECRET = "test-internal-secret"
ABROAD = "+2348100000000"


async def _plan(
    s,
    org,
    *,
    tier="pro",
    status="active",
    minutes=600,
    used=0,
    texts=10,
    emails=10,
    numbers=2,
    used_numbers=0,
    cap=200,
    countries=("US",),
) -> PlanAllowance:
    now = datetime.now(timezone.utc)
    plan = OrganizationPlan(
        organization_id=org,
        tier=tier,
        step_hours=10,
        billing_interval="month",
        status=status,
        stripe_customer_id="cus_test",
        stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
        started_at=now - timedelta(days=3),
        current_period_end=now + timedelta(days=27),
    )
    s.add(plan)
    await s.flush()
    row = PlanAllowance(
        organization_id=org,
        plan_id=plan.id,
        period_start=now - timedelta(days=3),
        period_end=now + timedelta(days=27),
        included_minutes=Decimal(minutes),
        used_minutes=Decimal(used),
        included_texts=texts,
        included_emails=emails,
        included_numbers=numbers,
        used_numbers=used_numbers,
        text_countries=list(countries),
        number_fee_cap_cents=Decimal(cap),
    )
    s.add(row)
    await s.commit()
    return row


@pytest.fixture()
def billing_on(monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", SECRET)
    monkeypatch.setattr(settings, "hail_base_url", "https://billing.example")


# ---------------------------------------------------------------------------
# Funds gate


async def test_plan_minutes_pass_the_gate_with_an_empty_wallet(async_session):
    org, _, _ = await insert_org_and_key(async_session, initial_credit_cents=0)
    assert not await has_funds(async_session, org, "call")
    await _plan(async_session, org)
    assert await has_funds(async_session, org, "call")
    assert await has_funds(async_session, org, "email")
    # Balance-only checks (purchases) ignore the plan.
    assert not await has_funds(async_session, org)


async def test_plan_texts_count_only_for_its_countries(async_session):
    org, _, _ = await insert_org_and_key(async_session, initial_credit_cents=0)
    await _plan(async_session, org, countries=("US",))
    assert await has_funds(async_session, org, "text", country="US")
    assert not await has_funds(async_session, org, "text", country="DE")
    assert not await has_funds(async_session, org, "text")


async def test_used_up_or_unpaid_plan_fails_the_gate(async_session):
    org, _, _ = await insert_org_and_key(async_session, initial_credit_cents=0)
    await _plan(async_session, org, minutes=60, used=60, texts=0, emails=0)
    assert not await has_funds(async_session, org, "call")
    assert not await has_funds(async_session, org, "text", country="US")
    assert not await has_funds(async_session, org, "email")
    other, _, _ = await insert_org_and_key(
        async_session, org_slug="b", initial_credit_cents=0
    )
    await _plan(async_session, other, status="past_due")
    assert not await has_funds(async_session, other, "call")
    assert await current_allowance(async_session, other) is None


# ---------------------------------------------------------------------------
# Schema


async def test_one_open_plan_per_workspace(async_session):
    org, _, _ = await insert_org_and_key(async_session, initial_credit_cents=0)
    await _plan(async_session, org)
    with pytest.raises(IntegrityError):
        await _plan(async_session, org)
    await async_session.rollback()


async def test_plan_ledger_row_moves_no_money(async_session):
    org, _, _ = await insert_org_and_key(async_session, initial_credit_cents=500)
    async_session.add(
        AccountCredit(
            organization_id=org,
            kind="plan",
            channel="voice",
            amount_cents=0,
            plan_units=Decimal("12.000"),
            ref="plan:test",
            source="plan",
        )
    )
    await async_session.commit()
    assert await get_balance_cents(async_session, org) == 500
    async_session.add(
        AccountCredit(
            organization_id=org,
            kind="plan",
            channel="voice",
            amount_cents=-5,
            plan_units=Decimal(1),
            ref="plan:bad",
            source="plan",
        )
    )
    with pytest.raises(IntegrityError):
        await async_session.commit()
    await async_session.rollback()


# ---------------------------------------------------------------------------
# Feature gate


async def test_plan_allows_by_tier(async_session, billing_on):
    pro, _, _ = await insert_org_and_key(async_session, initial_credit_cents=0)
    biz, _, _ = await insert_org_and_key(
        async_session, org_slug="b", initial_credit_cents=0
    )
    none, _, _ = await insert_org_and_key(
        async_session, org_slug="c", initial_credit_cents=0
    )
    await _plan(async_session, pro, tier="pro")
    await _plan(async_session, biz, tier="business")
    assert await plan_allows(async_session, pro, "porting")
    assert not await plan_allows(async_session, pro, "connected_phone_systems")
    assert await plan_allows(async_session, biz, "connected_phone_systems")
    assert not await plan_allows(async_session, none, "porting")


async def test_self_host_allows_every_feature(async_session, monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", "")
    org, _, _ = await insert_org_and_key(async_session, initial_credit_cents=0)
    assert await plan_allows(async_session, org, "connected_phone_systems")


# ---------------------------------------------------------------------------
# Call cap


def test_plan_split():
    standard = CallQuote(Decimal(10), Decimal(0), Decimal(1), Decimal(10))
    # No plan minutes: the balance alone, as before.
    assert plan_split(Decimal(100), Decimal(0), standard, Decimal(0)) == (10, 0)
    # 30 plan minutes and an empty wallet.
    assert plan_split(Decimal(0), Decimal(30), standard, Decimal(0)) == (30, 30)
    # Then the balance: 30 + 5 minutes at 10¢.
    assert plan_split(Decimal(50), Decimal(30), standard, Decimal(0)) == (35, 30)
    # Plus minutes use 2 plan minutes each.
    plus = CallQuote(Decimal(20), Decimal(0), Decimal(2), Decimal(20))
    assert plan_split(Decimal(0), Decimal(31), plus, Decimal(0)) == (15, 15)
    # Recording (1¢) is paid from credits: an empty wallet gets nothing.
    recorded = CallQuote(Decimal(11), Decimal(0), Decimal(1), Decimal(10))
    assert plan_split(Decimal(0), Decimal(30), recorded, Decimal(0)) == (0, 0)
    assert plan_split(Decimal(5), Decimal(30), recorded, Decimal(0)) == (5, 5)
    # A quote without plan fields ignores the plan.
    old = CallQuote(Decimal(10), Decimal(0))
    assert plan_split(Decimal(0), Decimal(30), old, Decimal(0)) == (0, 0)
    assert hold_parts(recorded, Decimal(0), 6, 5) == (Decimal(16), Decimal(5))


@pytest.fixture()
def quote(monkeypatch):
    class Quote:
        price: CallQuote | None = CallQuote(
            Decimal(10), Decimal(0), Decimal(1), Decimal(10)
        )

    async def fake(org, legs, recording):
        return Quote.price

    monkeypatch.setattr(settings, "hail_internal_secret", SECRET)
    monkeypatch.setattr("hailhq.api.routes.calls.quote_call_price", fake)
    monkeypatch.setattr("hailhq.api.routes.calls.quotes_enabled", lambda: True)
    return Quote


async def _call(client, key):
    return await client.post(
        "/calls",
        json={"to": ABROAD, "system_prompt": "hi", "recipient_consent": True},
        headers={"Authorization": f"Bearer {key}"},
    )


async def test_call_runs_on_plan_minutes(
    client, async_session, livekit_mock, add_phone_number, quote
):
    org, _, key = await insert_org_and_key(async_session, initial_credit_cents=0)
    async_session.add(
        OrganizationCallSettings(organization_id=org, max_duration_seconds=3600)
    )
    await async_session.commit()
    await add_phone_number(async_session, org)
    # No plan: refused, as before.
    assert (await _call(client, key)).status_code == 402
    await _plan(async_session, org, minutes=12)
    r = await _call(client, key)
    assert r.status_code == 201, r.text
    call = await async_session.get(Call, uuid.UUID(r.json()["id"]))
    assert call.max_duration_seconds == 12 * 60 - 10
    async_session.expire_all()
    hold = await async_session.get(CallBalanceHold, call.id)
    assert hold.cents == 0
    assert hold.plan_minutes == 12
    # The live call holds every plan minute: the next call is refused.
    assert await plan_minutes_left(async_session, org) == 0
    assert (await _call(client, key)).status_code == 402


async def test_quote_without_plan_fields_is_todays_cap(
    client, async_session, livekit_mock, add_phone_number, quote
):
    quote.price = CallQuote(Decimal(40), Decimal(0))
    org, _, key = await insert_org_and_key(async_session, initial_credit_cents=500)
    async_session.add(
        OrganizationCallSettings(organization_id=org, max_duration_seconds=3600)
    )
    await async_session.commit()
    await add_phone_number(async_session, org)
    await _plan(async_session, org, minutes=600)
    r = await _call(client, key)
    assert r.status_code == 201, r.text
    call = await async_session.get(Call, uuid.UUID(r.json()["id"]))
    # $5 at 40¢: 12 minutes, the plan unused.
    assert call.max_duration_seconds == 710
    async_session.expire_all()
    hold = await async_session.get(CallBalanceHold, call.id)
    assert hold.plan_minutes == 0


# ---------------------------------------------------------------------------
# Number purchases


async def _buy(client, session, monkeypatch, org, key, *, monthly):
    offer = CarrierOffer(
        provider="twilio",
        e164="+14155550199",
        country_code="US",
        number_type="local",
        capabilities=["voice", "sms"],
        monthly_cents=monthly,
        setup_cents=0,
        readiness="ready",
    )
    quote = NumberOffer(
        organization_id=org,
        offer=offer.model_dump(mode="json"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(quote)
    await session.commit()
    monkeypatch.setattr(
        "hailhq.core.providers.voice.twilio.purchase_ordered_number",
        AsyncMock(return_value="PN_us_1"),
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    return await client.post(
        "/numbers",
        json={"country_code": "US", "quote_id": str(quote.id)},
        headers={"Authorization": f"Bearer {key}"},
    )


async def _rows(session, org):
    session.expire_all()
    return {
        r.ref: r
        for r in (
            await session.execute(
                select(AccountCredit).where(AccountCredit.organization_id == org)
            )
        ).scalars()
    }


async def test_included_number_first_month_is_paid_by_the_plan(
    client, async_session, monkeypatch
):
    org, _, key = await insert_org_and_key(async_session, initial_credit_cents=0)
    allowance = await _plan(async_session, org, numbers=1, cap=200)
    r = await _buy(client, async_session, monkeypatch, org, key, monthly=115)
    assert r.status_code == 201, r.text
    number_id = uuid.UUID(r.json()["id"])
    rows = await _rows(async_session, org)
    fee = monthly_fee_ref(org, number_id, datetime.now(timezone.utc))
    assert rows[fee].kind == "plan"
    assert rows[fee].source == PLAN_NUMBER_SOURCE
    assert rows[fee].plan_units == 1
    assert f"{fee}{OVER_PLAN_REF_SUFFIX}" not in rows
    assert await get_balance_cents(async_session, org) == 0
    await async_session.refresh(allowance)
    assert allowance.used_numbers == 1


async def test_number_above_the_cap_pays_the_rest(client, async_session, monkeypatch):
    org, _, key = await insert_org_and_key(async_session, initial_credit_cents=100)
    await _plan(async_session, org, numbers=1, cap=200)
    r = await _buy(client, async_session, monkeypatch, org, key, monthly=250)
    assert r.status_code == 201, r.text
    number_id = uuid.UUID(r.json()["id"])
    rows = await _rows(async_session, org)
    fee = monthly_fee_ref(org, number_id, datetime.now(timezone.utc))
    assert rows[fee].kind == "plan"
    assert rows[f"{fee}{OVER_PLAN_REF_SUFFIX}"].amount_cents == -50
    assert await get_balance_cents(async_session, org) == 50


async def test_numbers_beyond_the_plan_are_paid_from_credits(
    client, async_session, monkeypatch
):
    org, _, key = await insert_org_and_key(async_session, initial_credit_cents=0)
    await _plan(async_session, org, numbers=1, used_numbers=1)
    r = await _buy(client, async_session, monkeypatch, org, key, monthly=115)
    assert r.status_code == 402
