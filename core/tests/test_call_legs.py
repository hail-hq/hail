"""Call legs: recording them at call end, and the cost pass."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hailhq.core import call_legs, carrier_routing
from hailhq.core.call_legs import LegRecord, fetch_leg_costs, record_call_legs
from hailhq.core.models import Call, CallLeg, PhoneNumber
from hailhq.core.providers.voice.base import CarrierNotConfigured, LegCost
from sqlalchemy import select

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


async def _call(
    session, provider: str = "telnyx", e164: str = "+14155550100"
) -> uuid.UUID:
    org = uuid.uuid4()
    number = PhoneNumber(
        organization_id=org,
        e164=e164,
        country_code="US",
        number_type="local",
        provider=provider,
        provisioning_state="active",
    )
    session.add(number)
    await session.flush()
    call = Call(
        organization_id=org,
        to_number_id=number.id,
        from_e164="+33612345678",
        to_e164=e164,
        direction="inbound",
        status="completed",
        end_reason="normal_hangup",
        provider=provider,
        voice_config={},
    )
    session.add(call)
    await session.commit()
    return call.id


def _leg(**kw) -> LegRecord:
    return LegRecord(
        **{
            "role": "call",
            "direction": "inbound",
            "sip_call_id": "sip-1",
            "carrier_call_id": None,
            "to_e164": "+14155550100",
            "joined_at": NOW - timedelta(minutes=10),
            "answered_at": NOW - timedelta(minutes=10),
            "left_at": NOW - timedelta(minutes=5),
            **kw,
        }
    )


async def _legs(session) -> list[CallLeg]:
    session.expire_all()
    return list(
        (await session.execute(select(CallLeg).order_by(CallLeg.joined_at))).scalars()
    )


@pytest.fixture()
def lookups(monkeypatch):
    """Replace each carrier's lookup; returns the per-provider answers."""
    answers: dict[str, object] = {}
    seen: dict[str, list] = {}

    def fake(provider):
        async def lookup(refs):
            seen.setdefault(provider, []).extend(refs)
            answer = answers.get(provider, {})
            if isinstance(answer, Exception):
                raise answer
            return answer(refs) if callable(answer) else answer

        return lookup

    for name, entry in carrier_routing.CARRIERS.items():
        monkeypatch.setitem(
            carrier_routing.CARRIERS, name, replace(entry, leg_costs=fake(name))
        )
    return answers, seen


async def test_record_call_legs_stores_each_leg_once(async_session):
    call_id = await _call(async_session)
    legs = [
        _leg(),
        _leg(
            role="handover",
            direction="outbound",
            sip_call_id="sip-2",
            to_e164="+33698765432",
            answered_at=None,
        ),
    ]
    await record_call_legs(call_id, legs)
    await record_call_legs(call_id, legs)  # a second shutdown path
    rows = await _legs(async_session)
    assert [(r.role, r.direction, r.provider, r.sip_call_id) for r in rows] == [
        ("call", "inbound", "telnyx", "sip-1"),
        ("handover", "outbound", "telnyx", "sip-2"),
    ]
    assert {r.cost_status for r in rows} == {"pending"}
    assert rows[1].answered_at is None


async def test_record_call_legs_fails_open(async_session):
    # An unknown call writes nothing and raises nothing.
    await record_call_legs(uuid.uuid4(), [_leg()])
    assert await _legs(async_session) == []


async def test_pass_stores_found_costs(async_session, lookups):
    answers, seen = lookups
    call_id = await _call(async_session)
    await record_call_legs(call_id, [_leg(), _leg(sip_call_id="sip-2")])
    first = (await _legs(async_session))[0]
    answers["telnyx"] = {first.id: LegCost(Decimal("0.0128"), 240, Decimal("0.0032"))}

    assert await fetch_leg_costs(NOW) == 1
    found, waiting = await _legs(async_session)
    assert (found.cost_status, found.cost_usd, found.billed_seconds) == (
        "found",
        Decimal("0.0128"),
        240,
    )
    assert found.usd_per_minute == Decimal("0.0032")
    assert (waiting.cost_status, waiting.cost_checked_at) == ("pending", NOW)
    assert [r.sip_call_id for r in seen["telnyx"]] == ["sip-1", "sip-2"]


async def test_pass_waits_for_the_carrier_record(async_session, lookups):
    _, seen = lookups
    call_id = await _call(async_session)
    # Ended 1 minute ago: inside the settle window.
    await record_call_legs(call_id, [_leg(left_at=NOW - timedelta(minutes=1))])
    assert await fetch_leg_costs(NOW) == 0
    assert seen == {}


async def test_pass_rechecks_only_after_the_interval(async_session, lookups):
    _, seen = lookups
    call_id = await _call(async_session)
    await record_call_legs(call_id, [_leg()])
    await fetch_leg_costs(NOW)
    await fetch_leg_costs(NOW + timedelta(minutes=5))
    assert len(seen["telnyx"]) == 1
    await fetch_leg_costs(NOW + call_legs.RECHECK_EVERY + timedelta(seconds=1))
    assert len(seen["telnyx"]) == 2


async def test_pass_gives_up_after_the_window(async_session, lookups):
    answers, _ = lookups
    answers["telnyx"] = RuntimeError("carrier down")
    call_id = await _call(async_session)
    await record_call_legs(call_id, [_leg()])
    later = NOW + call_legs.GIVE_UP_AFTER
    await fetch_leg_costs(later)
    (leg,) = await _legs(async_session)
    assert (leg.cost_status, leg.cost_usd, leg.cost_checked_at) == (
        "missing",
        None,
        later,
    )


async def test_pass_survives_an_unconfigured_carrier(async_session, lookups):
    answers, _ = lookups
    answers["telnyx"] = CarrierNotConfigured("no key")
    call_id = await _call(async_session)
    await record_call_legs(call_id, [_leg()])
    assert await fetch_leg_costs(NOW) == 0
    (leg,) = await _legs(async_session)
    assert (leg.cost_status, leg.cost_checked_at) == ("pending", NOW)


async def test_pass_groups_legs_by_carrier(async_session, lookups):
    _, seen = lookups
    await record_call_legs(await _call(async_session, "twilio"), [_leg()])
    didww = await _call(async_session, "didww", "+14155550101")
    await record_call_legs(didww, [_leg()])
    await fetch_leg_costs(NOW)
    assert sorted(seen) == ["didww", "twilio"]
