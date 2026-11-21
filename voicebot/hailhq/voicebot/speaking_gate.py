"""The agent does not listen while its own voice is on the line.

While agent audio plays, the session's input is replaced with silence, so
background talk and short words cannot cut a reply. The gate opens the moment
playout ends: the agent hears the other party while it thinks and while it is
silent.

Some lines return the agent's voice late (echo). A waveform detector compares
the raw caller track with the audio that was really played. When it confirms a
return delay, the gate also stays closed for that delay after each reply, so
the agent does not answer its own echo. Detection uses the waveform, never the
transcript: returned speech is distorted and transcribes as other words.

Audio stays in short in-memory buffers. Nothing is republished or saved.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import numpy as np
from livekit import rtc
from livekit.agents import AgentSession
from livekit.agents.utils.audio import silence_frame_like
from livekit.agents.voice import io

logger = logging.getLogger("hailhq.voicebot")
RATE = 8000
WINDOW = 4000  # 500 ms: require three distinct matching windows
MAX_DELAY = 1.6
HISTORY = 20.0
MIN_CORRELATION = 0.2
PEAK_RATIO = 1.08
LAG_TOLERANCE = 0.025
TAIL = 0.3  # margin after the return delay before listening again
CARRY = 3.0  # how long speech that began before a reply stays heard into it
QUIET_WINDOWS = 24  # compared agent windows without a match before echo is dropped

# Sessions whose input stays live through agent audio for now (a goodbye).
_listening: weakref.WeakSet = weakref.WeakSet()


@contextmanager
def listening_through(session: object) -> Iterator[None]:
    """Keep the session's input live while agent audio plays.

    For the goodbye before a hang-up: someone who talks over it is not done,
    and the hang-up check must hear them.
    """
    _listening.add(session)
    try:
        yield
    finally:
        _listening.discard(session)


def interruption_model_live(session: object) -> bool:
    """Whether LiveKit's interruption model is deciding for this session now.

    False when it was never created, was refused, or failed: the SDK then uses
    plain voice activity. Read from the SDK's own flag; unknown counts as off,
    so the gate closes.
    """
    activity = getattr(session, "_activity", None)
    return getattr(activity, "_interruption_detection_enabled", False) is True


@dataclass(frozen=True)
class EchoEvidence:
    delay_ms: float
    correlation: float
    matches: int
    detected_at: float


class EchoDetector:
    def __init__(self) -> None:
        self.inputs: deque[tuple[float, np.ndarray]] = deque()
        self.references: deque[tuple[float, np.ndarray]] = deque()
        self.matches: deque[tuple[float, float, float]] = deque(maxlen=12)
        self.evidence: EchoEvidence | None = None
        self.scored = 0  # reference windows compared with the caller audio
        self.last_match: tuple[float, float] | None = None
        self._latest = 0.0
        self.active = True
        self._partial = np.empty(0)
        self._partial_at = 0.0

    def add_input(self, samples: np.ndarray, started_at: float) -> None:
        if not self.active:
            return
        self._latest = max(self._latest, started_at + len(samples) / RATE)
        self.inputs.append((started_at, np.asarray(samples, dtype=float).copy()))
        self._prune()

    def add_played(self, samples: np.ndarray, started_at: float) -> None:
        if not self.active:
            return
        # Merge adjacent confirmed runs, but never bridge a playback pause.
        if (
            len(self._partial)
            and abs(started_at - (self._partial_at + len(self._partial) / RATE)) < 0.02
        ):
            data = np.concatenate((self._partial, np.asarray(samples, dtype=float)))
            at = self._partial_at
        else:
            data = np.asarray(samples, dtype=float)
            at = started_at
        offset = 0
        while offset + WINDOW <= len(data):
            self.references.append(
                (at + offset / RATE, data[offset : offset + WINDOW].copy())
            )
            offset += WINDOW
        self._partial = data[offset:].copy()
        self._partial_at = at + offset / RATE
        self._prune()

    def _prune(self) -> None:
        cutoff = self._latest - HISTORY
        while (
            self.inputs and self.inputs[0][0] + len(self.inputs[0][1]) / RATE < cutoff
        ):
            self.inputs.popleft()
        while self.references and self.references[0][0] < cutoff:
            self.references.popleft()
        # Reference events must not accumulate arbitrarily even without input.
        while len(self.references) > round(HISTORY * RATE / WINDOW):
            self.references.popleft()

    def clear(self) -> None:
        self.active = False
        self._partial = np.empty(0)
        self.inputs.clear()
        self.references.clear()
        self.matches.clear()

    def _input_window(self, start: float, end: float) -> np.ndarray:
        output = np.zeros(round((end - start) * RATE))
        for at, data in self.inputs:
            offset = round((at - start) * RATE)
            lo, hi = max(offset, 0), min(offset + len(data), len(output))
            if hi > lo:
                output[lo:hi] = data[lo - offset : hi - offset]
        return output

    def check(self, now: float) -> EchoEvidence | None:
        if self.evidence is not None:
            if now - self.evidence.detected_at <= 8:
                return self.evidence
            self.evidence = None
            self.matches.clear()
        processed = 0
        # A few per tick: each one costs FFTs on the event loop.
        while self.references and processed < 3:
            at, reference = self.references[0]
            if at + WINDOW / RATE + MAX_DELAY > now:
                break
            self.references.popleft()
            if at < now - 6:
                continue  # stale: costs nothing, so it does not use the budget
            processed += 1
            ref = reference - reference.mean()
            energy = float(ref @ ref)
            if energy / WINDOW < 150**2:
                continue
            # A single sustained tone can correlate with unrelated line tones.
            power = np.abs(np.fft.rfft(ref * np.hanning(WINDOW))) ** 2
            peak = int(np.argmax(power))
            if power[max(0, peak - 2) : peak + 3].sum() > 0.8 * power.sum():
                continue
            # Search all positive delays, using normalized correlation so gain
            # and polarity changes do not defeat detection.
            self.scored += 1
            heard = self._input_window(at, at + WINDOW / RATE + MAX_DELAY)
            size = 1 << (len(heard) + WINDOW - 1).bit_length()
            correlation = np.fft.irfft(
                np.fft.rfft(heard, size) * np.fft.rfft(ref[::-1], size), size
            )[WINDOW - 1 : len(heard)]
            sums = np.r_[0.0, np.cumsum(heard)]
            squares = np.r_[0.0, np.cumsum(heard * heard)]
            variance = np.maximum(
                squares[WINDOW:]
                - squares[:-WINDOW]
                - (sums[WINDOW:] - sums[:-WINDOW]) ** 2 / WINDOW,
                0,
            )
            scores = np.abs(correlation) / np.sqrt(np.maximum(variance * energy, 1))
            scores[variance / WINDOW < 150**2] = 0
            scores[: round(0.08 * RATE)] = 0
            index = int(np.argmax(scores))
            score, delay = float(scores[index]), index / RATE
            if score < MIN_CORRELATION:
                continue
            # Echo should have a distinctive delay peak. Voiced vowels can
            # correlate with unrelated speech at many pitch-period offsets.
            outside = scores.copy()
            outside[max(0, index - 160) : index + 161] = 0
            if score < PEAK_RATIO * float(outside.max()):
                continue
            self.matches.append((at, delay, score))
            self.last_match = (at, delay)
            consistent = [
                m
                for m in self.matches
                if at - m[0] <= 4 and abs(m[1] - delay) < LAG_TOLERANCE
            ]
            if len(consistent) >= 3:
                self.evidence = EchoEvidence(
                    delay * 1000, min(m[2] for m in consistent), len(consistent), now
                )
                return self.evidence
        return None


def _samples(frame: rtc.AudioFrame) -> np.ndarray:
    """Resample only the detector's copy; leave session audio untouched."""
    if frame.num_channels != 1:
        return np.empty(0)
    if frame.sample_rate == RATE:
        return np.asarray(frame.data, dtype=float)
    converter = rtc.AudioResampler(input_rate=frame.sample_rate, output_rate=RATE)
    chunks = converter.push(frame) + converter.flush()
    return (
        np.concatenate([np.asarray(f.data, dtype=float) for f in chunks])
        if chunks
        else np.empty(0)
    )


