"""Bounded, in-memory recheck of an inconclusive outbound greeting."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from livekit import rtc
from livekit.agents import APIConnectOptions
from livekit.agents.llm import LLM, ChatContext, function_tool
from livekit.agents.stt import STT
from livekit.agents.utils.participant import wait_for_track_publication
from livekit.agents.voice.amd import AMDCategory, AMDPredictionEvent

logger = logging.getLogger("hailhq.voicebot")

RECHECK_TIMEOUT_SECONDS = 5.0
SAMPLE_RATE = 16000


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
        result.category != AMDCategory.UNCERTAIN
        or result.reason != "detection_timeout"
        or result.speech_duration < 1.0
        or audio is None
        or audio.samples_per_channel < audio.sample_rate
    ):
        return result

    @function_tool
    async def save_prediction(label: AMDCategory) -> None:
        """Save the greeting category."""

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
            ctx = ChatContext()
            ctx.add_message(role="system", content=prompt)
            ctx.add_message(role="user", content=transcript)
            async with llm.chat(
                chat_ctx=ctx,
                tools=[save_prediction],
                tool_choice="required",
                # Google BYO also requires a >=10s provider deadline. The
                # outer five-second local budget still bounds the recheck.
                conn_options=APIConnectOptions(max_retry=0, timeout=10.0),
            ) as stream:
                response = await stream.collect()
            for call in response.tool_calls:
                if call.name != "save_prediction":
                    continue
                category = AMDCategory(json.loads(call.arguments)["label"])
                if category == AMDCategory.UNCERTAIN:
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
