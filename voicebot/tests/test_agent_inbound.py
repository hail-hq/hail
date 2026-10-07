"""Inbound rooms: static dispatch metadata, SIP attributes, refusals."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.core.inbound_calls import Accepted, Rejected, SipAttributes
from hailhq.core.models import Agent, Call, PhoneNumber, Sms
from hailhq.voicebot import agent as agent_mod
from hailhq.voicebot.agent import parse_metadata
from livekit import rtc


def test_parse_metadata_accepts_inbound_without_call_id() -> None:
    md = parse_metadata(json.dumps({"direction": "inbound"}))
    assert md == {"direction": "inbound"}
    with pytest.raises(ValueError, match="call_id"):
        parse_metadata(json.dumps({"direction": "outbound"}))


class _SipParticipant:
    kind = rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    identity = "sip_+33612345678"

    def __init__(self, attributes: dict[str, str]) -> None:
        self.attributes = attributes


class _Ctx:
    def __init__(self, participant: _SipParticipant | None) -> None:
        self.room = SimpleNamespace(name="hail-in-abc")
        self._participant = participant
        self.shutdown_calls: list[str] = []
        self.delete_room_calls = 0

    async def wait_for_participant(self, *, kind=None):
        if self._participant is None:
            await asyncio.sleep(3600)
        return self._participant

    async def delete_room(self) -> None:
        self.delete_room_calls += 1

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_calls.append(reason)


_ATTRS = {
    "sip.trunkPhoneNumber": "+14155550100",
    "sip.phoneNumber": "+33612345678",
    "sip.trunkID": "ST_in_tw",
    "sip.callIDFull": "abc@sip",
    "sip.callStatus": "active",
}


@pytest.fixture()
def no_db(monkeypatch: pytest.MonkeyPatch):
    class _Scope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(agent_mod, "session_scope", lambda: _Scope())


async def test_accepted_returns_metadata_from_sip_attributes(
    monkeypatch: pytest.MonkeyPatch, no_db
) -> None:
    seen: list[SipAttributes] = []
    call_id = uuid.uuid4()

    async def fake_open(_db, attrs):
        seen.append(attrs)
        return Accepted({"call_id": call_id, "direction": "inbound"})

    monkeypatch.setattr(agent_mod, "open_inbound_call", fake_open)
    ctx = _Ctx(_SipParticipant(_ATTRS))

    md = await agent_mod.open_inbound_from_room(ctx)  # type: ignore[arg-type]

    assert md == {"call_id": call_id, "direction": "inbound"}
    assert seen == [
        SipAttributes(
            dialed="+14155550100",
            caller="+33612345678",
            trunk_id="ST_in_tw",
            room_name="hail-in-abc",
            provider_call_sid="abc@sip",
        )
    ]
    assert ctx.delete_room_calls == 0
    assert ctx.shutdown_calls == []


async def test_rejected_deletes_room_and_exits(
    monkeypatch: pytest.MonkeyPatch, no_db
) -> None:
    monkeypatch.setattr(
        agent_mod, "open_inbound_call", AsyncMock(return_value=Rejected("no_agent"))
    )
    ctx = _Ctx(_SipParticipant(_ATTRS))

    assert await agent_mod.open_inbound_from_room(ctx) is None  # type: ignore[arg-type]
    assert ctx.delete_room_calls == 1
    assert ctx.shutdown_calls == ["no_agent"]


async def test_db_error_is_a_refusal_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, no_db
) -> None:
    monkeypatch.setattr(
        agent_mod, "open_inbound_call", AsyncMock(side_effect=RuntimeError("db down"))
    )
    ctx = _Ctx(_SipParticipant(_ATTRS))
    assert await agent_mod.open_inbound_from_room(ctx) is None  # type: ignore[arg-type]
    assert ctx.delete_room_calls == 1
    assert ctx.shutdown_calls == ["error"]


async def test_missing_participant_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_mod, "INBOUND_PARTICIPANT_TIMEOUT_SECONDS", 0.01)
    ctx = _Ctx(None)
    assert await agent_mod.open_inbound_from_room(ctx) is None  # type: ignore[arg-type]
    assert ctx.shutdown_calls == ["no_sip_participant"]


async def test_entrypoint_exits_after_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared session code never runs for a refused inbound call."""
    monkeypatch.setattr(
        agent_mod, "open_inbound_from_room", AsyncMock(return_value=None)
    )
    started = AsyncMock()
    monkeypatch.setattr(agent_mod, "resolve_org_configs", started)

    class _Job:
        metadata = json.dumps({"direction": "inbound"})

    ctx = SimpleNamespace(
        job=_Job(), connect=AsyncMock(), room=SimpleNamespace(name="r")
    )
    await agent_mod.entrypoint(ctx)  # type: ignore[arg-type]
    started.assert_not_awaited()


