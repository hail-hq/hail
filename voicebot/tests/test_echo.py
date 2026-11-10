"""Synthetic duplex audio: no customer audio or external providers."""

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from hailhq.voicebot import echo
from livekit import rtc
from livekit.agents.voice import io

RATE = 24000
SIZE = RATE // 100


def frame(samples):
    samples = np.asarray(samples, dtype=np.int16)
    return rtc.AudioFrame(samples.tobytes(), RATE, 1, len(samples))


def signal(seed, seconds=6):
    rng = np.random.default_rng(seed)
    return np.convolve(rng.normal(0, 4000, RATE * seconds), np.ones(5) / 5, "same")


def run_filter(reference, incoming):
    processor = echo.EchoFilter()
    result = []
    for i in range(0, len(incoming), SIZE):
        processor.render(frame(reference[i : i + SIZE]))
        result.extend(processor.capture(frame(incoming[i : i + SIZE])).data)
    return np.asarray(result, dtype=float)


@pytest.mark.parametrize("delay_ms", [80, 200, 400])
def test_cancels_delayed_echo_and_reflections(delay_ms):
    outgoing = signal(1)
    incoming = np.zeros(len(outgoing))
    for delay, gain in [(delay_ms, 0.6), (delay_ms + 45, 0.2), (delay_ms + 110, 0.1)]:
        offset = RATE * delay // 1000
        incoming[offset:] += outgoing[:-offset] * gain
    cleaned = run_filter(outgoing, incoming)
    tail = slice(RATE * 2, None)
    reduction_db = 10 * np.log10(
        np.mean(incoming[tail] ** 2) / max(np.mean(cleaned[tail] ** 2), 1)
    )
    assert reduction_db > 20


