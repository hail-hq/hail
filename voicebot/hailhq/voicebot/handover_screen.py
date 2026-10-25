"""Check who picked up the handover contact's phone before the intro.

A mobile that rings out goes to voicemail, and SIP reports that as an
answer. Without this check the agent introduced the caller to the mailbox
and left the caller talking to it.

LiveKit's ``AMD`` cannot run here: it reads the session's audio input,
which is linked to the caller, not to the contact's second SIP leg
(livekit-agents 1.8.8 ``voice/audio_recognition.py`` feeds
``session.amd.push_audio``). So this reads the contact's track directly,
waits for the greeting to end (VAD), then transcribes it once and
classifies it with the outbound AMD prompt.
"""

from __future__ import annotations

import asyncio
import logging

from hailhq.core.config import settings
from hailhq.voicebot.amd import (
    GREETING_CLASSIFICATION_PROMPT,
    _close_all,
    build_classifier,
    recheck_stt,
)
from hailhq.voicebot.pickup import SAMPLE_RATE, transcribe_and_classify
from hailhq.voicebot.pipeline import ResolvedLayer
from livekit import rtc
from livekit.agents.llm import LLM
from livekit.agents.stt import STT
from livekit.agents.utils.participant import wait_for_track_publication
from livekit.agents.vad import VAD, VADEventType
from livekit.agents.voice import AgentSession

logger = logging.getLogger("hailhq.voicebot")

# Verdicts that mean no person took the call: voicemail, a full or missing
# mailbox, a carrier announcement, or call screening.
NOT_A_PERSON: frozenset[str] = frozenset(
    {"machine-vm", "machine-unavailable", "machine-ivr"}
)

# A person who picks up and waits in silence hears the intro after this.
NO_SPEECH_SECONDS = 3.0
# Silence after speech that ends the greeting. Longer than a pause inside a
# recorded greeting ("Bonjour, ... vous êtes sur la messagerie de").
END_SILENCE_SECONDS = 0.8
# Cap on listening. A voicemail greeting names itself early; a person does
# not talk this long before hearing anyone.
MAX_LISTEN_SECONDS = 8.0
TRACK_WAIT_SECONDS = 5.0


async def listen_for_greeting(room: rtc.Room, identity: str, vad: VAD) -> bytes:
    """The contact's first speech as 16 kHz mono PCM, or b"" when they
    stayed silent for ``NO_SPEECH_SECONDS``."""
    publication = await asyncio.wait_for(
        wait_for_track_publication(
            room=room,
            identity=identity,
            kind=rtc.TrackKind.KIND_AUDIO,
            wait_for_subscription=True,
        ),
        timeout=TRACK_WAIT_SECONDS,
    )
    if publication.track is None:
        return b""
    stream = rtc.AudioStream.from_track(
        track=publication.track,
        sample_rate=SAMPLE_RATE,
        num_channels=1,
        frame_size_ms=20,
        capacity=50,
    )
    vad_stream = vad.stream()
    limit = int(MAX_LISTEN_SECONDS * SAMPLE_RATE) * 2
    pcm = bytearray()

    async def _feed() -> None:
        try:
            async for event in stream:
                pcm.extend(event.frame.data.cast("B")[: limit - len(pcm)])
                vad_stream.push_frame(event.frame)
        finally:
            vad_stream.end_input()

    feeder = asyncio.create_task(_feed(), name="handover_screen_feed")
    loop = asyncio.get_running_loop()
    started = loop.time()
    heard = False
    silent_since: float | None = None
    try:
        async with asyncio.timeout(MAX_LISTEN_SECONDS):
            async for event in vad_stream:
                now = loop.time()
                if event.type == VADEventType.START_OF_SPEECH:
                    heard, silent_since = True, None
                elif event.type == VADEventType.END_OF_SPEECH:
                    silent_since = now
                elif (not heard and now - started >= NO_SPEECH_SECONDS) or (
                    silent_since is not None
                    and now - silent_since >= END_SILENCE_SECONDS
                ):
                    # Silent pickup, or the greeting ended.
                    break
    except TimeoutError:
        pass
    finally:
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
        await stream.aclose()
        await vad_stream.aclose()
    return bytes(pcm) if heard else b""


async def classify_greeting(pcm: bytes, *, stt: STT, llm: LLM) -> str | None:
    """The AMD category of a finished greeting, or None when unknown."""
    if len(pcm) < SAMPLE_RATE // 2:  # under a quarter second of audio
        return None
    audio = rtc.AudioFrame(
        data=pcm,
        sample_rate=SAMPLE_RATE,
        num_channels=1,
        samples_per_channel=len(pcm) // 2,
    )
    _, category = await transcribe_and_classify(
        audio, stt=stt, llm=llm, prompt=GREETING_CLASSIFICATION_PROMPT
    )
    return category.value if category is not None else None


def _build_stt(org_cfgs: dict[str, ResolvedLayer]) -> STT | None:
    """A one-shot transcriber that follows the org's STT choice.

    Hail's Deepgram only when the org has no STT of its own, as in the
    outbound recheck (``amd.build_amd_models``). An org on Deepgram uses its
    own key and model. An org on another provider skips the check: its audio
    never goes to a provider it did not pick. Language detection is on in
    both cases: the contact's greeting can be in any language, and
    Deepgram's default is English.
    """
    org_stt = org_cfgs.get("stt")
    if org_stt is None:
        model, api_key = settings.deepgram_model, settings.deepgram_api_key
    elif org_stt.provider == "deepgram":
        model = org_stt.params.get("model") or settings.deepgram_model
        # Hail's key only when the org allows fallback to Hail.
        api_key = org_stt.api_key or (
            settings.deepgram_api_key if org_stt.fallback_enabled else ""
        )
    else:
        return None
    if not (model and api_key):
        return None
    return recheck_stt(model, api_key)


async def screen_handover_pickup(
    room: rtc.Room,
    identity: str,
    session: AgentSession,
    *,
    org_cfgs: dict[str, ResolvedLayer] | None = None,
    llm_override: bool = False,
) -> str | None:
    """Who answered the contact's leg: an AMD category, or None when the
    check could not run. A None or ``human``/``uncertain`` verdict connects
    the call, as before this check existed."""
    if session.vad is None:
        logger.warning("handover pickup check unavailable (no VAD)")
        return None
    owned: list = []
    try:
        stt = _build_stt(org_cfgs or {})
        if stt is None:
            logger.warning("handover pickup check has no transcriber; skipping")
            return None
        owned.append(stt)
        llm, classifiers = build_classifier(
            session, org_cfgs=org_cfgs, llm_override=llm_override
        )
        owned.extend(classifiers)
        if not isinstance(llm, LLM):
            logger.warning("handover pickup check has no classifier; skipping")
            return None
        pcm = await listen_for_greeting(room, identity, session.vad)
        return await classify_greeting(pcm, stt=stt, llm=llm)
    except Exception as exc:
        # Provider error bodies can contain audio or transcripts.
        logger.warning("handover pickup check failed (%s)", type(exc).__name__)
        return None
    finally:
        await _close_all(owned)


__all__ = [
    "NOT_A_PERSON",
    "classify_greeting",
    "listen_for_greeting",
    "screen_handover_pickup",
]
