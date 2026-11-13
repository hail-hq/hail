"""Repeated waveform echo must earn a warning; similar words are not evidence."""

import time
from unittest.mock import Mock

import numpy as np
import pytest
from hailhq.voicebot.echo_warning import WINDOW, EchoDetector, EchoWarning

from .test_recovery import Session as RecoverySession


class Session(RecoverySession):
    def __init__(self):
        from livekit.agents import Agent

        super().__init__()
        self.current_agent = Agent(instructions="test")
        self.user_state = "listening"
        self.say = Mock()


def disturb(session):
    """The agent was cut off twice: its own input reacted to returning audio."""
    session.emit("agent_false_interruption")
    session.emit("agent_false_interruption")


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
    warning = EchoWarning(session, line="Echo line.", can_warn=lambda: allowed[0])
    ref = speech(5)
    incoming = np.zeros_like(ref)
    incoming[6400:] = ref[:-6400] * 0.5
    warning.detector.add_input(incoming, 0)
    warning.detector.add_played(ref, 0)
    await warning.check(6)
    session.say.assert_not_called()
    allowed[0] = True
    session.agent_state = "speaking"
    await warning.check(6)
    session.say.assert_not_called()
    session.agent_state = "listening"
    # Raw echo that never reached the agent's own input is not worth a warning.
    await warning.check(6)
    session.say.assert_not_called()
    session.emit("agent_false_interruption")
    await warning.check(6)
    session.say.assert_not_called()  # one reaction can be the caller
    session.emit("agent_false_interruption")
    session.user_state = "speaking"
    await warning.check(6)
    session.say.assert_not_called()  # never over the caller
    session.user_state = "listening"
    await warning.check(6)
    await warning.check(7)
    session.say.assert_called_once()
    session.clear_user_turn.assert_called_once()
    # The fixed line, not a model reply, and it cannot be cut off by its echo.
    assert session.say.call_args.args == ("Echo line.",)
    assert session.say.call_args.kwargs == {"allow_interruptions": False}
    # Later turns are protected by the input gate, not by model instructions.
    assert not session.current_agent.chat_ctx.messages()
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
    warning = EchoWarning(session, line="Echo line.", can_warn=lambda: allowed[0])
    ref = speech(1)
    received = np.zeros_like(ref)
    received[6400:] = ref[:-6400] * 0.5
    warning.detector.add_input(received, 0)
    warning.detector.add_played(ref, 0)
    await warning.check(6)
    allowed[0] = True
    disturb(session)
    await warning.check(30)
    session.say.assert_not_called()
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
    warning = EchoWarning(session, line="Echo line.", can_warn=lambda: True)
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
    warning = EchoWarning(session, line="Echo line.", can_warn=lambda: True)
    session.emit("close")
    await warning.check(100)
    session.say.assert_not_called()
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


@pytest.mark.asyncio
async def test_long_reply_keeps_recent_audio():
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

        async def capture_frame(self, frame):
            await super().capture_frame(frame)

        def flush(self):
            super().flush()

        def clear_buffer(self):
            pass

    detector = EchoDetector()
    sink = Sink()
    output = EchoOutput(sink, detector)
    raw = speech(1, seconds=20).astype("<i2")
    await output.capture_frame(rtc.AudioFrame(raw.tobytes(), RATE, 1, len(raw)))
    # One confirmation at the end of a 20 s reply that began at t=100.
    sink.on_playback_progressed(started_at=100, offset=0, duration=20)
    assert len(detector.references) == 24  # the latest 12 s
    at, first = detector.references[0]
    assert at == pytest.approx(108)
    assert np.array_equal(first, raw[8 * RATE : 8 * RATE + WINDOW].astype(float))


class _Sink:
    """Built lazily so the module imports without the SDK loaded first."""

    def __new__(cls):
        from livekit.agents.voice import io

        class Sink(io.AudioOutput):
            def __init__(self):
                super().__init__(
                    label="sink",
                    sample_rate=RATE,
                    capabilities=io.AudioOutputCapabilities(pause=True),
                )

            async def capture_frame(self, frame):
                await super().capture_frame(frame)

            def flush(self):
                super().flush()

            def clear_buffer(self):
                pass

        return Sink()


def gated_call():
    from types import SimpleNamespace

    from livekit import rtc
    from livekit.agents.voice import io

    loud = rtc.AudioFrame(np.full(400, 5000, "<i2").tobytes(), RATE, 1, 400)

    class Source(io.AudioInput):
        def __init__(self):
            super().__init__(label="source")

        async def __anext__(self):
            return loud

    session = Session()
    sink = _Sink()
    session.output = SimpleNamespace(audio=sink)
    session.input = SimpleNamespace(audio=Source(), audio_enabled=True)
    session.room_io = SimpleNamespace(linked_participant=None)
    warning = EchoWarning(session, line="Echo line.", can_warn=lambda: True)
    warning.attach()
    return session, sink, warning, loud


