"""open_inbound_call: who answers, who is refused, and what gets written."""

from __future__ import annotations

import uuid

import pytest
from hailhq.core import inbound_calls
from hailhq.core.config import settings
from hailhq.core.models import (
    AccountCredit,
    Agent,
    Call,
    CallEvent,
    OrganizationCallSettings,
    PhoneNumber,
    WebhookDelivery,
    WebhookSubscription,
)
from sqlalchemy import select

DIALED = "+14155550100"
CALLER = "+33612345678"


@pytest.fixture(autouse=True)
def trunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_in_tw")
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_in_tx")
    monkeypatch.setattr(settings, "hail_voice_max_duration_seconds", 300)


@pytest.fixture(autouse=True)
def no_org_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(_org: str) -> str | None:
        return "Acme Dental"

    monkeypatch.setattr(inbound_calls, "fetch_organization_name", fake)


async def _seed(session, *, credits=10_000, agent=True, **number_kwargs):
    org = uuid.uuid4()
    if credits:
        session.add(
            AccountCredit(
                organization_id=org,
                kind="credit",
                channel="credit",
                amount_cents=credits,
                source="test",
                ref="test",
            )
        )
    agent_row = None
    if agent:
        agent_row = Agent(
            organization_id=org,
            name="Front desk",
            system_prompt="Book appointments.",
            first_message="How can I help?",
            voice_config={"voice_id": "v1", "language": "fr"},
            tools=["end_call"],
            max_duration_seconds=600,
        )
        session.add(agent_row)
        await session.flush()
    kwargs = {
        "organization_id": org,
        "e164": DIALED,
        "country_code": "US",
        "number_type": "local",
        "provider": "twilio",
        "provisioning_state": "active",
        "voice_agent_id": agent_row.id if agent_row else None,
    }
    kwargs.update(number_kwargs)
    number = PhoneNumber(**kwargs)
    session.add(number)
    session.add(
        WebhookSubscription(
            organization_id=org,
            target_url="https://example.com/hook",
            secret_encrypted="fake",
            status="active",
            event_types=["call.received", "call.failed"],
        )
    )
    await session.commit()
    return org, agent_row, number


def _attrs(**over) -> inbound_calls.SipAttributes:
    base = {
        "dialed": DIALED,
        "caller": CALLER,
        "trunk_id": "ST_in_tw",
        "room_name": "hail-in-abc",
        "provider_call_sid": "sip-call-1",
    }
    base.update(over)
    return inbound_calls.SipAttributes(**base)


