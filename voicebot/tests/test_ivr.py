"""Phone menus must not unlock speech merely because another prompt starts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hailhq.voicebot import agent as agent_mod
from hailhq.voicebot.agent import SpeechSanitizingAgent
from hailhq.voicebot.ivr import (
    PhoneMenu,
    classify_remote_speech,
    try_classify_remote_speech,
)
from livekit.agents import Agent, AgentSession
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.agents.voice.amd import AMDCategory

from ._fakes import FakeLLM
from .test_pickup import Classifier


async def chunks(text):
    yield text


async def test_menu_narration_never_reaches_tts(monkeypatch):
    agent = SpeechSanitizingAgent(instructions="test")
    agent.phone_menu = SimpleNamespace(active=True)
    monkeypatch.setattr(Agent.default, "tts_node", lambda _, text, settings: text)
    audio_input = await agent.tts_node(
        chunks("The menu wants a booking number. I'll enter it."), None
    )
    spoken = "" if audio_input is None else "".join([x async for x in audio_input])
    assert spoken == ""


async def test_every_menu_generation_receives_navigation_rules(monkeypatch):
    llm = FakeLLM(reply="...")
    llm.chat = Mock(wraps=llm.chat)
    agent = SpeechSanitizingAgent(instructions="Check an existing reservation.")
    agent.phone_menu = SimpleNamespace(active=True, checking=False, can_navigate=True)
    async with AgentSession(llm=llm) as session:
        await session.start(agent=agent)
        # Explicit generations also cover SDK silence/loop checks and tool followups.
        for _ in range(2):
            await session.run(user_input="")
        assert llm.chat.call_count == 2
        for call in llm.chat.call_args_list:
            instructions = "\n".join(
                m.text_content or "" for m in call.kwargs["chat_ctx"].messages()
            )
            assert "Wait for the complete menu" in instructions
            assert "Do not narrate" in instructions


async def test_next_recorded_prompt_does_not_mean_person_detected(monkeypatch):
    from uuid import UUID

    from .test_amd import _FakeAmdSession

    session = _FakeAmdSession()
    session.llm = Classifier('{"label":"machine-ivr"}')
    agent = SpeechSanitizingAgent(instructions="test")
    agent.update_chat_ctx = AsyncMock()
    events = []

    async def write(call_id, kind, payload):
        events.append(kind)

    monkeypatch.setattr(agent_mod, "write_call_event", write)
    tasks = set()
    agent_mod.arm_deferred_greeting(
        session, {}, UUID(int=1), tasks, agent=agent, on_machine=AsyncMock()
    )
    session.emit_keypress()
    await agent.on_user_turn_completed(
        ChatContext(),
        ChatMessage(role="user", content=["To book a new vacation, press one."]),
    )
    await asyncio.gather(*list(tasks))
    assert "person_detected" not in events
    assert session.say_calls == []


def menu_for(label):
    outcomes = []

    async def human(text):
        outcomes.append(("human", text))

    async def machine(category):
        outcomes.append(("machine", category.value))

    return (
        PhoneMenu(
            Classifier(json.dumps({"label": label})), on_human=human, on_machine=machine
        ),
        outcomes,
    )


@pytest.mark.parametrize(
    "text",
    [
        "To book a new vacation, press one.",
        "For help with an existing reservation, press two.",
        "Please stay on the line for a vacation expert.",
    ],
)
async def test_recorded_menu_and_hold_stay_silent(text):
    menu, outcomes = menu_for("machine-ivr")
    await menu.observe(text)
    assert menu.active
    assert outcomes == []


@pytest.mark.parametrize("text", ["Hello, reception.", "Bona tarda, recepció, digui?"])
async def test_classified_live_greeting_releases_once_without_a_keypress(text):
    menu, outcomes = menu_for("human")
    await menu.observe(text)
    await menu.observe(text)
    assert not menu.active
    assert outcomes == [("human", text)]


@pytest.mark.parametrize("label", ["machine-vm", "machine-unavailable"])
async def test_machine_reached_after_menu_ends_without_a_greeting(label):
    menu, outcomes = menu_for(label)
    await menu.observe("Please leave a message after the tone.")
    assert outcomes == [("machine", label)]
    assert menu.active


@pytest.mark.parametrize("args", ['{"label":"uncertain"}', '{"label":"bogus"}', "{}"])
async def test_unconfirmed_or_bad_classifier_output_never_unlocks_speech(args):
    menu, outcomes = menu_for("human")
    menu.llm.args = args
    await menu.observe("Hello")
    assert menu.active
    assert not menu.checking
    assert outcomes == []


async def test_classifier_error_does_not_unlock_or_hang_up():
    menu, outcomes = menu_for("human")
    menu.llm.chat = Mock(side_effect=RuntimeError("provider failed"))
    await menu.observe("Hello")
    assert menu.active
    assert outcomes == []


async def test_classifier_timeout_is_bounded(monkeypatch):
    from hailhq.voicebot import ivr

    menu, outcomes = menu_for("human")
    entered = asyncio.Event()

    class SlowStream:
        async def __aenter__(self):
            return self

        async def collect(self):
            entered.set()
            await asyncio.Future()

        async def __aexit__(self, *args):
            pass

    menu.llm.chat = lambda **kwargs: SlowStream()
    monkeypatch.setattr(ivr, "CLASSIFY_TIMEOUT_SECONDS", 0.01)
    result = await asyncio.wait_for(menu.observe("Hello"), 0.2)
    assert entered.is_set()
    assert result == AMDCategory.UNCERTAIN
    assert menu.active and not menu.checking
    assert outcomes == []


async def test_cancellation_cannot_release_greeting(monkeypatch):
    from hailhq.voicebot import ivr

    menu, outcomes = menu_for("human")
    entered = asyncio.Event()

    async def pending(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(ivr, "classify_remote_speech", pending)
    task = asyncio.create_task(menu.observe("Hello"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert menu.active and not menu.checking
    assert outcomes == []


async def test_empty_speech_never_calls_classifier():
    menu, outcomes = menu_for("human")
    await menu.observe("   ")
    assert menu.llm.calls == []
    assert outcomes == []


async def test_menu_turn_classification_uses_earlier_speech_as_context_only():
    menu, _ = menu_for("machine-ivr")
    await menu.observe("Welcome to Delta Vacations.")
    await menu.observe("To book a new vacation, press one.")
    request = menu.llm.calls[-1]
    messages = list(request["chat_ctx"].messages())
    assert messages[-2].text_content == "Earlier speech:\nWelcome to Delta Vacations."
    assert (
        messages[-1].text_content
        == "Latest speech:\nTo book a new vacation, press one."
    )
    assert request["conn_options"].max_retry == 0
    assert request["conn_options"].timeout == 10.0
    assert request["tool_choice"] == "required"


async def test_missing_classifier_is_uncertain():
    assert await classify_remote_speech(None, "Welcome") == AMDCategory.UNCERTAIN


async def test_failed_check_gives_no_verdict():
    """The unavailable recheck tells a failure apart from an unsure answer."""
    assert await try_classify_remote_speech(None, "Welcome") is None
    assert (
        await try_classify_remote_speech(
            Classifier(json.dumps({"label": "human"})), " "
        )
        is None
    )
    failing = Classifier(json.dumps({"label": "human"}))
    failing.chat = Mock(side_effect=RuntimeError("down"))
    assert await try_classify_remote_speech(failing, "Welcome") is None
    assert (
        await try_classify_remote_speech(
            Classifier(json.dumps({"label": "uncertain"})), "Welcome"
        )
        == AMDCategory.UNCERTAIN
    )


async def test_new_remote_speech_discards_a_pending_human_verdict(monkeypatch):
    from hailhq.voicebot import ivr

    menu, outcomes = menu_for("human")
    entered, finish = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        entered.set()
        await finish.wait()
        return AMDCategory.HUMAN

    monkeypatch.setattr(ivr, "classify_remote_speech", delayed)
    task = asyncio.create_task(menu.observe("Hello"))
    await entered.wait()
    menu.invalidate()
    finish.set()
    assert await task == AMDCategory.UNCERTAIN
    assert menu.active and not menu.checking
    assert not menu.can_navigate
    assert outcomes == []


async def test_silent_menu_narration_is_not_written_as_spoken_transcript(monkeypatch):
    from uuid import UUID

    from .test_amd import _FakeAmdSession

    events = []

    async def write(call_id, kind, payload):
        events.append((kind, payload))

    monkeypatch.setattr(agent_mod, "write_call_event", write)
    session = _FakeAmdSession()
    tasks = agent_mod.attach_event_handlers(
        session, UUID(int=1), speech_allowed=lambda: False
    )
    message = ChatMessage(role="assistant", content=["Pressing two now."])
    for handler in session.handlers["conversation_item_added"]:
        handler(SimpleNamespace(item=message))
    await asyncio.gather(*list(tasks))
    assert not [event for event in events if event[0] == "agent_turn"]


async def test_database_marker_cannot_delay_the_human_greeting(monkeypatch):
    from uuid import UUID

    from .test_amd import _FakeAmdSession

    session = _FakeAmdSession()
    agent = SpeechSanitizingAgent(instructions="test")
    finish = asyncio.Event()

    async def slow_write(*args):
        await finish.wait()

    monkeypatch.setattr(agent_mod, "write_call_event", slow_write)
    tasks = set()
    agent_mod.arm_deferred_greeting(
        session, {}, UUID(int=1), tasks, agent=agent, on_machine=AsyncMock()
    )
    turn = asyncio.create_task(
        agent.on_user_turn_completed(
            ChatContext(), ChatMessage(role="user", content=["Hello, reception."])
        )
    )
    try:
        done, _ = await asyncio.wait([turn], timeout=0.2)
        assert turn in done, "The greeting waited on the database marker"
        assert session.say_calls
    finally:
        finish.set()
        await turn
        await asyncio.gather(*list(tasks))


def greeting_menu(monkeypatch, llm=None):
    from hailhq.voicebot import ivr

    monkeypatch.setattr(ivr, "GREET_TIMEOUT_SECONDS", 0.01)
    greeted = asyncio.Event()

    async def human(text):
        greeted.set()

    menu = PhoneMenu(llm, on_human=AsyncMock(side_effect=human), on_machine=AsyncMock())
    return menu, greeted


async def test_untranscribed_speech_greets_after_a_bounded_wait(monkeypatch):
    """Uncertain is treated as a person; only a machine verdict hangs up."""
    menu, greeted = greeting_menu(monkeypatch)
    try:
        menu.pressed_keys()
        menu.heard_speech()  # A person in a language STT does not transcribe.
        menu.finished_speaking()
        await asyncio.wait_for(greeted.wait(), 0.2)
        assert not menu.active
        menu.on_human.assert_awaited_once_with("")
        menu.on_machine.assert_not_awaited()
    finally:
        await menu.aclose()


async def test_untranscribed_speech_lets_the_menu_reply_again(monkeypatch):
    menu, _ = greeting_menu(monkeypatch)
    try:
        menu.heard_speech()
        assert not menu.can_navigate
        # An empty transcript never reaches observe().
        menu.finished_speaking()
        assert menu.can_navigate
    finally:
        await menu.aclose()


async def test_untranscribed_audio_after_hold_announcement_does_not_greet(
    monkeypatch,
):
    menu, greeted = greeting_menu(monkeypatch, Classifier('{"label":"machine-ivr"}'))
    try:
        await menu.observe("Please hold for the next available agent.")
        for _ in range(2):  # Hold music and clicks: VAD speech, no transcript.
            menu.heard_speech()
            menu.finished_speaking()
            await asyncio.sleep(0.03)
        assert not greeted.is_set()
        assert menu.active and menu.can_navigate
    finally:
        await menu.aclose()


async def test_untranscribed_audio_after_pickup_menu_does_not_greet(monkeypatch):
    """The menu exists because the pickup was a recorded menu or hold line."""
    menu, greeted = greeting_menu(monkeypatch)
    try:
        menu.heard_speech()
        menu.finished_speaking()
        await asyncio.sleep(0.03)
        assert not greeted.is_set()
        assert menu.active
    finally:
        await menu.aclose()


async def test_hold_music_during_classification_does_not_greet(monkeypatch):
    from hailhq.voicebot import ivr

    menu, greeted = greeting_menu(monkeypatch)
    menu.pressed_keys()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        entered.set()
        await finish.wait()
        return AMDCategory.MACHINE_IVR

    monkeypatch.setattr(ivr, "classify_remote_speech", delayed)
    try:
        task = asyncio.create_task(menu.observe("Please hold."))
        await entered.wait()
        menu.heard_speech()  # Hold music starts before the verdict returns.
        finish.set()
        assert await task == AMDCategory.UNCERTAIN
        menu.finished_speaking()
        await asyncio.sleep(0.03)
        assert not greeted.is_set()
        assert menu.active
    finally:
        await menu.aclose()


async def test_keypress_after_menu_lets_untranscribed_speech_greet(monkeypatch):
    menu, greeted = greeting_menu(monkeypatch, Classifier('{"label":"machine-ivr"}'))
    try:
        await menu.observe("For reception, press one.")
        menu.pressed_keys()
        menu.heard_speech()  # A person in a language STT does not transcribe.
        menu.finished_speaking()
        await asyncio.wait_for(greeted.wait(), 0.2)
    finally:
        await menu.aclose()


async def test_long_menu_with_transcription_progress_never_hits_missing_text_deadline(
    monkeypatch,
):
    menu, greeted = greeting_menu(monkeypatch, Classifier('{"label":"machine-ivr"}'))
    try:
        menu.heard_speech()
        await asyncio.sleep(0.03)  # A menu longer than the missing-text deadline.
        assert not greeted.is_set()
        menu.transcript_progress("Welcome. For a new booking, press one.")
        menu.finished_speaking()
        await menu.observe("Welcome. For a new booking, press one.")
        await asyncio.sleep(0.03)
        assert not greeted.is_set()
        assert menu.active
    finally:
        await menu.aclose()


async def test_interim_without_completed_turn_still_greets_after_speech_ends(
    monkeypatch,
):
    menu, greeted = greeting_menu(monkeypatch)
    try:
        menu.pressed_keys()
        menu.heard_speech()
        menu.transcript_progress("Welcome to")
        menu.finished_speaking()
        await asyncio.wait_for(greeted.wait(), 0.2)
        menu.on_machine.assert_not_awaited()
    finally:
        await menu.aclose()


async def test_late_transcript_cannot_cancel_or_repeat_an_initiated_greeting(
    monkeypatch,
):
    from hailhq.voicebot import ivr

    monkeypatch.setattr(ivr, "GREET_TIMEOUT_SECONDS", 0.01)
    started, finish = asyncio.Event(), asyncio.Event()
    completed = []

    async def greet(text):
        started.set()
        await finish.wait()
        completed.append(text)

    menu = PhoneMenu(
        Classifier('{"label":"human"}'), on_human=greet, on_machine=AsyncMock()
    )
    try:
        # No transcript arrives after the speech ends.
        menu.pressed_keys()
        menu.heard_speech()
        menu.finished_speaking()
        await asyncio.wait_for(started.wait(), 0.2)
        menu.heard_speech()
        menu.transcript_progress("Hello, reception.")
        await menu.observe("Hello, reception.")
        finish.set()
        await asyncio.sleep(0)
        assert completed == [""]
        assert menu.llm.calls == []
    finally:
        finish.set()
        await menu.aclose()


async def test_valid_transcript_cancels_the_missing_speech_deadline(monkeypatch):
    menu, greeted = greeting_menu(monkeypatch, Classifier('{"label":"machine-ivr"}'))
    try:
        menu.heard_speech()
        await menu.observe("For an existing reservation, press two.")
        await asyncio.sleep(0.03)
        assert menu.can_navigate
        assert not greeted.is_set()
    finally:
        await menu.aclose()


async def test_late_progress_after_uncertain_verdict_still_greets(monkeypatch):
    menu, greeted = greeting_menu(monkeypatch, Classifier('{"label":"uncertain"}'))
    try:
        await menu.observe("Hello")
        menu.transcript_progress("Hello there")
        await asyncio.wait_for(greeted.wait(), 0.2)
        menu.on_human.assert_awaited_once_with("")
    finally:
        await menu.aclose()


async def test_failed_greeting_after_unconfirmed_speech_never_hangs_up(
    monkeypatch,
):
    from uuid import UUID

    from hailhq.voicebot import ivr

    from .test_amd import _FakeAmdSession

    monkeypatch.setattr(ivr, "GREET_TIMEOUT_SECONDS", 0.01)
    failed = asyncio.Event()

    async def speak(session, metadata, **kwargs):
        failed.set()
        raise RuntimeError("tts failed")

    async def write(*args):
        pass

    monkeypatch.setattr(agent_mod, "speak_greeting", speak)
    monkeypatch.setattr(agent_mod, "write_call_event", write)
    agent = SpeechSanitizingAgent(instructions="test")
    on_machine = AsyncMock()
    agent_mod.arm_deferred_greeting(
        _FakeAmdSession(), {}, UUID(int=1), set(), agent=agent, on_machine=on_machine
    )
    try:
        agent.phone_menu.pressed_keys()
        agent.phone_menu.heard_speech()
        agent.phone_menu.finished_speaking()
        await asyncio.wait_for(failed.wait(), 0.2)
        await asyncio.sleep(0)
        on_machine.assert_not_awaited()
        assert not agent.phone_menu.active
    finally:
        await agent.phone_menu.aclose()


async def test_menu_keypress_reaches_the_phone_menu(monkeypatch):
    from uuid import UUID

    from .test_amd import _FakeAmdSession

    session = _FakeAmdSession()
    agent = SpeechSanitizingAgent(instructions="test")
    agent_mod.arm_deferred_greeting(
        session, {}, UUID(int=1), set(), agent=agent, on_machine=AsyncMock()
    )
    agent.phone_menu._recorded = True
    session.emit_keypress("other_tool")
    assert agent.phone_menu._recorded
    session.emit_keypress()
    assert not agent.phone_menu._recorded


async def _greet_with(monkeypatch, agent, speak):
    from uuid import UUID

    from .test_amd import _FakeAmdSession

    async def write(*args):
        pass

    monkeypatch.setattr(agent_mod, "write_call_event", write)
    monkeypatch.setattr(agent_mod, "speak_greeting", speak)
    greeted = []
    agent_mod.arm_deferred_greeting(
        _FakeAmdSession(),
        {},
        UUID(int=1),
        set(),
        on_greeted=lambda: greeted.append(True),
        agent=agent,
        on_machine=AsyncMock(),
    )
    turn_ctx = ChatContext()
    await agent.on_user_turn_completed(
        turn_ctx, ChatMessage(role="user", content=["Hello, reception."])
    )
    return turn_ctx, greeted


async def test_reply_to_the_person_knows_the_greeting_was_spoken(monkeypatch):
    agent = SpeechSanitizingAgent(instructions="test")

    async def speak(session, metadata, **kwargs):
        agent._chat_ctx.add_message(role="assistant", content=["Hi, I'm an AI."])
        kwargs["on_opened"]()

    turn_ctx, greeted = await _greet_with(monkeypatch, agent, speak)
    assert [m.text_content for m in turn_ctx.messages()] == ["Hi, I'm an AI."]
    assert greeted == [True]


async def test_failed_deferred_greeting_does_not_escape_the_turn_hook(monkeypatch):
    agent = SpeechSanitizingAgent(instructions="test")

    async def speak(session, metadata, **kwargs):
        kwargs["on_opened"]()
        raise RuntimeError("session closed")

    _, greeted = await _greet_with(monkeypatch, agent, speak)
    assert greeted == []
    assert not agent._opening_pending


async def _timer_greeting(monkeypatch, metadata, *, said_over_greeting=None):
    from uuid import UUID

    from hailhq.voicebot import ivr

    from .test_amd import _FakeAmdSession

    async def write(*args):
        pass

    monkeypatch.setattr(ivr, "GREET_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(agent_mod, "write_call_event", write)
    session = _FakeAmdSession()
    session.llm = Classifier('{"label":"uncertain"}')
    agent = SpeechSanitizingAgent(instructions="test")
    agent.update_chat_ctx = AsyncMock()
    greeted = asyncio.Event()
    if said_over_greeting:

        def say(text, *, allow_interruptions=True):
            session.say_calls.append(text)
            agent._held_words.append(said_over_greeting)

            async def done():
                return None

            return done()

        session.say = say
    agent_mod.arm_deferred_greeting(
        session,
        metadata,
        UUID(int=1),
        set(),
        on_greeted=greeted.set,
        agent=agent,
        on_machine=AsyncMock(),
    )
    try:
        agent.phone_menu.pressed_keys()
        await agent.on_user_turn_completed(
            ChatContext(), ChatMessage(role="user", content=["Hello?"])
        )
        await asyncio.wait_for(greeted.wait(), 0.2)
    finally:
        await agent.phone_menu.aclose()
    return session


async def test_timer_greeting_answers_the_speech_that_started_the_wait(monkeypatch):
    # No disclosure and no first_message: the timer must not greet in silence.
    session = await _timer_greeting(monkeypatch, {"ai_disclosure": False})
    assert session.say_calls == []
    assert len(session.replies) == 1
    assert '"Hello?"' in session.replies[0]


async def test_timer_greeting_answers_words_said_over_the_greeting(monkeypatch):
    session = await _timer_greeting(
        monkeypatch, {"org_name": "Acme"}, said_over_greeting="Who is this?"
    )
    assert session.say_calls
    assert len(session.replies) == 1
    assert "Hello?" in session.replies[0]
    assert "Who is this?" in session.replies[0]
