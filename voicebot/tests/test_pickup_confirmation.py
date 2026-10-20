"""Uncertain pickup must earn a conversational reply before the opening."""

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


@pytest.fixture(autouse=True)
def short_deadline(monkeypatch):
    monkeypatch.setattr(confirmation, "RESPONSE_TIMEOUT_SECONDS", 0.08)


async def start(session, llm):
    task = asyncio.create_task(
        confirmation.confirm_pickup(session, llm=llm, prompt="Can you hear me?")
    )
    await asyncio.sleep(0)
    return task


@pytest.mark.parametrize("text", ["", "   "])
async def test_silence_empty_transcripts_and_noise_never_confirm(text):
    session = Session()
    llm = Classifier('{"label":"human"}')
    task = await start(session, llm)
    session.transcribe(text)
    session.transcribe("Yes, I can hear you", final=False)
    session.emit("user_state_changed", SimpleNamespace(new_state="speaking"))
    result = await task
    assert result.category == AMDCategory.UNCERTAIN
    assert llm.calls == []
    assert not session._events.get("user_input_transcribed")


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
    result = await task
    assert result.category.value == category
    assert result.transcript == text
    session.say.assert_awaited_once_with("Can you hear me?", allow_interruptions=True)
    assert llm.calls[0]["conn_options"].timeout >= 10
    assert llm.calls[0]["conn_options"].max_retry == 0


async def test_words_from_before_probe_are_not_confirmation():
    session = Session()
    llm = Classifier('{"label":"human"}')
    session.transcribe("Yes")
    result = await (await start(session, llm))
    assert result.category == AMDCategory.UNCERTAIN
    assert llm.calls == []


async def test_human_can_answer_during_probe():
    session = Session()
    llm = Classifier('{"label":"human"}')

    async def say(*args, **kwargs):
        session.transcribe("Who is calling?")
        await asyncio.sleep(0)

    session.say.side_effect = say
    result = await (await start(session, llm))
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
    assert (await task).category == AMDCategory.HUMAN


async def test_ongoing_unclassified_audio_cannot_reset_deadline():
    session = Session()
    llm = Classifier('{"label":"uncertain"}')
    task = await start(session, llm)

    async def noise():
        while not task.done():
            session.transcribe("Hello")
            await asyncio.sleep(0.005)

    producer = asyncio.create_task(noise())
    try:
        result = await asyncio.wait_for(task, timeout=0.3)
        assert result.category == AMDCategory.UNCERTAIN
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


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
    result = await task
    assert result.category == AMDCategory.MACHINE_VM
    assert "Hello" in result.transcript
    assert "Leave a message" in result.transcript


async def test_slow_classifier_cannot_extend_response_deadline():
    session = Session()
    llm = Classifier('{"label":"human"}')
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class SlowStream:
        async def __aenter__(self):
            return self

        async def collect(self):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def __aexit__(self, *args):
            pass

    llm.chat = lambda **kwargs: SlowStream()
    task = await start(session, llm)
    session.transcribe("Yes, I hear you")
    await entered.wait()
    result = await asyncio.wait_for(task, timeout=0.3)
    assert result.category == AMDCategory.UNCERTAIN
    assert cancelled.is_set()


@pytest.mark.parametrize("failure", ["closed", "tts", "cancelled", "llm"])
async def test_shutdown_provider_errors_and_cancellation_keep_opening_blocked(failure):
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
        result = await task
        assert result.category == AMDCategory.UNCERTAIN
        assert result.reason == (
            confirmation.UNAVAILABLE_REASON
            if failure == "llm"
            else confirmation.UNANSWERED_REASON
        )
    assert not session._events.get("user_input_transcribed")
    assert not session._events.get("close")


async def test_bad_label_is_unavailable_not_unanswered():
    session = Session()
    task = await start(session, Classifier('{"label":"robot"}'))
    session.transcribe("Yes, I can hear you")
    result = await task
    assert result.category == AMDCategory.UNCERTAIN
    assert result.reason == confirmation.UNAVAILABLE_REASON


async def test_person_who_keeps_talking_is_still_confirmed():
    """Every verdict is superseded by newer words until the deadline."""
    session = Session()
    llm = Classifier('{"label":"human"}')
    original = llm.chat

    def chat(**kwargs):
        session.transcribe("Hello?")
        return original(**kwargs)

    llm.chat = chat
    task = await start(session, llm)
    session.transcribe("Yes? Who is this?")
    result = await asyncio.wait_for(task, timeout=0.5)
    assert result.category == AMDCategory.HUMAN


async def test_long_probe_gets_time_to_play(monkeypatch):
    monkeypatch.setattr(confirmation, "PROBE_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(confirmation, "PROBE_CHARS_PER_SECOND", 100.0)
    session = Session()

    async def say(*args, **kwargs):
        await asyncio.sleep(0.05)
        session.transcribe("Yes")

    session.say.side_effect = say
    result = await confirmation.confirm_pickup(
        session, llm=Classifier('{"label":"human"}'), prompt="x" * 20
    )
    assert result.category == AMDCategory.HUMAN


def test_probe_line_covers_every_supported_language():
    assert confirmation.PROBE_LINES.keys() == SUPPORTED_LANGUAGES.keys()
    assert confirmation.probe_line(None) == "Can you hear me?"
    assert confirmation.probe_line("xx") == "Can you hear me?"
    assert confirmation.probe_line("sv") == "Hör du mig?"