async def _deliveries(session, event_type: str) -> list[WebhookDelivery]:
    rows = (
        (
            await session.execute(
                select(WebhookDelivery).where(WebhookDelivery.event_type == event_type)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def test_unknown_number_is_dropped_without_a_row(async_session) -> None:
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "unknown_number"
    assert (await async_session.execute(select(Call))).first() is None


async def test_pool_number_is_dropped(async_session) -> None:
    async_session.add(
        PhoneNumber(
            organization_id=None,
            is_pool=True,
            e164=DIALED,
            country_code="US",
            number_type="local",
            provisioning_state="active",
        )
    )
    await async_session.commit()
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "unknown_number"


async def test_wrong_trunk_for_the_carrier_writes_a_failed_call(
    async_session, caplog
) -> None:
    await _seed(async_session)
    with caplog.at_level("WARNING", logger="hailhq.core.inbound_calls"):
        outcome = await inbound_calls.open_inbound_call(
            async_session, _attrs(trunk_id="ST_in_tx")
        )
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "carrier_route_failed"
    call = (await async_session.execute(select(Call))).scalar_one()
    assert call.id == outcome.call_id
    assert call.status == "failed"
    assert call.end_reason == "carrier_route_failed"
    assert call.direction == "inbound"
    event = (await async_session.execute(select(CallEvent))).scalar_one()
    assert event.payload["reason"] == "carrier_route_failed"
    assert any(r.levelname == "WARNING" for r in caplog.records)


async def test_number_without_voice_writes_a_failed_call(async_session) -> None:
    _org, _agent, number = await _seed(async_session)
    number.capabilities = ["sms"]
    await async_session.commit()
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "carrier_route_failed"
    assert (await async_session.execute(select(Call))).scalar_one().status == "failed"


async def test_shared_trunk_serves_every_carrier(async_session, monkeypatch) -> None:
    """One LiveKit inbound trunk for all carriers (what LiveKit allows for a
    wildcard trunk): a DIDWW number on the shared trunk is answered."""
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_shared")
    monkeypatch.setattr(settings, "livekit_didww_sip_inbound_trunk_id", "ST_shared")
    await _seed(async_session, provider="didww", e164="+351300509184")
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(dialed="+351300509184", trunk_id="ST_shared")
    )
    assert isinstance(outcome, inbound_calls.Accepted)


async def test_missing_trunk_id_is_refused(async_session) -> None:
    await _seed(async_session)
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs(trunk_id=""))
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "carrier_route_failed"


async def test_no_agent_writes_a_failed_call(async_session) -> None:
    _org, _agent, _number = await _seed(async_session, agent=False)
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "no_agent"
    call = (await async_session.execute(select(Call))).scalar_one()
    assert call.direction == "inbound"
    assert call.status == "failed"
    assert call.end_reason == "no_agent"
    assert call.to_number_id == _number.id
    assert call.from_number_id is None
    assert call.from_e164 == CALLER and call.to_e164 == DIALED
    assert len(await _deliveries(async_session, "call.failed")) == 1
    assert await _deliveries(async_session, "call.received") == []


async def test_paused_agent_counts_as_no_agent(async_session) -> None:
    _org, agent, _number = await _seed(async_session)
    agent.status = "paused"
    await async_session.commit()
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "no_agent"


async def test_no_funds_writes_insufficient_funds(async_session) -> None:
    await _seed(async_session, credits=0)
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "insufficient_funds"
    call = (await async_session.execute(select(Call))).scalar_one()
    assert call.status == "failed"
    assert call.end_reason == "insufficient_funds"
    deliveries = await _deliveries(async_session, "call.failed")
    assert deliveries[0].payload["data"]["end_reason"] == "insufficient_funds"
    assert deliveries[0].payload["data"]["direction"] == "inbound"


async def test_accepted_call_rings_and_returns_dispatch_metadata(async_session) -> None:
    org, agent, number = await _seed(async_session)
    async_session.add(
        OrganizationCallSettings(
            organization_id=org,
            max_duration_seconds=120,
            ai_disclosure_line="You reached {org}. I am an AI.",
        )
    )
    await async_session.commit()

    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())

    assert isinstance(outcome, inbound_calls.Accepted)
    call = (await async_session.execute(select(Call))).scalar_one()
    assert call.status == "ringing"
    assert call.direction == "inbound"
    assert call.agent_id == agent.id
    assert call.to_number_id == number.id
    assert call.provider == "twilio"
    assert call.provider_call_sid == "sip-call-1"
    assert call.livekit_room == "hail-in-abc"
    assert call.started_at is not None
    assert call.max_duration_seconds == 600  # the agent's cap wins over the workspace
    assert call.metadata_ == {"billed": True}
    assert call.voice_config == {"voice_id": "v1", "language": "fr"}

    events = (await async_session.execute(select(CallEvent))).scalars().all()
    assert [e.payload for e in events] == [{"from": "queued", "to": "ringing"}]
    received = await _deliveries(async_session, "call.received")
    assert len(received) == 1
    assert received[0].payload["data"] == {
        "id": str(call.id),
        "status": "ringing",
        "direction": "inbound",
        "from": CALLER,
        "to": DIALED,
        "agent_id": str(agent.id),
    }

    md = outcome.metadata
    assert md["call_id"] == call.id
    assert md["organization_id"] == str(org)
    assert md["direction"] == "inbound"
    assert md["system_prompt"] == "Book appointments."
    assert md["first_message"] == "How can I help?"
    assert md["ai_disclosure"] is True
    assert md["ai_disclosure_line"] == "You reached {org}. I am an AI."
    assert md["org_name"] == "Acme Dental"
    assert md["tools"] == ["end_call"]
    assert md["max_duration_seconds"] == 600
    assert md["voice_config"] == {"voice_id": "v1", "language": "fr"}
    assert md["llm"] is None


async def test_agent_line_beats_workspace_line_and_workspace_cap_applies(
    async_session,
) -> None:
    org, agent, _number = await _seed(async_session)
    agent.ai_disclosure_line = "Agent line for {org}."
    agent.max_duration_seconds = None
    async_session.add(
        OrganizationCallSettings(
            organization_id=org, max_duration_seconds=120, ai_disclosure_line="WS."
        )
    )
    await async_session.commit()
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Accepted)
    assert outcome.metadata["ai_disclosure_line"] == "Agent line for {org}."
    assert outcome.metadata["max_duration_seconds"] == 120


async def test_service_default_cap_when_nothing_is_set(async_session) -> None:
    _org, agent, _number = await _seed(async_session)
    agent.max_duration_seconds = None
    await async_session.commit()
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Accepted)
    assert outcome.metadata["max_duration_seconds"] == 300
    assert outcome.metadata["ai_disclosure_line"] is None
