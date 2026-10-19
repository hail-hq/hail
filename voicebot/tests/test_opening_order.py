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
