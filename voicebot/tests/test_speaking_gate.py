"""The agent is deaf while its voice is on the line, and hears again after."""

import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest
from hailhq.voicebot import speaking_gate
from hailhq.voicebot.speaking_gate import (
    CARRY,
    QUIET_WINDOWS,
    TAIL,
    WINDOW,
    EchoDetector,
    Playout,
    SpeakingGuard,
    interruption_model_live,
    listening_through,
)
from livekit import rtc
from livekit.agents.voice import io
from livekit.agents.voice.agent_activity import AgentActivity

from .test_recovery import Session

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


def echo_of(ref, delay_samples=6400):
    incoming = np.zeros_like(ref)
    incoming[delay_samples:] = ref[:-delay_samples] * 0.5
    return incoming


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
        pass


class Source(io.AudioInput):
    def __init__(self, frame):
        super().__init__(label="source")
        self.frame = frame

    async def __anext__(self):
        return self.frame


def call(enabled=lambda: True, on_echo=None, skip_replies=0):
    loud = rtc.AudioFrame(np.full(400, 5000, "<i2").tobytes(), RATE, 1, 400)
    session = Session()
    sink = Sink()
    session.output = SimpleNamespace(audio=sink)
    session.input = SimpleNamespace(audio=Source(loud), audio_enabled=True)
    session.room_io = SimpleNamespace(linked_participant=None)
    session.user_state = "listening"
    guard = SpeakingGuard(
        session, enabled=enabled, skip_replies=skip_replies, on_echo=on_echo
    )
    guard.attach()
    return session, sink, guard, loud


def agent_frame(seconds=1.0):
    raw = speech(9, seconds=1)[: int(RATE * seconds)].astype("<i2")
    return rtc.AudioFrame(raw.tobytes(), RATE, 1, len(raw))


def silent(frame):
    return not np.asarray(frame.data).any()


def test_detects_repeated_delayed_echo():
    ref = speech(1)
    incoming = speech(2) * 0.1 + echo_of(ref)
    result = replay(ref, incoming)
    assert result is not None
    assert abs(result.delay_ms - 800) < 20
    assert result.matches >= 3


@pytest.mark.parametrize("kind", ["silence", "independent", "brief"])
def test_no_echo_on_silence_independent_speech_or_one_short_match(kind):
    ref = speech(3)
    incoming = np.zeros_like(ref) if kind == "silence" else speech(4)
    if kind == "brief":
        incoming[8000:9000] = ref[1600:2600] * 0.5
    assert replay(ref, incoming) is None


def test_stationary_tone_does_not_count_as_echoed_voice():
    t = np.arange(RATE * 6) / RATE
    ref = np.sin(2 * np.pi * 440 * t) * 3000
    assert replay(ref, np.roll(ref, 6400)) is None


@pytest.mark.parametrize("seed", [1, 2, 3])
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


async def test_agent_audio_passes_unchanged_and_only_played_audio_is_compared():
    detector = EchoDetector()
    sink = Sink()
    playout = Playout(sink, detector)
    raw = speech(1, seconds=2).astype("<i2")
    original = rtc.AudioFrame(raw.tobytes(), RATE, 1, len(raw))
    await playout.capture_frame(original)
    assert sink.frames[0] is original
    assert np.array_equal(np.asarray(original.data), raw)
    assert not detector.references
    sink.on_playback_progressed(started_at=0, offset=0, duration=1.5)
    sink.on_playback_finished(playback_position=1.5, interrupted=True)
    assert len(detector.references) == 3  # the other 0.5 s never played
    assert not playout._data


async def test_input_is_silent_while_the_agent_speaks_and_heard_right_after():
    session, sink, guard, loud = call()
    gate = session.input.audio
    assert await gate.__anext__() is loud  # nothing playing: the caller is heard

    await session.output.audio.capture_frame(agent_frame())
    heard = await gate.__anext__()
    assert silent(heard) and heard.samples_per_channel == loud.samples_per_channel

    sink.on_playback_finished(playback_position=1.0, interrupted=False)
    # No echo measured: a quick "yes" right after the agent stops is heard.
    assert await gate.__anext__() is loud
    await guard.aclose()


async def test_measured_echo_keeps_the_input_silent_for_its_delay_after_a_reply():
    reported = []

    async def on_echo(payload):
        reported.append(payload)

    session, sink, guard, loud = call(on_echo=on_echo)
    gate = session.input.audio
    ref = speech(5)
    guard.detector.add_input(echo_of(ref), 0.0)
    guard.detector.add_played(ref, 0.0)
    await guard.check(now=6.0)
    await guard.check(now=6.5)  # reported once
    assert guard.playout.echo_delay == pytest.approx(0.8, abs=0.02)
    assert len(reported) == 1 and abs(reported[0]["delay_ms"] - 800) < 20

    await session.output.audio.capture_frame(agent_frame())
    sink.on_playback_finished(playback_position=1.0, interrupted=False)
    assert silent(await gate.__anext__())  # the reply's echo is still returning
    guard.playout._finished_at = time.time() - (0.8 + TAIL + 0.05)
    assert await gate.__anext__() is loud
    await guard.aclose()


