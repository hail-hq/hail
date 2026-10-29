"""Listen to the handover contact's leg after pickup: does anyone speak?

The handover is a plain forward: whoever picks up is connected. Some
mailboxes answer and stay silent, which would leave the caller on a dead
line. The bridge listens (VAD only, nothing is said or transcribed) and
takes the caller back when nobody on the contact's side speaks in time.

LiveKit's session VAD reads the caller's audio, so this reads the
contact's track directly.
"""

from __future__ import annotations

import asyncio

from livekit import rtc
from livekit.agents.utils.participant import wait_for_track_publication
from livekit.agents.vad import VAD, VADEventType

SAMPLE_RATE = 16000


async def _contact_audio(room: rtc.Room, identity: str) -> rtc.AudioStream | None:
    """The contact's audio as 16 kHz mono, or None when they publish none."""
    publication = await wait_for_track_publication(
        room=room,
        identity=identity,
        kind=rtc.TrackKind.KIND_AUDIO,
        wait_for_subscription=True,
    )
    if publication.track is None:
        return None
    return rtc.AudioStream.from_track(
        track=publication.track,
        sample_rate=SAMPLE_RATE,
        num_channels=1,
        frame_size_ms=20,
        capacity=50,
    )


async def contact_speaks(
    room: rtc.Room, identity: str, vad: VAD, seconds: float
) -> bool:
    """True when the contact starts speaking within ``seconds`` from now.
    False when they stay silent, publish no audio, or leave."""
    stream: rtc.AudioStream | None = None
    vad_stream = None
    feeder: asyncio.Task | None = None

    async def _listen() -> bool:
        nonlocal stream, vad_stream, feeder
        stream = await _contact_audio(room, identity)
        if stream is None:
            return False
        vad_stream = vad.stream()

        async def _feed() -> None:
            try:
                async for event in stream:
                    vad_stream.push_frame(event.frame)
            finally:
                vad_stream.end_input()

        feeder = asyncio.create_task(_feed(), name="handover_watch_feed")
        async for event in vad_stream:
            if event.type == VADEventType.START_OF_SPEECH:
                return True
        return False  # the contact's audio ended: they left

    try:
        return await asyncio.wait_for(_listen(), timeout=seconds)
    except TimeoutError:
        return False
    finally:
        if feeder is not None:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        if stream is not None:
            await stream.aclose()
        if vad_stream is not None:
            await vad_stream.aclose()


__all__ = ["contact_speaks"]
