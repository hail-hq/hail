"""The agent stays quiet on hold, and only speaks up for a reason."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hailhq.core.agent_tools.spec import ToolContext
from hailhq.core.agent_tools.wait_for_other_party import SPEC as WAIT_SPEC
from hailhq.voicebot import hold as hold_mod
from hailhq.voicebot.agent import SpeechSanitizingAgent
from hailhq.voicebot.hold import (
    CANT_HEAR,
    CHECK_IN,
    HOLD_ENDED_NOTE,
    HOLD_SECONDS,
    MUSIC_SECONDS,
    Hold,
)
from hailhq.voicebot.recovery import ConversationRecovery
from livekit.agents import StopResponse
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.agents.voice.amd import AMDCategory


class Session:
    def __init__(self):
        self.agent_state = "listening"
        self.handlers = {}
        self.generate_reply = Mock()
        self.clear_user_turn = Mock()

    def on(self, event):
        def register(callback):
            self.handlers.setdefault(event, []).append(callback)
            return callback

        return register

    def emit(self, event, **kwargs):
        for callback in self.handlers.get(event, []):
            callback(SimpleNamespace(**kwargs))


@pytest.fixture
def line(monkeypatch):
    monkeypatch.setattr(hold_mod, "TRANSCRIPT_GRACE_SECONDS", 0)
    session = Session()
    now = [0.0]
    classifier = Mock()
    held = Hold(session, classifier=lambda: classifier, clock=lambda: now[0])
    return held, session, now, classifier


def speak(session, now, seconds, *, words=""):
    session.emit("user_state_changed", new_state="speaking")
    if words:
        session.emit("user_input_transcribed", transcript=words, is_final=True)
    now[0] += seconds
    session.emit("user_state_changed", new_state="listening")


async def test_wait_tool_starts_the_hold_and_asks_for_no_reply():
    started = AsyncMock()
    ctx = ToolContext(
        call_id=None, organization_id=None, api=None, hangup=None, send_dtmf=None
    )
    ctx.hold = started
    assert await WAIT_SPEC.execute(ctx, {}) is None
    started.assert_awaited_once()
    assert WAIT_SPEC.on_every_call
    assert not WAIT_SPEC.waits_for_speech


async def test_silence_checks_in_once_after_the_hold_length(line):
    held, session, now, _ = line
    await held.start()
    now[0] = HOLD_SECONDS - 1
    held.check()
    session.generate_reply.assert_not_called()
    assert held.active
    now[0] = HOLD_SECONDS
    held.check()
    session.generate_reply.assert_called_once_with(
        instructions=CHECK_IN, tool_choice="none"
    )
    assert not held.active
    now[0] = HOLD_SECONDS * 3
    held.check()
    session.generate_reply.assert_called_once()
    await held.aclose()


async def test_music_keeps_the_hold_and_restarts_the_silence_clock(line):
    held, session, now, _ = line
    await held.start()
    now[0] = HOLD_SECONDS - 5
    speak(session, now, MUSIC_SECONDS)
    # Later short wordless bursts in the same hold are music too.
    speak(session, now, 3)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert held.active
    now[0] += HOLD_SECONDS - 1
    held.check()
    session.generate_reply.assert_not_called()
    await held.aclose()


async def test_a_voice_without_words_ends_the_hold(line):
    held, session, now, _ = line
    await held.start()
    speak(session, now, 3)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not held.active
    session.generate_reply.assert_called_once_with(
        instructions=CANT_HEAR, tool_choice="none"
    )
    await held.aclose()


async def test_short_noise_and_words_do_not_trigger_the_cant_hear_line(line):
    held, session, now, _ = line
    await held.start()
    speak(session, now, 1)  # a click or a cough
    speak(session, now, 3, words="one second")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    session.generate_reply.assert_not_called()
    assert held.active
    await held.aclose()


async def test_a_hold_announcement_keeps_the_hold(line, monkeypatch):
    held, _, _, _ = line
    classify = AsyncMock(return_value=AMDCategory.MACHINE_IVR)
    monkeypatch.setattr(hold_mod, "try_classify_remote_speech", classify)
    await held.start()
    assert await held.keep_holding("Your call is important to us.")
    assert held.active
    await held.aclose()


@pytest.mark.parametrize(
    "verdict", [AMDCategory.HUMAN, AMDCategory.UNCERTAIN, None, "error"]
)
async def test_other_words_end_the_hold(line, monkeypatch, verdict):
    held, _, _, _ = line
    classify = AsyncMock(return_value=None if verdict == "error" else verdict)
    monkeypatch.setattr(hold_mod, "try_classify_remote_speech", classify)
    await held.start()
    assert not await held.keep_holding("Okay, I found it.")
    assert not held.active
    await held.aclose()


async def test_no_classifier_means_words_end_the_hold():
    held = Hold(Session())
    await held.start()
    assert not await held.keep_holding("Hello?")
    assert not held.active
    await held.aclose()


async def test_no_prompt_while_the_agent_may_not_speak(line):
    held, session, now, _ = line
    held._can_speak = lambda: False
    await held.start()
    now[0] = HOLD_SECONDS
    held.check()
    session.generate_reply.assert_not_called()
    await held.aclose()


async def test_hold_pauses_recovery_so_the_agent_never_hangs_up():
    session = Session()
    now = [0.0]
    held = Hold(session, clock=lambda: now[0])
    hangup = AsyncMock()
    recovery = ConversationRecovery(
        session,
        on_exhausted=hangup,
        can_recover=lambda: not held.active,
        clock=lambda: now[0],
    )
    await held.start()
    for second in range(0, int(HOLD_SECONDS), 10):
        now[0] = second
        await recovery.check()
    session.generate_reply.assert_not_called()
    hangup.assert_not_awaited()
    await held.aclose()
    await recovery.aclose()


async def test_agent_drops_a_held_announcement_and_notes_a_hold_end(monkeypatch):
    agent = SpeechSanitizingAgent(instructions="test")
    agent.update_chat_ctx = AsyncMock()
    monkeypatch.setattr(
        SpeechSanitizingAgent, "session", property(lambda _self: Mock())
    )
    held = Mock(active=True)
    held.keep_holding = AsyncMock(return_value=True)
    agent.hold = held
    words = ChatMessage(role="user", content=["Please continue to hold."])
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(ChatContext(), words)
    agent.update_chat_ctx.assert_awaited_once()

    held.keep_holding = AsyncMock(return_value=False)
    turn_ctx = ChatContext()
    await agent.on_user_turn_completed(turn_ctx, words)
    assert turn_ctx.messages()[-1].text_content == HOLD_ENDED_NOTE


async def test_hold_note_names_a_tool_every_call_has():
    assert WAIT_SPEC.name in HOLD_ENDED_NOTE