async def test_ringing_inbound_call_is_answered_once(async_session) -> None:
    """A duplicate attribute event must not write a second call.answered."""
    from hailhq.core.models import (
        Call,
        CallEvent,
        PhoneNumber,
        WebhookDelivery,
        WebhookSubscription,
    )
    from hailhq.voicebot.agent import mark_call_answered
    from sqlalchemy import select

    org = uuid.uuid4()
    pn = PhoneNumber(
        organization_id=org,
        e164="+14155550100",
        country_code="US",
        number_type="local",
        provisioning_state="active",
    )
    async_session.add(pn)
    await async_session.flush()
    call = Call(
        organization_id=org,
        to_number_id=pn.id,
        from_e164="+33612345678",
        to_e164="+14155550100",
        direction="inbound",
        status="ringing",
        voice_config={},
    )
    async_session.add(call)
    async_session.add(
        WebhookSubscription(
            organization_id=org,
            target_url="https://example.com/hook",
            secret_encrypted="x",
            status="active",
            event_types=["call.answered"],
        )
    )
    await async_session.commit()
    call_id = call.id

    assert await mark_call_answered(call_id) is True
    assert await mark_call_answered(call_id) is False

    async_session.expire_all()
    row = await async_session.get(Call, call_id)
    assert row.status == "in_progress" and row.answered_at is not None
    events = (
        (
            await async_session.execute(
                select(CallEvent).where(CallEvent.call_id == call_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].payload == {"from": "ringing", "to": "in_progress"}
    deliveries = (await async_session.execute(select(WebhookDelivery))).scalars().all()
    assert len(deliveries) == 1
    assert deliveries[0].payload["data"]["direction"] == "inbound"
    assert deliveries[0].payload["data"]["from"] == "+33612345678"


async def test_numbers_without_plus_are_normalised(
    monkeypatch: pytest.MonkeyPatch, no_db
) -> None:
    seen: list[SipAttributes] = []

    async def fake_open(_db, attrs):
        seen.append(attrs)
        return Rejected("unknown_number")

    monkeypatch.setattr(agent_mod, "open_inbound_call", fake_open)
    attrs = {
        **_ATTRS,
        "sip.trunkPhoneNumber": "351300509184",
        "sip.phoneNumber": "33612345678",
    }
    await agent_mod.open_inbound_from_room(_Ctx(_SipParticipant(attrs)))  # type: ignore[arg-type]
    assert seen[0].dialed == "+351300509184"
    assert seen[0].caller == "+33612345678"


async def test_caller_who_left_before_the_agent_joined_closes_the_row(
    async_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inbound: the room is empty once the Call row exists → the row is
    closed as canceled and no session starts."""
    from hailhq.core.models import Call, PhoneNumber

    org = uuid.uuid4()
    pn = PhoneNumber(
        organization_id=org,
        e164="+14155550100",
        country_code="US",
        number_type="local",
        provisioning_state="active",
    )
    async_session.add(pn)
    await async_session.flush()
    call = Call(
        organization_id=org,
        to_number_id=pn.id,
        from_e164="+33612345678",
        to_e164=pn.e164,
        direction="inbound",
        status="ringing",
        voice_config={},
    )
    async_session.add(call)
    await async_session.commit()
    call_id = call.id

    monkeypatch.setattr(
        agent_mod,
        "open_inbound_from_room",
        AsyncMock(
            return_value={
                "call_id": call_id,
                "direction": "inbound",
                "organization_id": str(org),
            }
        ),
    )
    started = AsyncMock()
    monkeypatch.setattr(agent_mod, "resolve_org_configs", started)

    class _Room:
        name = "hail-in-x"

        def __init__(self) -> None:
            self.remote_participants: dict = {}

        def on(self, _event):
            return lambda fn: fn

    shutdowns: list[str] = []
    ctx = SimpleNamespace(
        job=SimpleNamespace(metadata=json.dumps({"direction": "inbound"})),
        connect=AsyncMock(),
        room=_Room(),
        shutdown=lambda reason="": shutdowns.append(reason),
        proc=SimpleNamespace(userdata={"vad": object()}),
    )
    await agent_mod.entrypoint(ctx)  # type: ignore[arg-type]

    assert shutdowns == ["caller_left"]
    started.assert_not_awaited()
    async_session.expire_all()
    row = await async_session.get(Call, call_id)
    assert row.status == "canceled"
    assert row.end_reason == "normal_hangup"


def test_inbound_instructions_say_the_agent_answers_the_call() -> None:
    """The outbound preamble tells the model it placed the call and must say it
    is "calling on someone's behalf"; an inbound caller must not hear that."""
    from hailhq.voicebot.agent import (
        VOICE_PREAMBLE,
        VOICE_PREAMBLE_INBOUND,
        build_instructions,
    )

    assert "placing the call" in VOICE_PREAMBLE
    assert "placing the call" not in VOICE_PREAMBLE_INBOUND
    assert "calling on someone's behalf" not in VOICE_PREAMBLE_INBOUND
    assert "answering the call" in VOICE_PREAMBLE_INBOUND
    assert "answering on someone's behalf" in VOICE_PREAMBLE_INBOUND
    assert build_instructions("Book it.", "inbound").startswith(VOICE_PREAMBLE_INBOUND)
    assert build_instructions("Book it.", "outbound").startswith(VOICE_PREAMBLE)
    assert build_instructions("Book it.").startswith(VOICE_PREAMBLE)
    assert build_instructions(None, "inbound") == VOICE_PREAMBLE_INBOUND


async def test_inbound_without_first_message_waits_for_the_caller() -> None:
    """No generated "say why you are calling" opening on a call the person
    placed: the AI line, then the agent waits (``first_message: null``)."""
    from hailhq.voicebot.agent import speak_greeting

    from ._fakes import FakeAnnouncingSession

    session = FakeAnnouncingSession()
    await speak_greeting(session, {"direction": "inbound", "org_name": "Acme"})
    assert session.say_calls == [
        ("Hi, this is an AI assistant answering on behalf of Acme.", True)
    ]
    assert session.generate_reply_calls == []

    session = FakeAnnouncingSession()
    await speak_greeting(session, {"direction": "inbound", "ai_disclosure": False})
    assert session.say_calls == []
    assert session.generate_reply_calls == []


async def test_load_thread_context_renders_the_callers_texts(async_session) -> None:
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="a", system_prompt="x")
    async_session.add(agent)
    number = PhoneNumber(
        organization_id=org,
        e164="+14155550100",
        country_code="US",
        number_type="local",
        provider="twilio",
        provisioning_state="active",
    )
    async_session.add(number)
    await async_session.flush()
    call = Call(
        organization_id=org,
        agent_id=agent.id,
        to_number_id=number.id,
        voice_config={},
        from_e164="+33612345678",
        to_e164="+14155550100",
        direction="inbound",
        status="in_progress",
        provider="twilio",
    )
    async_session.add(call)
    async_session.add(
        Sms(
            organization_id=org,
            agent_id=agent.id,
            provider="twilio",
            from_e164="+33612345678",
            to_e164="+14155550100",
            direction="inbound",
            status="received",
            body="my order is 4411",
            requested_at=datetime.now(timezone.utc),
        )
    )
    await async_session.commit()

    text = await agent_mod.load_thread_context(call.id)

    assert text is not None and "my order is 4411" in text
    assert "text from caller" in text


async def test_load_thread_context_is_none_without_an_agent(async_session) -> None:
    assert await agent_mod.load_thread_context(uuid.uuid4()) is None


async def test_load_thread_context_is_none_when_the_read_fails(monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(agent_mod.threads, "call_thread_key", boom)
    assert await agent_mod.load_thread_context(uuid.uuid4()) is None


async def test_load_thread_context_is_none_when_the_read_is_slow(
    async_session, monkeypatch
) -> None:
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    async def key(*args, **kwargs):
        return uuid.uuid4(), uuid.uuid4(), "+33612345678"

    monkeypatch.setattr(agent_mod.threads, "call_thread_key", key)
    monkeypatch.setattr(agent_mod.threads, "thread_items", slow)
    monkeypatch.setattr(agent_mod, "HISTORY_TIMEOUT_SECONDS", 0.05)

    start = time.monotonic()
    assert await agent_mod.load_thread_context(uuid.uuid4()) is None
    assert time.monotonic() - start < 2
