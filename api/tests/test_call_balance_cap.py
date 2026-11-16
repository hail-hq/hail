"""A call's time limit follows the balance (hailhq.core.call_allowance)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import ClassVar

import pytest
from hailhq.core import hmac_signing
from hailhq.core.billing import CALL_META_BILLED
from hailhq.core.call_allowance import (
    available_cents,
    cap_seconds,
    hold,
    minutes_affordable,
    round_up_tenth,
)
from hailhq.core.config import settings
from hailhq.core.models import (
    AccountCredit,
    Agent,
    AgentHandoverContact,
    AuditLog,
    Call,
    CallBalanceHold,
    Contact,
    OrganizationCallSettings,
    PhoneNumber,
    UsageEvent,
)
from sqlalchemy import select

from .conftest import insert_org_and_key

# A made-up number abroad, priced well above the US minimum.
ABROAD = "+2348100000000"
SECRET = "test-internal-secret"
# The quote fixture's default price, as a hold stores it.
PRICES = {"cents_per_minute": Decimal(40), "cents_per_call": Decimal(0)}


@pytest.fixture()
def quote(monkeypatch):
    """Price every call at ``quote.price`` (cents per minute, per call)."""

    class Quote:
        price: tuple[Decimal, Decimal] | None = (Decimal(40), Decimal(0))
        legs: ClassVar[list] = []

    async def fake(org, legs, recording):
        Quote.legs.append(legs)
        return Quote.price

    monkeypatch.setattr(settings, "hail_internal_secret", SECRET)
    for module in ("calls", "internal.agent"):
        target = f"hailhq.api.routes.{module}"
        monkeypatch.setattr(f"{target}.quote_call_price", fake)
        monkeypatch.setattr(f"{target}.quotes_enabled", lambda: True)
    return Quote


async def _hold(s, call_id):
    s.expire_all()
    return await s.get(CallBalanceHold, call_id)


async def _org(s, cents, limit=3600):
    org, _, key = await insert_org_and_key(s, initial_credit_cents=cents)
    s.add(OrganizationCallSettings(organization_id=org, max_duration_seconds=limit))
    await s.commit()
    return org, key


async def _call(client, key, to=ABROAD):
    return await client.post(
        "/calls",
        json={"to": to, "system_prompt": "hi", "recipient_consent": True},
        headers={"Authorization": f"Bearer {key}"},
    )


def test_minutes_affordable():
    assert minutes_affordable(Decimal(500), Decimal(40), Decimal(0)) == 12
    assert minutes_affordable(Decimal(30), Decimal(40), Decimal(0)) == 0
    assert minutes_affordable(Decimal(100), Decimal(10), Decimal(5)) == 9
    assert minutes_affordable(Decimal(5), Decimal(10), Decimal(5)) == 0
    # The exact amount fits; a hold that rounds up past the balance does not.
    assert minutes_affordable(Decimal(25), Decimal(20), Decimal(5)) == 1
    assert minutes_affordable(Decimal("2.54"), Decimal("1.27"), Decimal(0)) == 1
    assert minutes_affordable(Decimal("2.54"), Decimal("0.02"), Decimal(0)) == 125
    assert minutes_affordable(Decimal(10), Decimal("1.99"), Decimal("0.05")) == 5
    assert cap_seconds(11) == 650
    assert cap_seconds(0) == 0


async def test_call_is_capped_to_the_balance(
    client, async_session, livekit_mock, add_phone_number, quote
):
    org, key = await _org(async_session, 500)
    await add_phone_number(async_session, org)
    r = await _call(client, key)
    assert r.status_code == 201, r.text
    call = await async_session.get(Call, uuid.UUID(r.json()["id"]))
    # $5 at 40¢ a minute: 12 whole minutes, less the goodbye.
    assert call.max_duration_seconds == 710
    row = await _hold(async_session, call.id)
    assert row.cents == Decimal(40) * 12
    # The price it shrinks to once the call ends.
    assert (row.cents_per_minute, row.cents_per_call) == (Decimal(40), Decimal(0))
    meta = livekit_mock.dispatch_agent.await_args.kwargs["metadata"]
    assert meta["max_duration_seconds"] == 710
    assert quote.legs[-1][0]["number_e164"] == ABROAD
    assert quote.legs[-1][0]["direction"] == "outbound"


async def test_shorter_limit_is_kept(
    client, async_session, livekit_mock, add_phone_number, quote
):
    org, key = await _org(async_session, 100_000, limit=300)
    await add_phone_number(async_session, org)
    r = await _call(client, key)
    assert r.status_code == 201, r.text
    call = await async_session.get(Call, uuid.UUID(r.json()["id"]))
    assert call.max_duration_seconds == 300
    # 300 s plus the goodbye bills 6 carrier minutes.
    assert (await _hold(async_session, call.id)).cents == Decimal(40) * 6


async def test_less_than_one_minute_is_refused(
    client, async_session, livekit_mock, add_phone_number, quote
):
    org, key = await _org(async_session, 30)
    await add_phone_number(async_session, org)
    r = await _call(client, key)
    assert r.status_code == 402
    assert "one minute" in r.json()["detail"]
    assert (await async_session.execute(select(Call))).first() is None
    livekit_mock.create_room.assert_not_awaited()


async def test_live_call_hold_counts_for_the_next_call(
    client, async_session, livekit_mock, add_phone_number, quote
):
    org, key = await _org(async_session, 500)
    await add_phone_number(async_session, org)
    first = await _call(client, key)
    assert first.status_code == 201, first.text
    # The first call set aside 480¢ of 500¢: not one more minute.
    second = await _call(client, key)
    assert second.status_code == 402


async def test_ended_call_hold_stops_counting(async_session, quote):
    org, _ = await _org(async_session, 500)
    now = datetime.now(timezone.utc)
    number = PhoneNumber(
        organization_id=org,
        e164="+14155550100",
        country_code="US",
        number_type="local",
        provider="twilio",
        provider_resource_id="PN_x",
        provisioning_state="active",
    )
    async_session.add(number)
    await async_session.flush()
    base = {
        "organization_id": org,
        "from_e164": "+14155550100",
        "to_e164": ABROAD,
        "voice_config": {},
        "direction": "inbound",
        "to_number_id": number.id,
    }
    ended = {"status": "completed", "end_reason": "normal_hangup"}
    two_min = now - timedelta(minutes=2)
    hour = now - timedelta(hours=1)
    calls = {
        "live": (Call(**base, status="in_progress"), 10),
        # Expired hold (a stuck call): not counted.
        "expired": (Call(**base, status="in_progress"), -1),
        # Ended long ago with no usage row: never billed.
        "old": (
            Call(
                **base,
                **ended,
                answered_at=now - timedelta(hours=2),
                ended_at=now - timedelta(hours=1),
            ),
            10,
        ),
        # Ended just now: the debit may not be written yet.
        "just_ended": (Call(**base, **ended, answered_at=two_min, ended_at=now), 10),
        # Never answered: never charged.
        "unanswered": (
            Call(
                **base, status="no_answer", end_reason="user_unavailable", ended_at=now
            ),
            10,
        ),
        # Completed with the answer signal missed: still billed.
        "completed": (Call(**base, **ended, ended_at=now), 10),
        # Final status but no end time: counts from its last update.
        "no_end": (Call(**base, **ended, answered_at=two_min), 10),
        "no_end_old": (
            Call(
                **base,
                **ended,
                answered_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=1),
            ),
            10,
        ),
        "debited": (Call(**base, **ended, answered_at=two_min, ended_at=now), 10),
        # Priced at 0 cents: no debit row, nothing more to come.
        "free": (Call(**base, **ended, answered_at=two_min, ended_at=now), 10),
        # Usage row not priced yet (a slow rater): counts past the grace and
        # past hold_until, up to a day after it, shrunk to the real cost:
        # 1 billed minute plus 1 spare, at 40¢.
        "unpriced": (Call(**base, **ended, answered_at=hour, ended_at=hour), -60),
        # A real cost above the hold counts the hold.
        "unpriced_long": (Call(**base, **ended, answered_at=hour, ended_at=hour), 10),
        # One handover dial at 10¢ a minute and 5¢ a call: 1 billed minute,
        # 1 spare and 1 for the dial (30), plus the fee for each line (10).
        "unpriced_dial": (Call(**base, **ended, answered_at=hour, ended_at=hour), 10),
        # A handover ran at a price Hail could not get: the full hold.
        "unpriced_unknown": (
            Call(**base, **ended, answered_at=hour, ended_at=hour),
            10,
        ),
        "unpriced_stale": (
            Call(**base, **ended, answered_at=hour, ended_at=hour),
            -2 * 24 * 60,
        ),
    }
    async_session.add_all([c for c, _ in calls.values()])
    await async_session.flush()
    prices = {
        "unpriced_dial": {
            "cents_per_minute": Decimal(10),
            "cents_per_call": Decimal(5),
            "dials": 1,
        },
        "unpriced_unknown": {"cents_per_minute": None, "cents_per_call": None},
    }
    for name, (call, minutes_left) in calls.items():
        async_session.add(
            CallBalanceHold(
                call_id=call.id,
                cents=Decimal(100),
                hold_until=now + timedelta(minutes=minutes_left),
                **prices.get(name, PRICES),
            )
        )
    usage = {
        name: UsageEvent(
            organization_id=org,
            channel="voice",
            units=600_000 if name == "unpriced_long" else 60_000,
            # Any ':suffix' after the call id matches, as the rater parses it.
            ref={
                "debited": f"{calls[name][0].id}:recording-v1",
                "unpriced": f"{calls[name][0].id}:some-later-suffix",
            }.get(name, str(calls[name][0].id)),
            priced_at=now if name in ("debited", "free") else None,
        )
        for name in (
            "debited",
            "free",
            "unpriced",
            "unpriced_long",
            "unpriced_dial",
            "unpriced_unknown",
            "unpriced_stale",
        )
    }
    async_session.add_all(usage.values())
    await async_session.flush()
    async_session.add(
        AccountCredit(
            organization_id=org,
            kind="debit",
            channel="voice",
            amount_cents=-10,
            source="usage_event_voice_inbound",
            ref=f"usage_event:{usage['debited'].id}",
        )
    )
    await async_session.commit()
    # 500 less the debit (10), the live, just-ended, completed, no-end-time,
    # long unpriced and unknown-price calls (100 each), the short unpriced
    # call (80) and the unpriced call with a handover dial (40).
    assert await available_cents(async_session, org) == Decimal(-230)


async def test_unknown_price_falls_back_to_default_limit(
    client, async_session, livekit_mock, add_phone_number, quote
):
    quote.price = None
    org, key = await _org(async_session, 500)
    await add_phone_number(async_session, org)
    r = await _call(client, key)
    assert r.status_code == 201, r.text
    call = await async_session.get(Call, uuid.UUID(r.json()["id"]))
    assert call.max_duration_seconds == settings.hail_voice_max_duration_seconds
    assert await _hold(async_session, call.id) is None


# --------------------------------------------------------------------------- #
# Handover
# --------------------------------------------------------------------------- #


async def _handover(
    client,
    async_session,
    add_phone_number,
    cents,
    answered_ago,
    hold=None,
    limit=None,
    tries=0,
):
    org = uuid.uuid4()
    if cents:
        async_session.add(
            AccountCredit(
                organization_id=org,
                kind="credit",
                channel="credit",
                amount_cents=cents,
                source="test",
                ref="test",
            )
        )
    agent = Agent(
        organization_id=org,
        name="Desk",
        system_prompt="Help.",
        handover_max_duration_seconds=limit,
    )
    contact = Contact(organization_id=org, name="Sam", phone_e164="+14155550120")
    async_session.add_all([agent, contact])
    await async_session.flush()
    async_session.add(
        AgentHandoverContact(
            agent_id=agent.id, contact_id=contact.id, note="Billing", position=0
        )
    )
    number = await add_phone_number(async_session, org)
    call = Call(
        organization_id=org,
        from_number_id=number.id,
        from_e164=number.e164,
        to_e164="+14155550199",
        status="in_progress",
        agent_id=agent.id,
        provider="twilio",
        voice_config={},
        answered_at=datetime.now(timezone.utc) - answered_ago,
        metadata_={CALL_META_BILLED: True},
    )
    async_session.add(call)
    await async_session.flush()
    if hold:
        async_session.add(
            CallBalanceHold(
                call_id=call.id, cents=hold[0], hold_until=hold[1], **PRICES
            )
        )
    if tries:
        async_session.add(
            CallBalanceHold(
                call_id=call.id,
                cents=Decimal(0),
                hold_until=datetime.now(timezone.utc),
                dials=tries,
                **PRICES,
            )
        )
    await async_session.commit()
    body = json.dumps({"call_id": str(call.id), "contact_id": str(contact.id)})
    r = await client.post(
        "/internal/agent/handover",
        content=body.encode(),
        headers={
            "Content-Type": "application/json",
            "X-Hail-Signature": hmac_signing.sign(body.encode(), SECRET),
        },
    )
    return r.json(), call


async def test_handover_is_capped_to_the_balance(
    client, async_session, add_phone_number, quote, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")
    quote.price = (Decimal(20), Decimal(0))
    data, call = await _handover(
        client, async_session, add_phone_number, 500, timedelta(seconds=90)
    )
    assert data["ok"] is True, data
    # 500¢ less 2 minutes talked and 2 to ring, at 20¢: 21 minutes left,
    # less the goodbye and the pickup check.
    assert data["max_seconds"] == 21 * 60 - 40
    assert sorted(len(legs) for legs in quote.legs[-2:]) == [1, 2]
    await async_session.refresh(call)
    assert (await _hold(async_session, call.id)).cents == Decimal(500)


async def test_handover_refused_without_one_minute(
    client, async_session, add_phone_number, quote, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")
    quote.price = (Decimal(20), Decimal(0))
    data, _ = await _handover(
        client, async_session, add_phone_number, 50, timedelta(seconds=30)
    )
    assert data["ok"] is False


async def test_handover_hold_never_shrinks(
    client, async_session, add_phone_number, quote, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")
    quote.price = (Decimal(20), Decimal(0))
    far = datetime(2999, 1, 1, tzinfo=timezone.utc)
    data, call = await _handover(
        client,
        async_session,
        add_phone_number,
        500,
        timedelta(seconds=30),
        (Decimal(900), far),
    )
    # Nothing else holds money, so the call's own hold is not counted: 500
    # less 3 minutes (talked and ring) leaves 22 minutes.
    assert data["max_seconds"] == 22 * 60 - 40
    row = await _hold(async_session, call.id)
    assert row.cents == Decimal(900)
    assert row.hold_until == far


async def test_handover_hold_grows(
    client, async_session, add_phone_number, quote, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")
    quote.price = (Decimal(20), Decimal(0))
    soon = datetime.now(timezone.utc) + timedelta(minutes=1)
    data, call = await _handover(
        client,
        async_session,
        add_phone_number,
        500,
        timedelta(seconds=30),
        (Decimal(100), soon),
    )
    assert data["ok"] is True
    row = await _hold(async_session, call.id)
    assert row.cents == Decimal(500)
    assert row.hold_until > soon + timedelta(minutes=20)


async def test_hold_prunes_expired_rows(
    client, async_session, livekit_mock, add_phone_number, quote
):
    org, key = await _org(async_session, 100_000)
    other, _ = await _org(async_session, 100_000)
    await add_phone_number(async_session, org)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    rows = {}
    for owner in (org, other):
        number = PhoneNumber(
            organization_id=owner,
            e164=f"+1415555{len(rows):04d}",
            country_code="US",
            number_type="local",
            provider="twilio",
            provider_resource_id=f"PN_{len(rows)}",
            provisioning_state="active",
        )
        async_session.add(number)
        await async_session.flush()
        call = Call(
            organization_id=owner,
            from_number_id=number.id,
            from_e164=number.e164,
            to_e164=ABROAD,
            status="completed",
            end_reason="normal_hangup",
            ended_at=old,
            voice_config={},
        )
        async_session.add(call)
        await async_session.flush()
        async_session.add(
            CallBalanceHold(
                call_id=call.id, cents=Decimal(10), hold_until=old, **PRICES
            )
        )
        rows[owner] = call.id
    await async_session.commit()
    r = await _call(client, key)
    assert r.status_code == 201, r.text
    assert await _hold(async_session, rows[org]) is None
    assert await _hold(async_session, rows[other]) is None


async def test_handover_hold_stops_at_the_agent_limit(
    client, async_session, add_phone_number, quote, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")
    quote.price = (Decimal("1.27"), Decimal("0.05"))
    data, call = await _handover(
        client, async_session, add_phone_number, 100_000, timedelta(seconds=30)
    )
    # The default handover limit is 30 minutes; the goodbye can bill a 31st.
    assert data["max_seconds"] == 30 * 60
    row = await _hold(async_session, call.id)
    spent = Decimal("1.27") * 3 + Decimal("0.05")
    assert row.cents == round_up_tenth(spent + Decimal("1.27") * 31)


async def test_handover_hold_follows_a_custom_agent_limit(
    client, async_session, add_phone_number, quote, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")
    quote.price = (Decimal(20), Decimal(0))
    data, call = await _handover(
        client,
        async_session,
        add_phone_number,
        100_000,
        timedelta(seconds=30),
        limit=120,
    )
    assert data["max_seconds"] == 120
    # 3 minutes talked and ring, then pickup, 120 s and the goodbye: 3.
    assert (await _hold(async_session, call.id)).cents == Decimal(20) * 6


async def test_handover_balance_below_agent_limit(
    client, async_session, add_phone_number, quote, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")
    quote.price = (Decimal(20), Decimal(0))
    data, _ = await _handover(
        client, async_session, add_phone_number, 100, timedelta(seconds=30), limit=600
    )
    # 100¢ less 60¢ spent: 2 minutes, well under the 10-minute limit.
    assert data["max_seconds"] == 2 * 60 - 40


async def test_handover_hold_for_an_odd_agent_limit(
    client, async_session, add_phone_number, quote, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")
    quote.price = (Decimal(20), Decimal(0))
    data, call = await _handover(
        client,
        async_session,
        add_phone_number,
        100_000,
        timedelta(seconds=30),
        limit=110,
    )
    assert data["max_seconds"] == 110
    # Pickup 30 s + 110 s + goodbye 10 s = 150 s: 3 billed minutes.
    assert (await _hold(async_session, call.id)).cents == Decimal(60 + 60)


async def test_handover_refused_when_only_seconds_are_left(
    client, async_session, add_phone_number, quote, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")
    quote.price = (Decimal(20), Decimal(0))
    # 80¢ less 60¢ spent pays 1 minute: 20 s after the pickup check, refused.
    data, _ = await _handover(
        client, async_session, add_phone_number, 80, timedelta(seconds=30)
    )
    assert data["ok"] is False


@pytest.mark.parametrize(
    ("tries", "fee", "minutes"), [(0, 0, 13), (1, 0, 12), (1, 5, 12), (1, 15, 11)]
)
async def test_handover_counts_earlier_attempts(
    client, async_session, add_phone_number, monkeypatch, tries, fee, minutes
):
    monkeypatch.setattr(settings, "hail_internal_secret", SECRET)
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")

    async def fake(org, legs, recording):
        # The call alone 10¢ a minute; both lines 20¢ plus the second
        # line's per-call fee.
        return (Decimal(10 * len(legs)), Decimal(fee * (len(legs) - 1)))

    monkeypatch.setattr("hailhq.api.routes.internal.agent.quote_call_price", fake)
    monkeypatch.setattr("hailhq.api.routes.internal.agent.quotes_enabled", lambda: True)
    # 290¢ less 3 minutes alone (30¢), the fee, and 10¢ + fee per earlier
    # dial.
    data, _ = await _handover(
        client, async_session, add_phone_number, 290, timedelta(seconds=30), tries=tries
    )
    assert data["max_seconds"] == minutes * 60 - 40


async def test_handover_counts_its_own_dial(
    client, async_session, add_phone_number, quote, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")
    quote.price = (Decimal(20), Decimal(0))
    data, call = await _handover(
        client, async_session, add_phone_number, 100_000, timedelta(seconds=30)
    )
    assert data["ok"] is True
    assert (await _hold(async_session, call.id)).dials == 1


async def test_handover_unknown_price_uses_the_agent_limit(
    client, async_session, add_phone_number, quote, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")
    quote.price = None
    data, call = await _handover(
        client, async_session, add_phone_number, 20, timedelta(seconds=30)
    )
    assert data["ok"] is True
    # No balance cap: the voicebot applies the agent's own handover limit.
    assert data["max_seconds"] is None
    # Nothing held, but the dial and the unknown price are kept, so a later
    # priced handover counts this dial and the full hold.
    row = await _hold(async_session, call.id)
    assert (row.cents, row.cents_per_minute, row.cents_per_call, row.dials) == (
        Decimal(0),
        None,
        None,
        1,
    )


async def test_handover_unknown_price_counts_the_full_hold(
    client, async_session, add_phone_number, quote, monkeypatch
):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_tw")
    quote.price = None
    until = datetime.now(timezone.utc) + timedelta(minutes=10)
    data, call = await _handover(
        client,
        async_session,
        add_phone_number,
        20,
        timedelta(seconds=30),
        hold=(Decimal(100), until),
    )
    assert data["ok"] is True
    call_id = call.id
    row = await _hold(async_session, call_id)
    await async_session.refresh(row)
    # The second line's price is unknown: once the call ends, the full hold
    # counts, and a later priced handover does not bring a price back.
    assert (row.cents, row.cents_per_minute, row.cents_per_call, row.dials) == (
        Decimal(100),
        None,
        None,
        1,
    )
    await hold(
        async_session,
        call_id,
        Decimal(50),
        until,
        cents_per_minute=Decimal(40),
        cents_per_call=Decimal(0),
    )
    await async_session.commit()
    row = await _hold(async_session, call_id)
    await async_session.refresh(row)
    assert (row.cents_per_minute, row.cents_per_call) == (None, None)


async def test_no_minute_left_is_logged(
    client, async_session, livekit_mock, add_phone_number, quote
):
    org, key = await _org(async_session, 30)
    await add_phone_number(async_session, org)
    r = await _call(client, key)
    assert r.status_code == 402
    rows = (
        (
            await async_session.execute(
                select(AuditLog).where(
                    AuditLog.organization_id == org,
                    AuditLog.action == "call.blocked",
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.payload for row in rows] == [
        {"to": ABROAD, "reason": "insufficient_funds"}
    ]


async def test_no_default_limit_still_caps_to_the_balance(
    client, async_session, livekit_mock, add_phone_number, quote, monkeypatch
):
    # HAIL_VOICE_MAX_DURATION_SECONDS=0 means no limit: the balance cap holds.
    monkeypatch.setattr(settings, "hail_voice_max_duration_seconds", 0)
    org, key = await _org(async_session, 500, limit=None)
    await add_phone_number(async_session, org)
    r = await _call(client, key)
    assert r.status_code == 201, r.text
    call = await async_session.get(Call, uuid.UUID(r.json()["id"]))
    assert call.max_duration_seconds == 710
