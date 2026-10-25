"""The handover pickup check: listen to the contact's leg, then classify."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.voicebot import handover_screen, pickup
from livekit import rtc
from livekit.agents.stt import SpeechData, SpeechEvent, SpeechEventType
from livekit.agents.vad import VADEventType
from livekit.agents.voice.amd import AMDCategory


def _frame(samples=320):
    return rtc.AudioFrame(
        data=bytes([1, 0]) * samples,
        sample_rate=16000,
        num_channels=1,
        samples_per_channel=samples,
    )


class _AudioStream:
    """``rtc.AudioStream``: one 20 ms frame every few ms, forever."""

    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0.001)
        return SimpleNamespace(frame=_frame())

    async def aclose(self) -> None:
        self.closed = True


class _VADStream:
    """Emits one scripted event per pushed frame, then INFERENCE_DONE ticks."""

    def __init__(self, script: list[VADEventType]) -> None:
        self._script = list(script)
        self._queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def push_frame(self, _frame) -> None:
        kind = self._script.pop(0) if self._script else VADEventType.INFERENCE_DONE
        self._queue.put_nowait(SimpleNamespace(type=kind))

    def end_input(self) -> None:
        self._queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def leg(monkeypatch):
    audio = _AudioStream()
    publication = SimpleNamespace(track=object())
    monkeypatch.setattr(
        handover_screen,
        "wait_for_track_publication",
        AsyncMock(return_value=publication),
    )
    monkeypatch.setattr(
        handover_screen.rtc.AudioStream, "from_track", lambda **_: audio
    )
    monkeypatch.setattr(handover_screen, "NO_SPEECH_SECONDS", 0.05)
    monkeypatch.setattr(handover_screen, "END_SILENCE_SECONDS", 0.05)
    monkeypatch.setattr(handover_screen, "MAX_LISTEN_SECONDS", 2.0)
    return audio


def _vad(script):
    stream = _VADStream(script)
    return SimpleNamespace(stream=lambda: stream), stream


async def test_silent_pickup_returns_no_audio(leg) -> None:
    """A person who answers and waits hears the intro after the silence."""
    vad, stream = _vad([])
    pcm = await handover_screen.listen_for_greeting(object(), "human-x", vad)
    assert pcm == b""
    assert leg.closed and stream.closed


async def test_greeting_ends_after_silence(leg) -> None:
    """Speech, then silence: the greeting so far comes back for classifying."""
    vad, _ = _vad(
        [VADEventType.START_OF_SPEECH]
        + [VADEventType.INFERENCE_DONE] * 3
        + [VADEventType.END_OF_SPEECH]
    )
    pcm = await asyncio.wait_for(
        handover_screen.listen_for_greeting(object(), "human-x", vad), timeout=1.5
    )
    assert len(pcm) >= 5 * 640


async def test_pause_inside_greeting_keeps_listening(leg) -> None:
    """A short pause, then more speech, is still one greeting."""
    vad, _ = _vad(
        [VADEventType.START_OF_SPEECH, VADEventType.END_OF_SPEECH]
        + [VADEventType.START_OF_SPEECH]
        + [VADEventType.INFERENCE_DONE] * 200
    )
    # END_SILENCE_SECONDS would end it after the first END_OF_SPEECH if the
    # next START_OF_SPEECH did not reset the silence.
    pcm = await handover_screen.listen_for_greeting(object(), "human-x", vad)
    # It listened to the cap: MAX_LISTEN_SECONDS of 16 kHz 16-bit audio.
    assert len(pcm) == 2 * 16000 * 2


def _stt(text):
    return SimpleNamespace(
        recognize=AsyncMock(
            return_value=SpeechEvent(
                type=SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[SpeechData(text=text, language="")],
            )
        )
    )


async def test_classify_voicemail(monkeypatch) -> None:
    seen: list[str] = []

    async def classify(_llm, prompt, transcript):
        seen.append(transcript)
        assert "complete greeting" in prompt
        return AMDCategory.MACHINE_VM

    monkeypatch.setattr(pickup, "classify_speech", classify)
    text = "Bonjour, vous êtes sur la messagerie de Rym, laissez un message."
    verdict = await handover_screen.classify_greeting(
        b"\x01\x00" * 16000, stt=_stt(text), llm=object()
    )
    assert verdict == "machine-vm" and seen == [text]
    assert verdict in handover_screen.NOT_A_PERSON


async def test_classify_skips_empty_or_tiny_audio(monkeypatch) -> None:
    monkeypatch.setattr(
        pickup, "classify_speech", AsyncMock(side_effect=AssertionError)
    )
    assert (
        await handover_screen.classify_greeting(b"", stt=_stt("x"), llm=object())
        is None
    )
    assert (
        await handover_screen.classify_greeting(
            b"\x01\x00" * 16000, stt=_stt(""), llm=object()
        )
        is None
    )


def test_person_verdicts_connect() -> None:
    assert "human" not in handover_screen.NOT_A_PERSON
    assert "uncertain" not in handover_screen.NOT_A_PERSON


def test_transcriber_follows_the_org_stt(monkeypatch) -> None:
    """Audio goes only to a provider the org allows."""
    monkeypatch.setattr(handover_screen.settings, "deepgram_api_key", "dg")
    monkeypatch.setattr(handover_screen.settings, "deepgram_model", "nova-3")
    house = handover_screen._build_stt({})
    assert type(house).__name__ == "STT" and house.model == "nova-3"
    assert house._opts.detect_language is True

    org_dg = SimpleNamespace(
        provider="deepgram", params={"model": "nova-2"}, api_key="org-key"
    )
    org = handover_screen._build_stt({"stt": org_dg})
    assert org.model == "nova-2" and org._opts.detect_language is True

    keyless = SimpleNamespace(
        provider="deepgram", params={}, api_key=None, fallback_enabled=False
    )
    assert handover_screen._build_stt({"stt": keyless}) is None
    keyless.fallback_enabled = True
    assert handover_screen._build_stt({"stt": keyless}).model == "nova-3"

    org_sm = SimpleNamespace(provider="speechmatics")
    assert handover_screen._build_stt({"stt": org_sm}) is None

    monkeypatch.setattr(handover_screen.settings, "deepgram_api_key", "")
    assert handover_screen._build_stt({}) is None
