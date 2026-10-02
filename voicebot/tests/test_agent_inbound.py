"""Inbound rooms: static dispatch metadata, SIP attributes, refusals."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.core.inbound_calls import Accepted, Rejected, SipAttributes
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