def test_preserves_caller_talking_over_agent():
    outgoing = signal(2)
    t = np.arange(RATE * 6) / RATE
    phase = np.cumsum(130 + 20 * np.sin(2 * np.pi * 2.3 * t)) / RATE * 2 * np.pi
    caller = sum(np.sin(phase * k) / k for k in range(1, 9)) * 2000
    caller *= 0.2 + 0.8 * np.sin(2 * np.pi * 2 * t) ** 2
    caller[: RATE * 3] = 0  # allow the echo path to settle before double talk
    incoming = caller.copy()
    incoming[4800:] += outgoing[:-4800] * 0.6
    cleaned = run_filter(outgoing, incoming)
    tail = slice(RATE * 4, None)
    # Preserve independent speech, rather than muting while the agent speaks.
    # AEC's sub-band filter adds latency/phase rotation: align within 20 ms.
    start = RATE * 4
    correlation = max(
        np.corrcoef(
            cleaned[start + lag : start + 5000 + lag], caller[start : start + 5000]
        )[0, 1]
        for lag in range(RATE // 50)
    )
    assert correlation > 0.6
    assert np.mean(cleaned[tail] ** 2) > np.mean(caller[tail] ** 2) * 0.4


def test_failure_passes_original_audio_and_disables_processor():
    processor = echo.EchoFilter()
    processor._apm = SimpleNamespace(
        process_stream=lambda _: (_ for _ in ()).throw(RuntimeError("failed"))
    )
    original = frame([500] * SIZE)
    assert bytes(processor.capture(original).data) == bytes(original.data)
    assert processor.disabled


class Sink(io.AudioOutput):
    def __init__(self):
        super().__init__(
            label="test",
            capabilities=io.AudioOutputCapabilities(pause=True),
            sample_rate=RATE,
        )
        self.frames = []
        self.cleared = False

    async def capture_frame(self, value):
        await super().capture_frame(value)
        self.frames.append(value)

    def flush(self):
        super().flush()

    def clear_buffer(self):
        self.cleared = True


@pytest.mark.asyncio
async def test_reference_is_paced_and_output_remains_unchanged():
    sink = Sink()
    processor = echo.EchoFilter()
    wrapper = echo.EchoOutput(sink, processor)
    original = frame(np.arange(SIZE * 6))
    loop = asyncio.get_running_loop()
    started = loop.time()
    await wrapper.capture_frame(original)
    assert loop.time() - started >= 0.045
    assert b"".join(bytes(f.data) for f in sink.frames) == bytes(original.data)
    assert all(f.samples_per_channel == SIZE for f in sink.frames)


@pytest.mark.asyncio
async def test_interruption_discards_unplayed_reference():
    sink = Sink()
    wrapper = echo.EchoOutput(sink, echo.EchoFilter())
    task = asyncio.create_task(wrapper.capture_frame(frame([500] * SIZE * 30)))
    await asyncio.sleep(0.025)
    wrapper.clear_buffer()
    await task
    assert sink.cleared
    assert len(sink.frames) < 10


@pytest.mark.asyncio
async def test_pause_holds_output_until_resume():
    sink = Sink()
    wrapper = echo.EchoOutput(sink, echo.EchoFilter())
    wrapper.pause()
    task = asyncio.create_task(wrapper.capture_frame(frame([500] * SIZE)))
    await asyncio.sleep(0.025)
    assert not sink.frames
    wrapper.resume()
    await task
    assert len(sink.frames) == 1


@pytest.mark.asyncio
async def test_input_is_cleaned_before_it_reaches_session_consumers():
    class Source(io.AudioInput):
        def __init__(self):
            super().__init__(label="test")
            self.done = False

        async def __anext__(self):
            if self.done:
                raise StopAsyncIteration
            self.done = True
            return frame(np.arange(SIZE * 5))

    class Processor:
        def capture(self, value):
            return frame([42] * value.samples_per_channel)

    wrapper = echo.EchoInput(Source(), Processor())
    results = [value async for value in wrapper]
    assert len(results) == 5
    assert all(set(value.data) == {42} for value in results)


def test_disabled_trial_leaves_session_audio_unchanged(monkeypatch):
    monkeypatch.setattr(echo.settings, "hail_echo_cancellation_enabled", False)
    source, sink = object(), object()
    session = SimpleNamespace(
        input=SimpleNamespace(audio=source), output=SimpleNamespace(audio=sink)
    )
    echo.attach_echo_filter(session)
    assert session.input.audio is source
    assert session.output.audio is sink


def test_enabled_trial_wraps_both_directions(monkeypatch):
    monkeypatch.setattr(echo.settings, "hail_echo_cancellation_enabled", True)
    source = io.AudioInput(label="test")
    session = SimpleNamespace(
        input=SimpleNamespace(audio=source), output=SimpleNamespace(audio=Sink())
    )
    echo.attach_echo_filter(session)
    assert isinstance(session.input.audio, echo.EchoInput)
    assert isinstance(session.output.audio, echo.EchoOutput)


@pytest.mark.asyncio
async def test_output_forwards_playback_events():
    sink = Sink()
    wrapper = echo.EchoOutput(sink, echo.EchoFilter())
    events = []
    wrapper.on("playback_finished", events.append)
    await wrapper.capture_frame(frame([500] * SIZE))
    wrapper.flush()
    sink.on_playback_finished(playback_position=0.01, interrupted=False)
    assert len(events) == 1
    assert events[0].playback_position == 0.01


@pytest.mark.asyncio
async def test_paced_duplex_pipeline_removes_echo_with_batched_network_input():
    """Exercise the wrappers together, including 50 ms RoomIO input bursts."""
    outgoing = signal(7, seconds=4)
    incoming = np.zeros(len(outgoing))
    incoming[4800:] = outgoing[:-4800] * 0.6
    loop = asyncio.get_running_loop()
    started = loop.time()

    class Source(io.AudioInput):
        def __init__(self):
            super().__init__(label="network")
            self.offset = 0

        async def __anext__(self):
            if self.offset >= len(incoming):
                raise StopAsyncIteration
            end = self.offset + SIZE * 5
            await asyncio.sleep(max(0, started + end / RATE - loop.time()))
            result = frame(incoming[self.offset : end])
            self.offset = end
            return result

    processor = echo.EchoFilter()
    output = echo.EchoOutput(Sink(), processor)
    source = echo.EchoInput(Source(), processor)

    async def receive():
        return np.concatenate([np.asarray(f.data, dtype=float) async for f in source])

    _, cleaned = await asyncio.gather(output.capture_frame(frame(outgoing)), receive())
    tail = slice(RATE * 2, None)
    reduction = 10 * np.log10(
        np.mean(incoming[tail] ** 2) / max(np.mean(cleaned[tail] ** 2), 1)
    )
    assert reduction > 15