class Playout(io.AudioOutput):
    """Passes agent audio through unchanged and knows when it is on the line."""

    def __init__(self, sink: io.AudioOutput, detector: EchoDetector) -> None:
        super().__init__(
            label="SpeakingGatePlayout",
            next_in_chain=sink,
            sample_rate=sink.sample_rate,
            capabilities=io.AudioOutputCapabilities(pause=True),
        )
        self.detector = detector
        self._data = bytearray()
        self._dropped = 0  # bytes trimmed from the start of this segment
        self._rate = 24000
        # Measured return delay in seconds; None until echo is confirmed.
        self.echo_delay: float | None = None
        # False: stay open during playout unless echo is measured (an
        # interruption model then decides what stops the agent).
        self.always: Callable[[], bool] = lambda: True
        # Replies still to play before the gate applies (an outbound opening).
        self.skip_replies = 0
        self._started_at = 0.0
        self._paused_at = 0.0
        self._playing = False
        self._expected_end = 0.0
        self._finished_at = 0.0
        self.on("playback_progressed", self._played)
        self.on("playback_finished", self._finished)

    def _finished(self, _event) -> None:
        self._playing = False
        self._paused_at = 0.0
        self._expected_end = 0.0  # unplayed audio of an interrupted reply is gone
        self._finished_at = time.time()
        if self.skip_replies:
            self.skip_replies -= 1
        self.reset()

    def pause(self) -> None:
        # Paused audio is not on the line: listen, and do not let the time
        # spent paused count as playout.
        if not self._paused_at:
            self._paused_at = time.time()
        super().pause()

    def resume(self) -> None:
        if self._paused_at:
            self._expected_end += time.time() - self._paused_at
            self._paused_at = 0.0
        super().resume()

    def playing(self, now: float) -> bool:
        # Bounded by the audio handed over, so a lost finish event cannot
        # leave the agent deaf.
        return self._playing and not self._paused_at and now < self._expected_end + 1.0

    def carrying(self, now: float) -> bool:
        """Early in a reply: speech that began before it is still let through."""
        return self._playing and now < self._started_at + CARRY

    def muted(self, now: float) -> bool:
        """Whether agent audio, or its measured return, can be on the line."""
        if self.skip_replies:
            return False
        if self.playing(now):
            return self.always() or self.echo_delay is not None
        return (
            self.echo_delay is not None
            and now < self._finished_at + self.echo_delay + TAIL
        )

    def reset(self) -> None:
        self._data.clear()
        self._dropped = 0

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        try:
            self._observe(frame)
        except Exception:
            # The observer must never cost the caller the agent's audio.
            logger.exception("playout observation failed; continuing call")
        await self.next_in_chain.capture_frame(frame)

    def _observe(self, frame: rtc.AudioFrame) -> None:
        now = time.time()
        if not self._playing:
            self._started_at = now
        self._playing = True
        self._expected_end = max(self._expected_end, now) + frame.duration
        if self.detector.active and frame.num_channels == 1:
            self._rate = frame.sample_rate
            # Keep the latest 12 s: playout is often confirmed once, at the end
            # of a long reply, and only recent audio can still be matched.
            self._data.extend(frame.data.cast("B"))
            extra = len(self._data) - self._rate * 12 * 2
            if extra > 0:
                extra += extra % 2
                del self._data[:extra]
                self._dropped += extra

    def _played(self, event) -> None:
        try:
            first = round(event.offset * self._rate) * 2 - self._dropped
            start = max(0, first)
            end = min(len(self._data), first + round(event.duration * self._rate) * 2)
            if end > start:
                frame = rtc.AudioFrame(
                    bytes(self._data[start:end]), self._rate, 1, (end - start) // 2
                )
                # Audio trimmed from the start played before this stretch.
                skipped = (start - first) / 2 / self._rate
                self.detector.add_played(_samples(frame), event.started_at + skipped)
        except Exception:
            logger.exception("echo reference observation failed; continuing call")

    def flush(self) -> None:
        super().flush()
        self.next_in_chain.flush()

    def clear_buffer(self) -> None:
        # The sink reports the played portion before completing interruption.
        self.next_in_chain.clear_buffer()


