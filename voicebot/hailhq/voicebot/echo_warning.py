"""Bounded waveform echo detection using confirmed playout, never transcript guesses.

Audio stays in short in-memory buffers. Nothing is muted, republished, or saved.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np
from livekit import rtc
from livekit.agents import AgentSession
from livekit.agents.voice import io

logger = logging.getLogger("hailhq.voicebot")
RATE = 8000
WINDOW = 4000  # 500 ms: require three distinct matching windows
MAX_DELAY = 1.6
HISTORY = 20.0
MIN_CORRELATION = 0.2
PEAK_RATIO = 1.08
LAG_TOLERANCE = 0.025


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
        while self.references and processed < 8:
            at, reference = self.references[0]
            if at + WINDOW / RATE + MAX_DELAY > now:
                break
            self.references.popleft()
            processed += 1
            if at < now - 6:
                continue
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


class EchoOutput(io.AudioOutput):
    def __init__(self, sink: io.AudioOutput, detector: EchoDetector) -> None:
        super().__init__(
            label="EchoReference",
            next_in_chain=sink,
            sample_rate=sink.sample_rate,
            capabilities=io.AudioOutputCapabilities(pause=True),
        )
        self.detector = detector
        self._data = bytearray()
        self._rate = 24000
        self.on("playback_progressed", self._played)
        self.on("playback_finished", lambda _: self._data.clear())

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        if self.detector.active and frame.num_channels == 1:
            self._rate = frame.sample_rate
            remaining = max(0, self._rate * 12 * 2 - len(self._data))
            self._data.extend(frame.data.cast("B")[:remaining])
        await self.next_in_chain.capture_frame(frame)

    def _played(self, event) -> None:
        try:
            start = round(event.offset * self._rate) * 2
            end = min(len(self._data), start + round(event.duration * self._rate) * 2)
            if end > start:
                frame = rtc.AudioFrame(
                    bytes(self._data[start:end]), self._rate, 1, (end - start) // 2
                )
                self.detector.add_played(_samples(frame), event.started_at)
        except Exception:
            logger.exception("echo reference observation failed; continuing call")

    def flush(self) -> None:
        super().flush()
        self.next_in_chain.flush()

    def clear_buffer(self) -> None:
        # The sink reports the played portion before completing interruption.
        self.next_in_chain.clear_buffer()


class EchoWarning:
    def __init__(
        self,
        session: AgentSession,
        *,
        can_warn: Callable[[], bool],
        on_warning: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self.session = session
        self.can_warn = can_warn
        self.on_warning = on_warning
        self.detector = EchoDetector()
        self._warned = False
        self._closed = False
        self._task: asyncio.Task | None = None
        self._output: EchoOutput | None = None
        self._raw_task: asyncio.Task | None = None

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
        if sink is None:
            return
        self._output = EchoOutput(sink, self.detector)
        self.session.output.audio = self._output
        self._raw_task = asyncio.create_task(self._observe_raw(), name="echo_raw_input")
        self._task = asyncio.create_task(self._run(), name="echo_warning")

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
                try:
                    async for event in stream:
                        if self._closed or not self.detector.active:
                            return
                        if self.session.room_io.linked_participant is not participant:
                            break
                        if not self.session.input.audio_enabled:
                            continue
                        frame = event.frame
                        self.detector.add_input(
                            np.asarray(frame.data, dtype=float),
                            time.time() - frame.duration,
                        )
                finally:
                    await stream.aclose()
                await asyncio.sleep(0.1)
        except Exception:
            logger.exception("raw echo observation failed; continuing call")

    async def check(self, now: float | None = None) -> None:
        if self._closed or self._warned:
            return
        evidence = self.detector.check(time.time() if now is None else now)
        if (
            evidence is None
            or not self.can_warn()
            or self.session.agent_state in ("speaking", "thinking")
        ):
            return
        self.session.generate_reply(
            instructions=(
                "In the conversation's language, say only this short sentence: "
                "I'm hearing an echo—it's disrupting the call. "
                "Do not add advice, questions, explanations, or laughter. "
                "Do not claim the problem is fixed."
            ),
            tool_choice="none",
            allow_interruptions=False,
        )
        self._warned = True
        if self._raw_task:
            self._raw_task.cancel()
        self._clear()
        if self.on_warning:
            await self.on_warning(
                {
                    "delay_ms": round(evidence.delay_ms),
                    "correlation": round(evidence.correlation, 3),
                    "matched_windows": evidence.matches,
                }
            )

    async def _run(self) -> None:
        while not self._closed and not self._warned:
            await asyncio.sleep(0.5)
            try:
                await self.check()
            except Exception:
                logger.exception("echo warning failed; continuing call")

    def _clear(self) -> None:
        self.detector.clear()
        if self._output:
            self._output._data.clear()

    async def aclose(self) -> None:
        self._closed = True
        tasks = [task for task in (self._task, self._raw_task) if task is not None]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._clear()
