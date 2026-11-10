"""Repeated waveform echo must earn a warning; similar words are not evidence."""

import numpy as np
import pytest
from hailhq.voicebot.echo_warning import WINDOW, EchoDetector, EchoWarning

from .test_recovery import Session as RecoverySession


class Session(RecoverySession):
    def __init__(self):
        from livekit.agents import Agent

        super().__init__()
        self.current_agent = Agent(instructions="test")


RATE = 8000


def replay(reference, incoming):
    detector = EchoDetector()
    evidence = None
    for i in range(0, len(incoming), 400):
        t = i / RATE
        detector.add_input(incoming[i : i + 400], t)
        detector.add_played(reference[i : i + 400], t)
        evidence = detector.check(t + 0.05) or evidence
    return evidence


def speech(seed, seconds=6):
    rng = np.random.default_rng(seed)
    return np.convolve(rng.normal(0, 3000, RATE * seconds), np.ones(4) / 4, "same")


def test_detects_repeated_delayed_echo():
    ref = speech(1)
    incoming = speech(2) * 0.1
    incoming[6400:] += ref[:-6400] * 0.5
    result = replay(ref, incoming)
    assert result is not None
    assert abs(result.delay_ms - 800) < 20
    assert result.matches >= 3


@pytest.mark.parametrize("kind", ["silence", "independent", "brief"])
def test_does_not_warn_on_silence_independent_speech_or_one_short_match(kind):
    ref = speech(3)
    incoming = np.zeros_like(ref) if kind == "silence" else speech(4)
    if kind == "brief":
        incoming[8000:9000] = ref[1600:2600] * 0.5
    assert replay(ref, incoming) is None


@pytest.mark.asyncio
async def test_warning_is_deferred_and_spoken_only_once():
    session = Session()
    allowed = [False]
    warning = EchoWarning(session, can_warn=lambda: allowed[0])
    ref = speech(5)
    incoming = np.zeros_like(ref)
    incoming[6400:] = ref[:-6400] * 0.5
    warning.detector.add_input(incoming, 0)
    warning.detector.add_played(ref, 0)
    await warning.check(6)
    session.generate_reply.assert_not_called()
    allowed[0] = True
    session.agent_state = "speaking"
    await warning.check(6)
    session.generate_reply.assert_not_called()
    session.agent_state = "listening"
    await warning.check(6)
    await warning.check(7)
    session.generate_reply.assert_called_once()
    kwargs = session.generate_reply.call_args.kwargs
    assert "echo" in kwargs["instructions"]
    assert "disrupting the call" in kwargs["instructions"]
    assert "speakerphone" not in kwargs["instructions"]
    assert "headphones" not in kwargs["instructions"]
    assert kwargs["allow_interruptions"] is False
    assert kwargs["tool_choice"] == "none"
    # The next turn must also know that echo was confirmed and already warned.
    context = session.current_agent.chat_ctx.copy()
    assert any(
        item.role == "system" and '"echo_only":true' in (item.text_content or "")
        for item in context.messages()
    )
    await warning.aclose()


def test_stationary_tone_does_not_count_as_echoed_voice():
    t = np.arange(RATE * 6) / RATE
    ref = np.sin(2 * np.pi * 440 * t) * 3000
    assert replay(ref, np.roll(ref, 6400)) is None


def test_buffers_are_bounded_and_stop_after_close():
    detector = EchoDetector()
    for i in range(1000):
        detector.add_input(np.ones(400), i * 0.05)
        detector.add_played(np.ones(400), i * 0.05)
    assert len(detector.inputs) <= 403
    assert len(detector.references) <= 80
    detector.clear()
    detector.add_input(np.ones(400), 100)
    detector.add_played(np.ones(WINDOW), 100)
    assert not detector.inputs
    assert not detector.references


