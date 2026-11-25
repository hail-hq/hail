"""A slow tool gets a soft sound; a quick one and call control get none."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.voicebot import busy_sound as busy_mod
from hailhq.voicebot.busy_sound import SOUND, BusySound
from hailhq.voicebot.tools import _make_handler


class Player:
    def __init__(self):
        self.start = AsyncMock()
        self.aclose = AsyncMock()
        self.handle = Mock()
        self.play = Mock(return_value=self.handle)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(busy_mod, "DELAY_SECONDS", 0.01)
    monkeypatch.setattr(busy_mod, "POLL_SECONDS", 0.01)


async def started(player, *, agent_state="thinking", can_play=lambda: True):
    sound = BusySound(player_factory=lambda: player, can_play=can_play)
    await sound.start(Mock(), Mock(agent_state=agent_state))
    return sound


async def test_a_slow_body_plays_the_sound_until_it_ends():
    player = Player()
    sound = await started(player)
    async with sound.during():
        await asyncio.sleep(0.05)
    player.play.assert_called_once_with(SOUND, loop=True)
    player.handle.stop.assert_called_once()


async def test_a_quick_body_stays_silent():
    player = Player()
    sound = await started(player)
    async with sound.during():
        pass
    player.play.assert_not_called()


async def test_never_over_the_agents_own_words():
    player = Player()
    sound = await started(player, agent_state="speaking")
    async with sound.during():
        await asyncio.sleep(0.05)
    player.play.assert_not_called()


async def test_never_during_a_handover_or_after_hangup():
    player = Player()
    sound = await started(player, can_play=lambda: False)
    async with sound.during():
        await asyncio.sleep(0.05)
    player.play.assert_not_called()


async def test_a_sound_that_cannot_start_never_breaks_the_call():
    player = Player()
    player.start.side_effect = RuntimeError("no track")
    sound = await started(player)
    # The half-started player is closed at once; the call goes on silent.
    player.aclose.assert_awaited_once()
    async with sound.during():
        await asyncio.sleep(0.05)
    player.play.assert_not_called()
    await sound.aclose()
    player.aclose.assert_awaited_once()


async def test_a_play_failure_never_breaks_the_tool():
    player = Player()
    player.play.side_effect = RuntimeError("mixer closed")
    sound = await started(player)
    async with sound.during():
        await asyncio.sleep(0.05)
    await sound.aclose()
    player.aclose.assert_awaited_once()


def _spec(risk_tier):
    async def execute(_ctx, _args):
        await asyncio.sleep(0.05)
        return "done"

    return ToolSpec(
        name="lookup",
        description="",
        parameters={"type": "object", "properties": {}},
        risk_tier=risk_tier,
        is_available=AsyncMock(return_value=True),
        execute=execute,
        waits_for_speech=False,
    )


@pytest.mark.parametrize(
    "risk_tier, plays",
    [("read_only", True), ("customer_api", True), ("session_control", False)],
)
async def test_only_lookups_and_sends_get_the_sound(risk_tier, plays):
    player = Player()
    sound = await started(player)
    tctx = ToolContext(
        call_id=None, organization_id=None, api=None, hangup=None, send_dtmf=None
    )
    handler = _make_handler(_spec(risk_tier), tctx, sound)
    assert await handler({}, Mock()) == "done"
    assert player.play.called is plays