def feed_echo(warning, start=0.0, seed=5):
    ref = speech(seed)
    incoming = np.zeros_like(ref)
    incoming[6400:] = ref[:-6400] * 0.5
    warning.detector.add_input(incoming, start)
    warning.detector.add_played(ref, start)


@pytest.mark.asyncio
async def test_confirmed_disturbing_echo_silences_input_around_agent_speech():
    from hailhq.voicebot.echo_warning import QUIET_WINDOWS, EchoGate

    session, sink, warning, loud = gated_call()
    gate, output = session.input.audio, session.output.audio
    assert isinstance(gate, EchoGate)

    async def heard():
        return bool(np.any(np.asarray((await gate.__anext__()).data)))

    # Before any warning the caller is always heard, even over agent speech.
    await output.capture_frame(loud)
    assert await heard()
    # Speech detected twice while the agent itself plays: the call is disturbed.
    session.emit("user_state_changed", new_state="speaking")
    session.emit("user_state_changed", new_state="speaking")
    feed_echo(warning)
    await warning.check(6)
    session.say.assert_called_once()
    done = session.say.return_value.add_done_callback.call_args.args[0]
    # The gate waits until the warning was really spoken.
    assert output.gate_delay is None
    await output.capture_frame(loud)
    done(None)
    assert output.gate_delay == pytest.approx(0.8, abs=0.02)

    assert not await heard()  # agent speaking
    sink.on_playback_finished(playback_position=0.05, interrupted=False)
    assert not await heard()  # its voice is still on the way back
    output._finished_at = time.time() - 2
    assert await heard()  # the caller's reply
    # A lost finish event must not leave the agent deaf.
    await output.capture_frame(loud)
    output._expected_end = time.time() - 5
    assert await heard()

    # Echo gone (speakerphone off): enough clean agent audio lifts the gate.
    warning.detector.evidence = None
    warning.detector.scored += QUIET_WINDOWS
    await warning.check(30)
    assert output.gate_delay is None
    # Echo back: the gate returns without a second warning.
    feed_echo(warning, start=40.0, seed=6)
    for step in range(12):
        await warning.check(43 + step * 0.5)
    assert output.gate_delay == pytest.approx(0.8, abs=0.02)
    session.say.assert_called_once()
    session.emit("close")
    assert output.gate_delay is None
    await warning.aclose()


@pytest.mark.asyncio
async def test_warning_that_was_never_spoken_leaves_the_call_ungated():
    session, _, warning, _ = gated_call()
    disturb(session)
    feed_echo(warning)
    await warning.check(6)
    done = session.say.return_value.add_done_callback.call_args.args[0]
    done(None)  # the reply failed before any audio
    assert session.output.audio.gate_delay is None
    await warning.aclose()


@pytest.mark.asyncio
async def test_quick_reply_after_agent_speech_is_not_a_disturbance():
    session, _, warning, _ = gated_call()
    session.output.audio._finished_at = time.time() - 0.5
    session.emit("user_state_changed", new_state="speaking")
    session.emit("user_state_changed", new_state="speaking")
    assert not warning._is_disturbed()
    await warning.aclose()


@pytest.mark.asyncio
async def test_raw_frames_are_timed_by_sample_count_through_a_stall(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from livekit import rtc

    clock = iter([100.05, 100.28, 100.28, 100.28])  # three frames arrive bunched

    class RawStream:
        def __init__(self):
            self.left = 4

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.left:
                await asyncio.Event().wait()
            self.left -= 1
            return SimpleNamespace(frame=rtc.AudioFrame(bytes(800), RATE, 1, 400))

        async def aclose(self):
            pass

    monkeypatch.setattr(rtc.AudioStream, "from_participant", lambda **_: RawStream())
    session = Session()
    session.room_io = SimpleNamespace(linked_participant=object())
    session.input = SimpleNamespace(audio_enabled=True)
    warning = EchoWarning(session, line="Echo line.", can_warn=lambda: True)
    from hailhq.voicebot import echo_warning

    fake = SimpleNamespace(time=lambda: next(clock, time.time()))
    monkeypatch.setattr(echo_warning, "time", fake)
    warning._raw_task = asyncio.create_task(warning._observe_raw())
    await asyncio.sleep(0.01)
    starts = [at for at, _ in warning.detector.inputs]
    assert starts == pytest.approx([100.0, 100.05, 100.10, 100.15])
    monkeypatch.undo()
    await warning.aclose()


def test_every_language_has_a_dash_free_echo_line():
    from hailhq.voicebot.spoken_lines import SPOKEN_LINES

    for language, lines in SPOKEN_LINES.items():
        assert lines.echo.strip(), language
        assert "\u2014" not in lines.echo and "\u2013" not in lines.echo, language
    assert SPOKEN_LINES["en"].echo == "I'm hearing an echo. It's disrupting the call."