@pytest.mark.asyncio
async def test_stale_echo_does_not_warn_after_a_long_bridge_or_menu():
    session = Session()
    allowed = [False]
    warning = EchoWarning(session, can_warn=lambda: allowed[0])
    ref = speech(1)
    received = np.zeros_like(ref)
    received[6400:] = ref[:-6400] * 0.5
    warning.detector.add_input(received, 0)
    warning.detector.add_played(ref, 0)
    await warning.check(6)
    allowed[0] = True
    await warning.check(30)
    session.generate_reply.assert_not_called()
    await warning.aclose()


@pytest.mark.asyncio
async def test_only_confirmed_playback_is_used_and_audio_is_unchanged():
    from hailhq.voicebot.echo_warning import EchoOutput
    from livekit import rtc
    from livekit.agents.voice import io

    class Sink(io.AudioOutput):
        def __init__(self):
            super().__init__(
                label="sink",
                sample_rate=RATE,
                capabilities=io.AudioOutputCapabilities(pause=True),
            )
            self.frames = []

        async def capture_frame(self, frame):
            await super().capture_frame(frame)
            self.frames.append(frame)

        def flush(self):
            super().flush()

        def clear_buffer(self):
            self.on_playback_progressed(started_at=0, offset=0, duration=1.5)
            self.on_playback_finished(playback_position=1.5, interrupted=True)

    detector = EchoDetector()
    sink = Sink()
    output = EchoOutput(sink, detector)
    raw = speech(1, seconds=2).astype("<i2")
    original = rtc.AudioFrame(raw.tobytes(), RATE, 1, len(raw))
    await output.capture_frame(original)
    assert sink.frames[0] is original
    assert np.array_equal(np.asarray(original.data), raw)
    assert not detector.references
    output.clear_buffer()
    assert len(detector.references) == 3  # the other 0.5 s never played
    assert not output._data


@pytest.mark.asyncio
async def test_raw_sidecar_bypasses_filter_and_closes_with_call(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from livekit import rtc

    stopped = asyncio.Event()
    arguments = {}

    class RawStream:
        sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.sent:
                self.sent = True
                return SimpleNamespace(frame=rtc.AudioFrame(bytes(800), RATE, 1, 400))
            await asyncio.Event().wait()

        async def aclose(self):
            stopped.set()

    def factory(**kwargs):
        arguments.update(kwargs)
        return RawStream()

    monkeypatch.setattr(rtc.AudioStream, "from_participant", factory)
    session = Session()
    session.room_io = SimpleNamespace(linked_participant=object())
    session.input = SimpleNamespace(audio_enabled=False)
    warning = EchoWarning(session, can_warn=lambda: True)
    warning._raw_task = asyncio.create_task(warning._observe_raw())
    await asyncio.sleep(0.01)
    assert arguments["noise_cancellation"] is None
    assert arguments["sample_rate"] == RATE
    assert arguments["capacity"] == 20
    assert not warning.detector.inputs  # handover mute also covers the observer
    await warning.aclose()
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_close_prevents_pending_warning():
    session = Session()
    warning = EchoWarning(session, can_warn=lambda: True)
    session.emit("close")
    await warning.check(100)
    session.generate_reply.assert_not_called()
    assert not warning.detector.active
    await warning.aclose()


@pytest.mark.parametrize("seed", range(20))
def test_unrelated_voiced_speech_does_not_trigger_on_similar_pitch(seed):
    t = np.arange(RATE * 6) / RATE

    def voice(k):
        rng = np.random.default_rng(k)
        pitch = (
            130
            + rng.uniform(-30, 30)
            + 20 * np.sin(2 * np.pi * rng.uniform(1, 3) * t + rng.uniform(0, 6))
        )
        phase = np.cumsum(pitch) / RATE * 2 * np.pi
        wave = (
            sum(np.sin(h * phase + rng.uniform(0, 6)) / h for h in range(1, 8)) * 2000
        )
        return wave * (0.2 + 0.8 * np.sin(2 * np.pi * rng.uniform(1, 3) * t) ** 2)

    assert replay(voice(seed), voice(seed + 100)) is None
