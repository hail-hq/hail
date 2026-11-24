"""Ownership check of a connected number: the outbound leg sends the code."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hailhq.core.agent_tools.send_dtmf import DIGIT_DELAY_SECONDS
from hailhq.core.models import Call, CallEvent, PhoneNumber
from hailhq.core.sip_connections import CHECK_EVENT_KIND
from hailhq.voicebot import agent
from hailhq.voicebot.agent import (
    CHECK_SEND_DELAY_SECONDS,
    CHECK_SEND_GAP_SECONDS,
    check_arrival,
    entrypoint,
    send_check_code,
)
from livekit import rtc

CALL_ID = "00000000-0000-0000-0000-00000000c4ec"


class _Room:
    def __init__(self, status: str = "active") -> None:
        self.name = f"hail-{CALL_ID}"
        self.remote_participants = {
            "caller": SimpleNamespace(
                kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
                identity=f"pbx-check-{CALL_ID}",
                attributes={"sip.callStatus": status},
            )
        }
        self.handlers: dict[str, list] = {}
        self.local_participant = SimpleNamespace(publish_dtmf=AsyncMock())

    def on(self, event, handler=None):
        self.handlers.setdefault(event, []).append(handler)
        return handler

    def off(self, event, handler):
        self.handlers[event].remove(handler)


@pytest.fixture()
def check_ctx():
    return SimpleNamespace(
        room=_Room(),
        job=SimpleNamespace(metadata="{}"),
        connect=AsyncMock(),
        delete_room=AsyncMock(),
        shutdown=Mock(),
    )


@pytest.fixture()
def arrived(monkeypatch):
    seen = AsyncMock(return_value=True)
    monkeypatch.setattr(agent, "check_arrival", seen)
    return seen


@pytest.fixture()
def steps(check_ctx, monkeypatch):
    """Every ``asyncio.sleep`` and every key press, in order."""
    seen: list[tuple[str, object]] = []
    real = asyncio.sleep

    async def _sleep(seconds, *args, **kwargs):
        seen.append(("sleep", seconds))
        return await real(0)

    async def _publish(*, code, digit):
        seen.append(("dtmf", digit))

    monkeypatch.setattr(agent.asyncio, "sleep", _sleep)
    check_ctx.room.local_participant.publish_dtmf.side_effect = _publish
    return seen


def _one_try(code: str) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    for i, digit in enumerate(code):
        if i:
            out.append(("sleep", DIGIT_DELAY_SECONDS))
        out.append(("dtmf", digit))
    return out


async def test_check_sender_sends_code_twice_one_second_apart(
    check_ctx, steps, arrived
):
    await send_check_code(check_ctx, uuid.UUID(CALL_ID), "482913")
    assert steps == [
        ("sleep", CHECK_SEND_DELAY_SECONDS),
        *_one_try("482913"),
        ("sleep", CHECK_SEND_GAP_SECONDS),
        *_one_try("482913"),
    ]
    assert CHECK_SEND_DELAY_SECONDS == 1
    assert CHECK_SEND_GAP_SECONDS == 1
    arrived.assert_awaited_once_with(uuid.UUID(CALL_ID))
    calls = check_ctx.room.local_participant.publish_dtmf.await_args_list
    assert [c.kwargs["code"] for c in calls] == [4, 8, 2, 9, 1, 3] * 2


async def test_check_sender_waits_for_the_answer(check_ctx, steps, arrived):
    check_ctx.room = _Room(status="ringing")
    check_ctx.room.local_participant.publish_dtmf.side_effect = (
        lambda *, code, digit: steps.append(("dtmf", digit))
    )
    task = asyncio.ensure_future(
        send_check_code(check_ctx, uuid.UUID(CALL_ID), "482913")
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert not [s for s in steps if s[0] == "dtmf"]
    arrived.assert_not_awaited()
    participant = check_ctx.room.remote_participants["caller"]
    participant.attributes["sip.callStatus"] = "active"
    for handler in list(check_ctx.room.handlers["participant_attributes_changed"]):
        handler({"sip.callStatus": "active"}, participant)
    await task
    assert [s[1] for s in steps if s[0] == "dtmf"] == list("482913482913")
    assert check_ctx.room.handlers["participant_attributes_changed"] == []


async def test_check_sender_sends_nothing_without_an_answer(
    check_ctx, steps, arrived, monkeypatch
):
    monkeypatch.setattr(agent, "CHECK_ANSWER_WAIT_SECONDS", 0.01)
    check_ctx.room = _Room(status="ringing")
    await send_check_code(check_ctx, uuid.UUID(CALL_ID), "482913")
    check_ctx.room.local_participant.publish_dtmf.assert_not_awaited()
    arrived.assert_not_awaited()


async def test_check_sender_sends_nothing_unless_arrived(check_ctx, steps, arrived):
    arrived.return_value = False
    await send_check_code(check_ctx, uuid.UUID(CALL_ID), "482913")
    assert steps == []
    check_ctx.room.local_participant.publish_dtmf.assert_not_awaited()


async def test_check_sender_never_raises(check_ctx, steps, arrived):
    check_ctx.room.local_participant.publish_dtmf.side_effect = RuntimeError("closed")
    assert await send_check_code(check_ctx, uuid.UUID(CALL_ID), "482913") is None


async def test_check_sender_never_logs_the_code(check_ctx, steps, arrived, caplog):
    with caplog.at_level(logging.DEBUG):
        await send_check_code(check_ctx, uuid.UUID(CALL_ID), "482913")
        check_ctx.room.local_participant.publish_dtmf.side_effect = RuntimeError(
            "closed"
        )
        await send_check_code(check_ctx, uuid.UUID(CALL_ID), "482913")
    records = [r for r in caplog.records if r.name.startswith("hailhq")]
    assert records  # the failure is logged
    for record in records:
        text = record.getMessage() + (record.exc_text or "")
        assert "482913" not in text
        assert "digit" not in text


async def test_check_sender_leaves_the_room_to_the_api(check_ctx, steps, arrived):
    await send_check_code(check_ctx, uuid.UUID(CALL_ID), "482913")
    check_ctx.delete_room.assert_not_awaited()
    check_ctx.shutdown.assert_not_called()


async def test_check_dispatch_runs_only_the_sender(check_ctx, monkeypatch):
    check_ctx.job.metadata = json.dumps(
        {"call_id": CALL_ID, "pbx_check_send": "482913"}
    )
    sender, session_cls = AsyncMock(), Mock()
    org_configs = AsyncMock()
    monkeypatch.setattr(agent, "send_check_code", sender)
    monkeypatch.setattr(agent, "AgentSession", session_cls)
    monkeypatch.setattr(agent, "resolve_org_configs", org_configs)
    await entrypoint(check_ctx)
    check_ctx.connect.assert_awaited_once()
    sender.assert_awaited_once_with(check_ctx, uuid.UUID(CALL_ID), "482913")
    session_cls.assert_not_called()
    org_configs.assert_not_awaited()


async def _check_call(session, pool_e164="+14155550199") -> uuid.UUID:
    pool = PhoneNumber(
        organization_id=None,
        e164=pool_e164,
        country_code="US",
        number_type="local",
        capabilities=["voice"],
        provider="twilio",
        provider_resource_id="PN_pool",
        provisioning_state="active",
        is_pool=True,
    )
    session.add(pool)
    await session.flush()
    call = Call(
        organization_id=uuid.uuid4(),
        from_number_id=pool.id,
        from_e164=pool_e164,
        to_e164="+14155550100",
        direction="outbound",
        status="ringing",
        voice_config={},
        max_duration_seconds=90,
    )
    session.add(call)
    await session.commit()
    return call.id


async def _event(session, call_id, outcome) -> None:
    session.add(
        CallEvent(call_id=call_id, kind=CHECK_EVENT_KIND, payload={"outcome": outcome})
    )
    await session.commit()


async def test_check_arrival_sees_arrived(async_session):
    call_id = await _check_call(async_session)
    await _event(async_session, call_id, "arrived")
    assert await check_arrival(call_id, seconds=0.5) is True


async def test_check_arrival_sees_a_late_arrival(async_session):
    call_id = await _check_call(async_session)

    async def _later():
        await asyncio.sleep(0.1)
        await _event(async_session, call_id, "arrived")

    task = asyncio.create_task(_later())
    assert await check_arrival(call_id, seconds=2) is True
    await task


async def test_check_arrival_refuses_wrong_trunk(async_session):
    call_id = await _check_call(async_session)
    await _event(async_session, call_id, "not_authenticated")
    assert await check_arrival(call_id, seconds=0.5) is False


async def test_check_arrival_gives_up(async_session):
    call_id = await _check_call(async_session)
    assert await check_arrival(call_id, seconds=0.05) is False


async def test_check_arrival_ignores_other_calls(async_session):
    call_id = await _check_call(async_session)
    other = await _check_call(async_session, "+14155550198")
    await _event(async_session, other, "arrived")
    assert await check_arrival(call_id, seconds=0.05) is False
