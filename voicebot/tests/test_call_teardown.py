"""The duration ceiling must survive stalled speech and room API failures."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from hailhq.voicebot import agent
from livekit import rtc
from livekit.api import ServerError

from ._fakes import FakeAnnouncingSession, FakeJobContext

CALL_ID = UUID("11111111-2222-3333-4444-555555555555")


@pytest.mark.parametrize(
    "failure", ["transient", "missing", "stalled", "fallback", "exhausted"]
)
async def test_room_teardown_observes_api_outcome(monkeypatch, caplog, failure):
    ctx = FakeJobContext()
    monkeypatch.setattr(agent, "_ROOM_DELETE_TIMEOUT_SECONDS", 0.01, raising=False)
    unavailable = ServerError("unavailable", "room service unavailable", status=503)
    if failure == "transient":
        delete = AsyncMock(side_effect=[unavailable, None])
    elif failure == "missing":
        delete = AsyncMock(side_effect=ServerError("not_found", "gone", status=404))
    elif failure == "stalled":

        async def stall(_request):
            await asyncio.Event().wait()

        delete = AsyncMock(side_effect=stall)
    else:
        delete = AsyncMock(side_effect=unavailable)
    ctx.api.room.delete_room = delete
    if failure in ("fallback", "exhausted"):
        ctx.room.remote_participants = {
            "caller": SimpleNamespace(
                identity="caller", kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            ),
            "human": SimpleNamespace(
                identity="human", kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            ),
            "observer": SimpleNamespace(
                identity="observer", kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
            ),
        }
    if failure == "exhausted":
        ctx.api.room.remove_participant.side_effect = unavailable

    # The outer deadline makes an unbounded implementation fail promptly.
    result = await asyncio.wait_for(agent._drop_room(ctx, CALL_ID), timeout=0.5)

    expected_attempts = {"transient": 2, "missing": 1}.get(failure, 3)
    assert delete.await_count == expected_attempts
    assert all(c.args[0].room == ctx.room.name for c in delete.await_args_list)
    assert result is (failure in ("transient", "missing", "fallback"))
    removed = [c.args[0] for c in ctx.api.room.remove_participant.await_args_list]
    assert {r.identity for r in removed} == (
        {"caller", "human"} if failure in ("fallback", "exhausted") else set()
    )
    assert all(r.room == ctx.room.name for r in removed)
    if failure == "exhausted":
        assert "could not disconnect" in caplog.text


async def test_stalled_soft_cap_goodbye_cannot_keep_phone_connected(monkeypatch):
    ctx = FakeJobContext()
    ctx.api.room.delete_room = AsyncMock()
    monkeypatch.setattr(agent, "_SOFT_CAP_ANNOUNCEMENT_SECONDS", 0.01, raising=False)

    class Session(FakeAnnouncingSession):
        def say(self, *args, **kwargs):
            super().say(*args, **kwargs)
            return SimpleNamespace(
                wait_for_playout=AsyncMock(side_effect=asyncio.Event().wait)
            )

    await asyncio.wait_for(
        agent.soft_cap_announce_and_hangup(ctx, Session(), CALL_ID, delay_seconds=0),
        timeout=0.5,
    )
    ctx.api.room.delete_room.assert_awaited_once()
    assert ctx.shutdown_calls == ["soft_cap_reached"]


async def test_shutdown_joins_teardown_after_cancelling_cap():
    ctx = FakeJobContext()
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    state = {"human": None, "connected": False}

    async def delete(_request):
        entered.set()
        await release.wait()
        finished.set()

    ctx.api.room.delete_room = AsyncMock(side_effect=delete)
    cap = asyncio.create_task(
        agent.soft_cap_announce_and_hangup(
            ctx,
            FakeAnnouncingSession(),
            CALL_ID,
            delay_seconds=0,
            bridge_state=state,
        )
    )
    join = None
    try:
        await asyncio.wait_for(entered.wait(), 0.5)
        # Room disconnection starts SDK shutdown before the HTTP response.
        cap.cancel()
        await asyncio.gather(cap, return_exceptions=True)
        join = asyncio.create_task(agent.drop_handover_leg(ctx, state, CALL_ID))
        await asyncio.sleep(0)
        assert not join.done()
        release.set()
        await asyncio.wait_for(join, 0.5)
        assert finished.is_set()
        ctx.api.room.delete_room.assert_awaited_once()
    finally:
        release.set()
        cap.cancel()
        await asyncio.gather(cap, return_exceptions=True)
        if join is not None:
            await asyncio.gather(join, return_exceptions=True)


@pytest.mark.parametrize("who", ["caller", "human", "failed_bridge"])
async def test_other_end_paths_share_teardown_with_shutdown(who):
    ctx = FakeJobContext()
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    state = {"human": f"human-{CALL_ID}-1", "connected": True}

    async def delete(_request):
        entered.set()
        await release.wait()
        finished.set()

    ctx.api.room.delete_room = AsyncMock(side_effect=delete)
    ending = None
    join = None
    try:
        if who == "failed_bridge":
            state["connected"] = False
            ctx.room.remote_participants[state["human"]] = SimpleNamespace(
                identity=state["human"], kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            )
            ctx.api.room.remove_participant.side_effect = RuntimeError("remove failed")
            ending = asyncio.create_task(
                agent._remove_or_end(ctx, state, state["human"], CALL_ID)
            )
        else:
            agent.handle_sip_disconnect(
                ctx,
                SimpleNamespace(
                    identity=state["human"] if who == "human" else "caller",
                    kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
                    disconnect_reason=rtc.DisconnectReason.CLIENT_INITIATED,
                ),
                {"status": None, "end_reason": None},
                state,
                CALL_ID,
            )
        await asyncio.wait_for(entered.wait(), 0.5)
        if ending is not None:
            ending.cancel()
            await asyncio.gather(ending, return_exceptions=True)
        join = asyncio.create_task(agent.drop_handover_leg(ctx, state, CALL_ID))
        await asyncio.sleep(0)
        assert not join.done()
        release.set()
        await asyncio.wait_for(join, 0.5)
        assert finished.is_set()
        assert state["room_disconnected"] is True
        ctx.api.room.delete_room.assert_awaited_once()
    finally:
        release.set()
        if ending is not None:
            ending.cancel()
            await asyncio.gather(ending, return_exceptions=True)
        if join is not None:
            await asyncio.gather(join, return_exceptions=True)
