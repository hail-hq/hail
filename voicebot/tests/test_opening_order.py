"""Pickup speech must not queue an introduction ahead of Hail's greeting."""

import asyncio
from unittest.mock import AsyncMock, Mock, PropertyMock

import pytest
from hailhq.voicebot.agent import SpeechSanitizingAgent, speak_greeting
from livekit.agents import AgentSession, StopResponse
from livekit.agents.llm import ChatContext, ChatMessage

from ._fakes import FakeAnnouncingSession, FakeLLM


async def test_pickup_hello_cannot_generate_or_queue_an_early_reply():
    llm = FakeLLM(reply="A second greeting must never be queued")
    llm.chat = Mock(wraps=llm.chat)
    agent = SpeechSanitizingAgent(instructions="test", opening_pending=True)
    async with AgentSession(llm=llm) as session:
        await session.start(agent=agent)
        added = []
        session.on("conversation_item_added", lambda ev: added.append(ev.item))
        with pytest.raises(StopResponse):
            await agent.on_user_turn_completed(
                ChatContext(), ChatMessage(role="user", content=["Hello?"])
            )
        # The held words stay in the chat and the transcript.
        assert agent.chat_ctx.messages()[-1].text_content == "Hello?"
        assert [m.text_content for m in added] == ["Hello?"]
        # Also drive the real generation pipeline, covering preemptive work.
        await session.run(user_input="Hello?")
        llm.chat.assert_not_called()
        agent.release_opening()
        result = await session.run(user_input="Can you hear me?")
        result.expect[:].contains_message(role="assistant")
        llm.chat.assert_called_once()


@pytest.mark.parametrize("first_message", [None, "Calling about your order."])
async def test_automatic_replies_resume_only_after_introduction(first_message):
    started, finish = asyncio.Event(), asyncio.Event()
    agent = SpeechSanitizingAgent(instructions="test", opening_pending=True)

    class Session(FakeAnnouncingSession):
        async def say(self, text, *, allow_interruptions):
            self.say_calls.append((text, allow_interruptions))
            started.set()
            await finish.wait()

    session = Session()
    greeting = asyncio.create_task(
        speak_greeting(
            session,
            {"first_message": first_message},
            on_opened=agent.release_opening,
        )
    )
    try:
        await started.wait()
        with pytest.raises(StopResponse):
            await agent.on_user_turn_completed(ChatContext(), None)
        finish.set()
        await greeting
        await agent.on_user_turn_completed(ChatContext(), None)
        assert len(session.say_calls) == 1
        if first_message:
            assert session.generate_reply_calls == []
        else:
            assert len(session.generate_reply_calls) == 1
            assert (
                "Do not greet or introduce yourself again"
                in session.generate_reply_calls[0]
            )
    finally:
        greeting.cancel()
        await asyncio.gather(greeting, return_exceptions=True)


async def test_generated_opening_answers_words_held_during_greeting(monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    agent = SpeechSanitizingAgent(instructions="test", opening_pending=True)

    class Session(FakeAnnouncingSession):
        async def say(self, text, *, allow_interruptions):
            started.set()
            await finish.wait()

    session = Session()
    agent.update_chat_ctx = AsyncMock()
    monkeypatch.setattr(
        SpeechSanitizingAgent, "session", PropertyMock(return_value=Mock())
    )
    greeting = asyncio.create_task(
        speak_greeting(session, {}, on_opened=agent.release_opening)
    )
    try:
        await started.wait()
        with pytest.raises(StopResponse):
            await agent.on_user_turn_completed(
                ChatContext(), ChatMessage(role="user", content=["Who is this?"])
            )
        finish.set()
        await greeting
        assert 'saying: "Who is this?"' in session.generate_reply_calls[0]
    finally:
        greeting.cancel()
        await asyncio.gather(greeting, return_exceptions=True)


def test_release_drops_the_empty_early_reply():
    """An early reply started while held is empty and must not be reused."""
    from livekit.agents.voice.agent_activity import AgentActivity

    assert callable(getattr(AgentActivity, "_cancel_preemptive_generation", None))
    agent = SpeechSanitizingAgent(instructions="test", opening_pending=True)
    assert agent._activity is None
    agent._activity = Mock()
    agent.release_opening()
    agent._activity._cancel_preemptive_generation.assert_called_once()


@pytest.mark.parametrize(
    "metadata, generate_opening, cleared_after",
    [
        # Outbound: drop the unfinished pickup turn before anything is spoken,
        # so its late end of turn cannot cut the opening off.
        ({"direction": "outbound"}, True, [0]),
        ({"direction": "outbound", "first_message": "About your order."}, True, [0]),
        # Deferred menu greeting: the person just spoke; the turn loop answers.
        ({"direction": "outbound"}, False, []),
        # Inbound is unchanged.
        ({"direction": "inbound"}, True, []),
        ({"direction": "inbound", "first_message": "Hi, Acme here."}, True, []),
    ],
)
async def test_outbound_opening_clears_the_pickup_turn_first(
    metadata, generate_opening, cleared_after
):
    session = FakeAnnouncingSession()
    await speak_greeting(session, metadata, generate_opening=generate_opening)
    assert session.cleared_after == cleared_after


async def test_clearing_the_pickup_turn_after_hangup_is_safe():
    class Session(FakeAnnouncingSession):
        def clear_user_turn(self):
            raise RuntimeError("AgentSession isn't running")

    session = Session()
    await speak_greeting(session, {"direction": "outbound"})
    assert len(session.say_calls) == 1


async def test_cleared_turn_with_no_words_gets_no_reply():
    # The cleared turn's end of turn can still fire, with an empty transcript.
    agent = SpeechSanitizingAgent(instructions="test")
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(
            ChatContext(), ChatMessage(role="user", content=[""])
        )


@pytest.mark.parametrize(
    "metadata, generate_opening, cancelled",
    [
        ({"direction": "outbound"}, True, True),
        ({"direction": "outbound"}, False, False),
        ({"direction": "inbound"}, True, False),
    ],
)
async def test_outbound_opening_cancels_the_pending_end_of_turn(
    metadata, generate_opening, cancelled
):
    # The cleared turn's end-of-turn wait must not fire while no speech is
    # current: LiveKit would interrupt the opening before the hook runs.
    import inspect

    from livekit.agents.voice.agent_activity import AgentActivity
    from livekit.agents.voice.audio_recognition import AudioRecognition

    assert "self._activity" in inspect.getsource(AgentSession.__init__)
    assert "self._audio_recognition" in inspect.getsource(AgentActivity.__init__)
    assert "self._end_of_turn_task" in inspect.getsource(AudioRecognition.__init__)
    session = FakeAnnouncingSession()
    session._activity = Mock()
    task = session._activity._audio_recognition._end_of_turn_task
    # Utterances already spoken or requested at each cancel().
    task.cancel.side_effect = lambda: cancelled_after.append(
        len(session.say_calls) + len(session.generate_reply_calls)
    )
    cancelled_after: list[int] = []
    await speak_greeting(session, metadata, generate_opening=generate_opening)
    assert cancelled_after == ([0] if cancelled else [])
