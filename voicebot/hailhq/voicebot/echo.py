"""Duplex WebRTC echo cancellation before VAD and STT.

The reverse stream must follow playout, not the speed of TTS generation.
Only the agent's input is cleaned; published audio and recordings stay original.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from hailhq.core.config import settings
from livekit import rtc
from livekit.agents import AgentSession
from livekit.agents.voice import io

logger = logging.getLogger("hailhq.voicebot")
RATE = 24000
SAMPLES = RATE // 100


def _copy(frame: rtc.AudioFrame) -> rtc.AudioFrame:
    return rtc.AudioFrame(
        bytes(frame.data),
        frame.sample_rate,
        frame.num_channels,
        frame.samples_per_channel,
    )


class EchoFilter:
    """One adaptive filter per call; a native failure switches to passthrough."""

    def __init__(self) -> None:
        self.disabled = False
        self._apm = rtc.AudioProcessingModule(echo_cancellation=True)
        # Render is paced here. AEC3 estimates the remaining network/acoustic delay.
        self._apm.set_stream_delay_ms(0)
        self._last_render = 0.0

    def _failed(self) -> None:
        self.disabled = True
        logger.exception("echo cancellation failed; continuing with original audio")

    def render(self, frame: rtc.AudioFrame) -> None:
        if self.disabled:
            return
        try:
            # Native processing is in-place: never mutate the published frame.
            reference = _copy(frame)
            if reference.samples_per_channel < SAMPLES:
                reference = rtc.AudioFrame(
                    bytes(reference.data)
                    + bytes((SAMPLES - reference.samples_per_channel) * 2),
                    RATE,
                    1,
                    SAMPLES,
                )
            self._apm.process_reverse_stream(reference)
            self._last_render = time.monotonic()
        except Exception:
            self._failed()

    def capture(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        if self.disabled:
            return frame
        try:
            cleaned = _copy(frame)
            if time.monotonic() - self._last_render > 0.03:
                # Keep the reverse stream advancing through pauses between replies.
                self._apm.process_reverse_stream(
                    rtc.AudioFrame(bytes(SAMPLES * 2), RATE, 1, SAMPLES)
                )
            self._apm.process_stream(cleaned)
            return cleaned
        except Exception:
            self._failed()
            return frame


class EchoInput(io.AudioInput):
    def __init__(self, source: io.AudioInput, processor: EchoFilter) -> None:
        super().__init__(label="EchoCancellation", source=source)
        self._processor = processor
        self._pending: deque[rtc.AudioFrame] = deque()

    async def __anext__(self) -> rtc.AudioFrame:
        while not self._pending:
            frame = await self.source.__anext__()
            if frame.sample_rate != RATE or frame.num_channels != 1:
                # Unexpected transport format: keep the call audible.
                self._processor.disabled = True
                return frame
            data = bytes(frame.data)
            for offset in range(0, frame.samples_per_channel, SAMPLES):
                count = min(SAMPLES, frame.samples_per_channel - offset)
                part = rtc.AudioFrame(
                    data[offset * 2 : (offset + count) * 2], RATE, 1, count
                )
                # APM requires exactly 10 ms. Preserve a short tail unprocessed.
                self._pending.append(
                    self._processor.capture(part) if count == SAMPLES else part
                )
        return self._pending.popleft()


class EchoOutput(io.AudioOutput):
    """Pace TTS at 10 ms so the reference follows actual outgoing speech.

    This also bounds RoomIO's normally unbounded generated-audio queue. Pause,
    interruption, flush, and playback events keep their normal SDK semantics.
    """

    def __init__(self, sink: io.AudioOutput, processor: EchoFilter) -> None:
        super().__init__(
            label="EchoReference",
            next_in_chain=sink,
            sample_rate=RATE,
            capabilities=io.AudioOutputCapabilities(pause=True),
        )
        self._processor = processor
        self._ready = asyncio.Event()
        self._ready.set()
        self._generation = 0
        self._next_at = 0.0

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        generation = self._generation
        data = bytes(frame.data)
        for offset in range(0, frame.samples_per_channel, SAMPLES):
            await self._ready.wait()
            if generation != self._generation:
                return
            now = time.monotonic()
            if now - self._next_at > 0.03:
                self._next_at = now
            await asyncio.sleep(max(0, self._next_at - now))
            await self._ready.wait()
            if generation != self._generation:
                return
            count = min(SAMPLES, frame.samples_per_channel - offset)
            part = rtc.AudioFrame(
                data[offset * 2 : (offset + count) * 2], RATE, 1, count
            )
            self._processor.render(part)
            await self.next_in_chain.capture_frame(part)
            self._next_at += count / RATE

    def flush(self) -> None:
        super().flush()
        self.next_in_chain.flush()

    def clear_buffer(self) -> None:
        self._generation += 1
        self._next_at = 0
        self._ready.set()
        self.next_in_chain.clear_buffer()

    def pause(self) -> None:
        self._ready.clear()
        super().pause()

    def resume(self) -> None:
        self._next_at = 0
        super().resume()
        self._ready.set()


def attach_echo_filter(session: AgentSession) -> None:
    """Install after RoomIO starts, before the first greeting can be generated."""
    if not settings.hail_echo_cancellation_enabled:
        return
    source, sink = session.input.audio, session.output.audio
    if source is None or sink is None:
        return
    try:
        processor = EchoFilter()
    except Exception:
        logger.exception("echo cancellation unavailable; continuing call")
        return
    session.output.audio = EchoOutput(sink, processor)
    session.input.audio = EchoInput(source, processor)