async def test_gate_is_off_during_a_phone_menu_and_fails_open():
    menu = [True]
    session, _sink, guard, loud = call(enabled=lambda: not menu[0])
    gate = session.input.audio
    await session.output.audio.capture_frame(agent_frame())
    assert await gate.__anext__() is loud  # the menu stays heard
    menu[0] = False
    assert silent(await gate.__anext__())

    def broken():
        raise RuntimeError("boom")

    gate._enabled = broken
    assert await gate.__anext__() is loud  # a failing check never costs input
    await guard.aclose()


async def test_a_lost_finish_event_cannot_leave_the_agent_deaf():
    session, _sink, guard, _loud = call()
    await session.output.audio.capture_frame(agent_frame(0.5))
    playout = guard.playout
    assert playout.muted(time.time())
    assert not playout.muted(time.time() + 0.5 + 1.01)
    await guard.aclose()


async def test_raw_reader_skips_the_filter_and_closes_with_the_call(monkeypatch):
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
    guard = SpeakingGuard(session, enabled=lambda: True)
    guard._raw_task = asyncio.create_task(guard._observe_raw())
    await asyncio.sleep(0.01)
    assert arguments["noise_cancellation"] is None
    assert arguments["sample_rate"] == RATE
    assert not guard.detector.inputs  # handover mute also covers the reader
    await guard.aclose()
    assert stopped.is_set()


async def test_raw_frames_are_timed_by_sample_count_through_a_stall(monkeypatch):
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
    guard = SpeakingGuard(session, enabled=lambda: True)
    fake = SimpleNamespace(time=lambda: next(clock, time.time()))
    monkeypatch.setattr(speaking_gate, "time", fake)
    guard._raw_task = asyncio.create_task(guard._observe_raw())
    await asyncio.sleep(0.01)
    starts = [at for at, _ in guard.detector.inputs]
    assert starts == pytest.approx([100.0, 100.05, 100.10, 100.15])
    monkeypatch.undo()
    await guard.aclose()


async def test_with_the_interruption_model_working_only_echo_calls_are_gated():
    session, _sink, guard, loud = call()
    gate = session.input.audio
    session._activity = SimpleNamespace(_interruption_detection_enabled=True)
    await session.output.audio.capture_frame(agent_frame())
    assert await gate.__anext__() is loud  # the model decides; input stays live
    guard.playout.echo_delay = 0.8
    assert silent(await gate.__anext__())
    guard.playout.echo_delay = None
    # The model was refused or failed: the gate takes over.
    session._activity._interruption_detection_enabled = False
    assert silent(await gate.__anext__())
    await guard.aclose()


def test_the_sdk_still_has_the_flag_the_gate_reads():
    assert "_interruption_detection_enabled" in AgentActivity.__init__.__code__.co_names
    assert interruption_model_live(object()) is False  # unknown counts as off


async def test_a_paused_reply_is_not_on_the_line():
    session, _sink, guard, loud = call()
    gate = session.input.audio
    await session.output.audio.capture_frame(agent_frame())
    playout = guard.playout
    end = playout._expected_end
    playout.pause()
    assert await gate.__anext__() is loud
    playout._paused_at -= 2.0  # two seconds pass while paused
    playout.resume()
    assert playout._expected_end == pytest.approx(end + 2.0, abs=0.05)
    assert silent(await gate.__anext__())
    await guard.aclose()


async def test_speech_that_began_before_a_reply_is_not_cut_mid_word():
    session, _sink, guard, loud = call()
    gate = session.input.audio
    session.user_state = "speaking"
    await session.output.audio.capture_frame(agent_frame())
    assert await gate.__anext__() is loud
    session.user_state = "listening"  # they stopped: now nobody can start
    assert silent(await gate.__anext__())
    session.user_state = "speaking"
    guard.playout._started_at -= CARRY + 0.1  # background talk that never pauses
    assert silent(await gate.__anext__())
    await guard.aclose()


async def test_the_opening_of_a_placed_call_is_not_gated():
    session, sink, guard, loud = call(skip_replies=1)
    gate = session.input.audio
    await session.output.audio.capture_frame(agent_frame())
    assert await gate.__anext__() is loud  # a menu over the opening stays heard
    sink.on_playback_finished(playback_position=1.0, interrupted=False)
    await session.output.audio.capture_frame(agent_frame())
    assert silent(await gate.__anext__())
    await guard.aclose()


async def test_the_goodbye_before_a_hang_up_stays_heard():
    session, _sink, guard, loud = call()
    gate = session.input.audio
    await session.output.audio.capture_frame(agent_frame())
    with listening_through(session):
        assert await gate.__anext__() is loud
    assert silent(await gate.__anext__())
    await guard.aclose()


async def test_echo_is_forgotten_after_enough_agent_audio_without_it():
    _session, _sink, guard, _loud = call()
    guard.playout.echo_delay = 0.8
    guard.detector.scored = QUIET_WINDOWS - 1
    await guard.check(now=1.0)
    assert guard.playout.echo_delay == 0.8
    # One window that matches at the known delay keeps it.
    guard.detector.last_match = (5.0, 0.81)
    guard.detector.scored = QUIET_WINDOWS + 5
    await guard.check(now=2.0)
    assert guard.playout.echo_delay == 0.8
    guard.detector.scored += QUIET_WINDOWS
    await guard.check(now=3.0)
    assert guard.playout.echo_delay is None
    await guard.aclose()
