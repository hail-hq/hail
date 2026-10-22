"""Uncertain pickup gets a short probe; only full silence hangs up."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.core.languages import SUPPORTED_LANGUAGES
from hailhq.voicebot import confirmation
from livekit.agents.utils import EventEmitter
from livekit.agents.voice.amd import AMDCategory

from .test_pickup import Classifier


class Session(EventEmitter):
    def __init__(self):
        super().__init__()
        self.say = AsyncMock()

    def transcribe(self, text, *, final=True):
        self.emit(
            "user_input_transcribed",
            SimpleNamespace(transcript=text, is_final=final),
        )

    def speak(self):
        self.emit("user_state_changed", SimpleNamespace(new_state="speaking"))


@pytest.fixture(autouse=True)
def short_deadline(monkeypatch):
    monkeypatch.setattr(confirmation, "RESPONSE_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(confirmation, "SPEECH_CAP_SECONDS", 0.08)


async def start(session, llm):
    task = asyncio.create_task(
        confirmation.confirm_pickup(session, llm=llm, prompt="Can you hear me?")
    )
    await asyncio.sleep(0)
    return task


def assert_detached(session):
    for event in ("user_input_transcribed", "user_state_changed", "close"):
        assert not session._events.get(event)


@pytest.mark.parametrize("text", ["", "   "])
async def test_silence_and_empty_transcripts_hang_up(text):
    session = Session()
    llm = Classifier('{"label":"human"}')
    task = await start(session, llm)
    session.transcribe(text)
    session.transcribe("Yes, I can hear you", final=False)
    session.emit("user_state_changed", SimpleNamespace(new_state="listening"))
    result, _heard = await task
    assert result.category == AMDCategory.UNCERTAIN
    assert result.reason == confirmation.UNANSWERED_REASON
    assert llm.calls == []
    assert_detached(session)


async def test_voice_without_a_transcript_opens_the_call():
    """STT is one language per call; the person may speak another."""
    session = Session()
    llm = Classifier('{"label":"human"}')
    task = await start(session, llm)
    session.speak()
    result, _heard = await task
    assert result.category == AMDCategory.UNCERTAIN
    assert result.reason == confirmation.HEARD_REASON
    assert llm.calls == []
    assert_detached(session)


async def test_voice_stops_the_no_speech_timer(monkeypatch):
    monkeypatch.setattr(confirmation, "RESPONSE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(confirmation, "SPEECH_CAP_SECONDS", 0.3)
    session = Session()
    task = await start(session, Classifier('{"label":"human"}'))
    session.speak()
    await asyncio.sleep(0.1)
    assert not task.done()
    session.transcribe("Hallå?")
    assert (await task)[0].category == AMDCategory.HUMAN


@pytest.mark.parametrize("text", ["Hello?", "Hej", "Bonjour"])
async def test_greeting_reply_opens_the_call(text):
    session = Session()
    llm = Classifier('{"label":"human"}')
    task = await start(session, llm)
    session.transcribe(text)
    result, _heard = await task
    assert result.is_human
    assert f'"{text}"' in confirmation.CONFIRMATION_PROMPT
    assert '"Bonjour" -> human' in confirmation.CONFIRMATION_PROMPT


async def test_uncertain_reply_opens_the_call():
    session = Session()
    task = await start(session, Classifier('{"label":"uncertain"}'))
    session.transcribe("Hello, you have reached Sam")
    result, _heard = await task
    assert result.category == AMDCategory.UNCERTAIN
    assert result.reason == "pickup_confirmation"


@pytest.mark.parametrize(
    "category,text",
    [
        ("human", "Yes, I can hear you over the music."),
        ("human", "Ja, jag hör dig. Vem är det?"),
        ("machine-vm", "Please leave a message after the tone."),
        ("machine-vm", "Lämna ett meddelande efter tonen."),
        ("machine-unavailable", "Unrelated song lyrics"),
        ("machine-ivr", "For reception, press one."),
    ],
)
async def test_only_classified_responses_unlock_their_matching_path(category, text):
    session = Session()
    llm = Classifier(json.dumps({"label": category}))
    task = await start(session, llm)
    session.transcribe(text)
    result, _heard = await task
    assert result.category.value == category
    assert result.transcript == text
    assert _heard
    session.say.assert_awaited_once_with("Can you hear me?", allow_interruptions=True)
    assert llm.calls[0]["conn_options"].timeout >= 10
    assert llm.calls[0]["conn_options"].max_retry == 0


async def test_words_from_before_probe_are_not_confirmation():
    session = Session()
    llm = Classifier('{"label":"human"}')
    session.transcribe("Yes")
    result, _heard = await (await start(session, llm))
    assert result.category == AMDCategory.UNCERTAIN
    assert llm.calls == []


async def test_human_can_answer_during_probe():
    session = Session()
    llm = Classifier('{"label":"human"}')

    async def say(*args, **kwargs):
        session.transcribe("Who is calling?")
        await asyncio.sleep(0)

    session.say.side_effect = say
    result, _heard = await (await start(session, llm))
    assert result.category == AMDCategory.HUMAN


async def test_deadline_starts_after_probe_playback():
    session = Session()
    started, finish = asyncio.Event(), asyncio.Event()

    async def say(*args, **kwargs):
        started.set()
        await finish.wait()

    session.say.side_effect = say
    task = await start(session, Classifier('{"label":"human"}'))
    await started.wait()
    await asyncio.sleep(0.1)
    assert not task.done()
    finish.set()
    await asyncio.sleep(0)
    session.transcribe("Yes, I hear you")
    assert (await task)[0].category == AMDCategory.HUMAN


async def test_new_words_during_classification_prevent_stale_human_verdict():
    session = Session()
    llm = Classifier('{"label":"human"}')
    called = asyncio.Event()
    original = llm.chat

    def chat(**kwargs):
        if not called.is_set():
            called.set()
            session.transcribe("Leave a message after the tone.")
        else:
            llm.args = '{"label":"machine-vm"}'
        return original(**kwargs)

    llm.chat = chat
    task = await start(session, llm)
    session.transcribe("Hello")
    result, _heard = await task
    assert result.category == AMDCategory.MACHINE_VM
    assert "Hello" in result.transcript
    assert "Leave a message" in result.transcript


async def test_cap_never_cancels_a_running_classifier():
    """The classifier ends under its own provider timeout, not the cap."""
    session = Session()
    llm = Classifier('{"label":"machine-vm"}')
    original = llm.chat
    cancelled = asyncio.Event()

    class SlowStream:
        def __init__(self, stream):
            self.stream = stream

        async def __aenter__(self):
            await self.stream.__aenter__()
            return self

        async def collect(self):
            try:
                # Longer than the 0.08 s cap.
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return await self.stream.collect()

        async def __aexit__(self, *args):
            await self.stream.__aexit__(*args)

    llm.chat = lambda **kwargs: SlowStream(original(**kwargs))
    task = await start(session, llm)
    session.transcribe("Leave a message after the tone.")
    result, _heard = await asyncio.wait_for(task, timeout=1.0)
    assert result.category == AMDCategory.MACHINE_VM
    assert not cancelled.is_set()


@pytest.mark.parametrize("failure", ["closed", "tts", "cancelled", "llm"])
async def test_shutdown_provider_errors_and_cancellation(failure):
    session = Session()
    llm = Classifier('{"label":"human"}')
    if failure == "tts":
        session.say.side_effect = RuntimeError("session closed")
    task = await start(session, llm)
    if failure == "closed":
        session.emit("close", SimpleNamespace())
    elif failure == "cancelled":
        task.cancel()
    elif failure == "llm":
        llm.chat = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("failed"))
        session.transcribe("Yes")
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result, _heard = await task
        assert result.category == AMDCategory.UNCERTAIN
        assert _heard == (failure != "tts")
        # Only a closed session hangs up; a TTS or classifier error talks.
        assert result.reason == (
            confirmation.UNANSWERED_REASON
            if failure == "closed"
            else confirmation.UNAVAILABLE_REASON
        )
    assert_detached(session)


async def test_interrupted_probe_is_not_heard():
    """Talked over mid-probe: the listener may have missed the disclosure."""
    session = Session()
    session.say.return_value = SimpleNamespace(interrupted=True)
    task = await start(session, Classifier('{"label":"human"}'))
    session.transcribe("Hello? Who is this?")
    result, heard = await task
    assert result.is_human
    assert not heard


async def test_bad_label_is_unavailable_not_unanswered():
    session = Session()
    task = await start(session, Classifier('{"label":"robot"}'))
    session.transcribe("Yes, I can hear you")
    result, _heard = await task
    assert result.category == AMDCategory.UNCERTAIN
    assert result.reason == confirmation.UNAVAILABLE_REASON


async def test_person_who_keeps_talking_is_still_confirmed():
    """Every verdict is superseded by newer words until the cap."""
    session = Session()
    llm = Classifier('{"label":"human"}')
    original = llm.chat

    def chat(**kwargs):
        session.transcribe("Hello?")
        return original(**kwargs)

    llm.chat = chat
    task = await start(session, llm)
    session.transcribe("Yes? Who is this?")
    result, _heard = await asyncio.wait_for(task, timeout=0.5)
    assert result.category == AMDCategory.HUMAN


async def test_long_probe_gets_time_to_play(monkeypatch):
    monkeypatch.setattr(confirmation, "PROBE_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(confirmation, "PROBE_CHARS_PER_SECOND", 100.0)
    session = Session()

    async def say(*args, **kwargs):
        await asyncio.sleep(0.05)
        session.transcribe("Yes")

    session.say.side_effect = say
    result, _heard = await confirmation.confirm_pickup(
        session, llm=Classifier('{"label":"human"}'), prompt="x" * 20
    )
    assert result.category == AMDCategory.HUMAN


def test_probe_line_covers_every_supported_language():
    assert confirmation.PROBE_LINES.keys() == SUPPORTED_LANGUAGES.keys()
    assert confirmation.probe_line(None) == "Can you hear me?"
    assert confirmation.probe_line("xx") == "Can you hear me?"
    assert confirmation.probe_line("sv") == "Hör du mig?"


async def test_echo_during_the_probe_is_not_a_reply():
    """VAD on our own echoed probe must not stop the silence hang-up."""
    session = Session()

    async def say(*_args, **_kwargs):
        session.speak()
        return SimpleNamespace(interrupted=False)

    session.say = AsyncMock(side_effect=say)
    llm = Classifier('{"label":"human"}')
    result, heard = await (await start(session, llm))
    assert result.reason == confirmation.UNANSWERED_REASON
    assert heard is True
    assert llm.calls == []
    assert_detached(session)


async def test_speech_still_going_after_the_probe_counts():
    session = Session()
    session.user_state = "speaking"
    llm = Classifier('{"label":"human"}')
    result, _heard = await (await start(session, llm))
    assert result.reason == confirmation.HEARD_REASON
    assert_detached(session)