class SpeakingGate(io.AudioInput):
    """Silence the agent's input while its own voice can be on the line."""

    def __init__(
        self,
        source: io.AudioInput,
        playout: Playout,
        enabled: Callable[[], bool],
        user_speaking: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__(label="SpeakingGate", source=source)
        self._playout = playout
        self._enabled = enabled
        self._user_speaking = user_speaking

    async def __anext__(self) -> rtc.AudioFrame:
        frame = await self.source.__anext__()
        try:
            now = time.time()
            muted = (
                self._enabled()
                and self._playout.muted(now)
                # Someone already talking when the reply starts is not cut
                # mid-word: the agent yields to them, as it always did. While
                # the gate is closed nobody can start, so this only carries
                # speech that began before it.
                and not (self._playout.carrying(now) and self._user_speaking())
            )
        except Exception:
            # The gate must never cost the call its input.
            logger.exception("speaking gate check failed; listening")
            muted = False
        if not muted:
            return frame
        # Silence, not a gap: VAD and STT streams stay continuous.
        return silence_frame_like(frame)


class SpeakingGuard:
    """Wires the gate and the echo detector into one call's session."""

    def __init__(
        self,
        session: AgentSession,
        *,
        enabled: Callable[[], bool],
        skip_replies: int = 0,
        on_echo: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self.session = session
        self.skip_replies = skip_replies  # replies played before the gate applies
        self._echo_scored = 0
        self._seen_match: tuple[float, float] | None = None
        self.enabled = enabled  # False keeps the input untouched (phone menus)
        self.on_echo = on_echo  # called once, when echo is first confirmed
        self.detector = EchoDetector()
        self._reported = False
        self._closed = False
        self._task: asyncio.Task | None = None
        self._raw_task: asyncio.Task | None = None
        self.playout: Playout | None = None

        @session.on("close")
        def closed(_):
            self._closed = True
            if self._task:
                self._task.cancel()
            if self._raw_task:
                self._raw_task.cancel()
            self._clear()

    def attach(self) -> None:
        sink = getattr(getattr(self.session, "output", None), "audio", None)
        source = getattr(getattr(self.session, "input", None), "audio", None)
        if sink is None or source is None:
            return
        self.playout = Playout(sink, self.detector)
        session = self.session
        # With LiveKit's interruption model deciding, the gate closes only on
        # calls with measured echo. Once the model is refused or fails, the
        # gate takes over.
        self.playout.always = lambda: not interruption_model_live(session)
        self.playout.skip_replies = self.skip_replies
        self.session.output.audio = self.playout
        self.session.input.audio = SpeakingGate(
            source,
            self.playout,
            lambda: self.enabled() and session not in _listening,
            lambda: getattr(session, "user_state", None) == "speaking",
        )
        self._raw_task = asyncio.create_task(self._observe_raw(), name="echo_raw_input")
        self._task = asyncio.create_task(self._run(), name="echo_detector")

    async def _observe_raw(self) -> None:
        # A second local reader of the subscribed caller track, without voice
        # isolation. It adds no room connection, publication, or recording.
        try:
            while not self._closed and self.detector.active:
                participant = self.session.room_io.linked_participant
                if participant is None:
                    await asyncio.sleep(0.1)
                    continue
                stream = rtc.AudioStream.from_participant(
                    participant=participant,
                    track_source=rtc.TrackSource.SOURCE_MICROPHONE,
                    sample_rate=RATE,
                    num_channels=1,
                    frame_size_ms=50,
                    capacity=20,
                    noise_cancellation=None,
                )
                next_at = float("-inf")
                try:
                    async for event in stream:
                        if self._closed or not self.detector.active:
                            return
                        if self.session.room_io.linked_participant is not participant:
                            break
                        if not self.session.input.audio_enabled:
                            continue
                        frame = event.frame
                        # Count samples from an anchor: arrival times bunch up
                        # when the event loop stalls. Re-anchor after a real gap.
                        arrived = time.time() - frame.duration
                        if abs(arrived - next_at) > 0.2:
                            next_at = arrived
                        self.detector.add_input(
                            np.asarray(frame.data, dtype=float), next_at
                        )
                        next_at += frame.duration
                finally:
                    await stream.aclose()
                await asyncio.sleep(0.1)
        except Exception:
            logger.exception("raw caller audio observation failed; continuing call")

    async def check(self, now: float | None = None) -> None:
        if self._closed or self.playout is None:
            return
        evidence = self.detector.check(time.time() if now is None else now)
        if evidence is None:
            self._drop_stale_echo()
            return
        self.playout.echo_delay = evidence.delay_ms / 1000
        self._echo_scored = self.detector.scored
        if self._reported:
            return
        self._reported = True
        if self.on_echo:
            await self.on_echo(
                {
                    "delay_ms": round(evidence.delay_ms),
                    "correlation": round(evidence.correlation, 3),
                    "matched_windows": evidence.matches,
                }
            )

    def _drop_stale_echo(self) -> None:
        """Forget the echo once enough agent audio came back without it."""
        delay = self.playout.echo_delay if self.playout else None
        if delay is None:
            return
        # Short replies never give three matches in a row. One window that
        # matches at the known delay still shows the echo is there.
        match = self.detector.last_match
        if (
            match is not None
            and match != self._seen_match
            and abs(match[1] - delay) < 2 * LAG_TOLERANCE
        ):
            self._seen_match = match
            self._echo_scored = self.detector.scored
        if self.detector.scored - self._echo_scored >= QUIET_WINDOWS:
            # The caller left speakerphone, or the one confirmation was wrong.
            self.playout.echo_delay = None

    async def _run(self) -> None:
        while not self._closed:
            await asyncio.sleep(0.5)
            try:
                await self.check()
            except Exception:
                logger.exception("echo check failed; continuing call")

    def _clear(self) -> None:
        self.detector.clear()
        if self.playout:
            self.playout.reset()

    async def aclose(self) -> None:
        self._closed = True
        tasks = [task for task in (self._task, self._raw_task) if task is not None]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._clear()
