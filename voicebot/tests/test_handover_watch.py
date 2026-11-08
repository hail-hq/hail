"""The handover silence check: did anyone on the contact's side speak?"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.voicebot import handover_watch
from livekit import rtc
from livekit.agents.vad import VADEventType


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
    monkeypatch.setattr(
        handover_watch,
        "wait_for_track_publication",
        AsyncMock(return_value=SimpleNamespace(track=object())),
    )
    monkeypatch.setattr(handover_watch.rtc.AudioStream, "from_track", lambda **_: audio)
    return audio


def _vad(script):
    stream = _VADStream(script)
    return SimpleNamespace(stream=lambda: stream), stream


async def test_speech_counts(leg) -> None:
    vad, stream = _vad(
        [VADEventType.INFERENCE_DONE] * 3 + [VADEventType.START_OF_SPEECH]
    )
    assert await handover_watch.contact_speaks(object(), "human-x", vad, 2.0)
    assert leg.closed and stream.closed


async def test_silence_until_the_deadline(leg) -> None:
    vad, stream = _vad([])
    assert not await handover_watch.contact_speaks(object(), "human-x", vad, 0.05)
    assert leg.closed and stream.closed


async def test_no_audio_track_is_silent(monkeypatch) -> None:
    monkeypatch.setattr(
        handover_watch,
        "wait_for_track_publication",
        AsyncMock(return_value=SimpleNamespace(track=None)),
    )
    vad, _ = _vad([VADEventType.START_OF_SPEECH])
    assert not await handover_watch.contact_speaks(object(), "human-x", vad, 1.0)


async def test_deadline_includes_waiting_for_the_track(monkeypatch) -> None:
    """A leg that never publishes audio still ends the check on time."""

    async def never(**_):
        await asyncio.sleep(3600)

    monkeypatch.setattr(handover_watch, "wait_for_track_publication", never)
    vad, _ = _vad([])
    assert not await asyncio.wait_for(
        handover_watch.contact_speaks(object(), "human-x", vad, 0.05), timeout=1
    )
