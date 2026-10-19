"""A noisy VAD cannot keep a conversation stalled forever."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hailhq.voicebot.recovery import ConversationRecovery


class Session:
    def __init__(self):
        self.agent_state = "listening"
        self.handlers = {}
        self.clear_user_turn = Mock()
        self.generate_reply = Mock()

    def on(self, event):
        def register(callback):
            self.handlers.setdefault(event, []).append(callback)
            return callback

        return register

    def emit(self, event, **kwargs):
        for callback in self.handlers.get(event, []):
            callback(SimpleNamespace(**kwargs))


@pytest.fixture
def recovery():
    session = Session()
    now = [0.0]
    hangup = AsyncMock()
    permitted = [True]
    watcher = ConversationRecovery(
        session,
        on_exhausted=hangup,
        can_recover=lambda: permitted[0],
        clock=lambda: now[0],
    )
    return watcher, session, now, hangup, permitted


async def test_continuous_vad_without_transcription_gets_one_recovery_then_ends(
    recovery,
):
    watcher, session, now, hangup, _ = recovery
    now[0] = 19
    await watcher.check()
    session.generate_reply.assert_not_called()
    now[0] = 20
    session.emit("user_state_changed", new_state="speaking")
    await watcher.check()
    session.clear_user_turn.assert_called_once()
    session.generate_reply.assert_called_once()
    assert session.generate_reply.call_args.kwargs["allow_interruptions"] is False
    assert session.generate_reply.call_args.kwargs["tool_choice"] == "none"
    hangup.assert_not_awaited()
    now[0] = 42
    await watcher.check()
    hangup.assert_awaited_once()
    await watcher.check()
    hangup.assert_awaited_once()


async def test_interim_transcription_protects_long_utterances(recovery):
    watcher, session, now, hangup, _ = recovery
    for tick in range(10, 100, 10):
        now[0] = tick
        session.emit(
            "user_input_transcribed", transcript="still speaking", is_final=False
        )
        await watcher.check()
    session.generate_reply.assert_not_called()
    hangup.assert_not_awaited()


async def test_transcription_after_recovery_rearms_the_budget(recovery):
    watcher, session, now, hangup, _ = recovery
    now[0] = 21
    await watcher.check()
    now[0] = 30
    session.emit("user_input_transcribed", transcript="Hello?", is_final=True)
    now[0] = 51
    await watcher.check()
    assert session.generate_reply.call_count == 2
    hangup.assert_not_awaited()


async def test_interim_after_recovery_extends_listening_without_another_prompt(
    recovery,
):
    watcher, session, now, hangup, _ = recovery
    now[0] = 20
    await watcher.check()
    now[0] = 30
    session.emit("user_input_transcribed", transcript="partial speech", is_final=False)
    now[0] = 49
    await watcher.check()
    hangup.assert_not_awaited()
    session.generate_reply.assert_called_once()
    now[0] = 50
    await watcher.check()
    hangup.assert_awaited_once()
    session.generate_reply.assert_called_once()


@pytest.mark.parametrize("state", ["speaking", "thinking"])
async def test_does_not_interrupt_agent_speech_or_tools(recovery, state):
    watcher, session, now, hangup, _ = recovery
    session.agent_state = state
    now[0] = 60
    await watcher.check()
    session.generate_reply.assert_not_called()
    hangup.assert_not_awaited()


async def test_disabled_during_ivr_handover_or_shutdown(recovery):
    watcher, session, now, hangup, permitted = recovery
    permitted[0] = False
    now[0] = 60
    await watcher.check()
    session.generate_reply.assert_not_called()
    hangup.assert_not_awaited()


async def test_empty_transcripts_do_not_mask_a_stall(recovery):
    watcher, session, now, _, _ = recovery
    now[0] = 21
    session.emit("user_input_transcribed", transcript="  ", is_final=True)
    await watcher.check()
    session.generate_reply.assert_called_once()
