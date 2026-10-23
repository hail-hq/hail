"""Bounded, in-memory recheck of an inconclusive outbound greeting."""

from __future__ import annotations

import asyncio
import logging
import time

from hailhq.voicebot.classify import classify_speech
from livekit import rtc
from livekit.agents import APIConnectOptions
from livekit.agents.llm import LLM
from livekit.agents.stt import STT
from livekit.agents.utils.participant import wait_for_track_publication
from livekit.agents.voice.amd import AMDCategory, AMDPredictionEvent

logger = logging.getLogger("hailhq.voicebot")

RECHECK_TIMEOUT_SECONDS = 5.0
MIN_SPEECH_SECONDS = 1.0
SAMPLE_RATE = 16000
# The shared prompt describes live, partial greetings and a postpone tool.
RECHECK_NOTE = (
    "\nThis transcript is the complete greeting. Postponing is not available.\n"
)


class PickupAudio:
    """Keep only the answered callee's first audio, without the input filter.

    The conversation filter can remove recorded music. This independent read
    leaves conversation input and Egress unchanged. No audio is saved to disk.
    https://docs.livekit.io/reference/python/livekit/rtc/audio_stream.html
    """

    def __init__(self, room: rtc.Room, identity: str, *, seconds: float) -> None:
        self._room = room
        self._identity = identity
        self._limit = int(seconds * SAMPLE_RATE) * 2
        self._pcm = bytearray()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._collect(), name="pickup_audio")

    async def _collect(self) -> None:
        try:
            publication = await wait_for_track_publication(
                room=self._room,
                identity=self._identity,
                kind=rtc.TrackKind.KIND_AUDIO,
                wait_for_subscription=True,
            )
            participant = self._room.remote_participants.get(self._identity)
            if participant is None or publication.track is None:
                return
            stream = rtc.AudioStream.from_track(
                track=publication.track,
                sample_rate=SAMPLE_RATE,
                num_channels=1,
                frame_size_ms=20,
                capacity=50,
            )
            try:
                async for event in stream:
                    # Ringback and early media must not become the greeting.
                    if participant.attributes.get("sip.callStatus") != "active":
                        continue
                    remaining = self._limit - len(self._pcm)
                    self._pcm.extend(event.frame.data.cast("B")[:remaining])
                    if len(self._pcm) >= self._limit:
                        break
            finally:
                await stream.aclose()
        except Exception:
            logger.warning("pickup audio unavailable for recheck", exc_info=True)

    def frame(self) -> rtc.AudioFrame | None:
        if not self._pcm:
            return None
        return rtc.AudioFrame(
            data=bytes(self._pcm),
            sample_rate=SAMPLE_RATE,
            num_channels=1,
            samples_per_channel=len(self._pcm) // 2,
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def aclose(self) -> None:
        await self.stop()
        self._pcm.clear()


def needs_recheck(result: AMDPredictionEvent) -> bool:
    """Only a timeout after real speech is worth a second transcription."""
    return (
        result.category == AMDCategory.UNCERTAIN
        and result.reason == "detection_timeout"
        and result.speech_duration >= MIN_SPEECH_SECONDS
    )


async def recheck_greeting(
    result: AMDPredictionEvent,
    audio: rtc.AudioFrame | None,
    *,
    stt: STT,
    llm: LLM,
    prompt: str,
) -> AMDPredictionEvent:
    """Recheck speech timeouts; silence and positive verdicts stay fast."""
    if (
        not needs_recheck(result)
        or audio is None
        or audio.samples_per_channel < audio.sample_rate
    ):
        return result

    started = time.monotonic()
    try:
        async with asyncio.timeout(RECHECK_TIMEOUT_SECONDS):
            event = await stt.recognize(
                audio, conn_options=APIConnectOptions(max_retry=0, timeout=3.0)
            )
            transcript = (
                event.alternatives[0].text.strip() if event.alternatives else ""
            )
            if not transcript:
                return result
            # classify_speech keeps the >=10s provider deadline Google BYO
            # needs. The outer five-second budget still bounds the recheck.
            category = await classify_speech(llm, prompt + RECHECK_NOTE, transcript)
        if category is None or category == AMDCategory.UNCERTAIN:
            return result
        return AMDPredictionEvent(
            category=category,
            reason="greeting_recheck",
            transcript=transcript,
            speech_duration=result.speech_duration,
            delay=result.delay + time.monotonic() - started,
        )
    except Exception as exc:
        # Provider error bodies can contain audio or transcripts.
        logger.warning("pickup recheck unavailable (%s)", type(exc).__name__)
    return result
