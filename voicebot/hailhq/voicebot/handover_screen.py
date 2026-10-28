"""Check who picked up the handover contact's phone.

A mobile that rings out goes to voicemail, and SIP reports that as an
answer. Without this check the agent introduced the caller to the mailbox
and left the caller talking to it.

LiveKit's ``AMD`` cannot run here: it reads the session's audio input,
which is linked to the caller, not to the contact's second SIP leg
(livekit-agents 1.8.8 ``voice/audio_recognition.py`` feeds
``session.amd.push_audio``). So this reads the contact's track directly,
waits for the greeting to end (VAD), then transcribes it once and
classifies it with the outbound AMD prompt.

One audio stream covers the whole check (``ContactListener``): first the
pickup, then, when the pickup was silent, the intro and the reply after it.
No speech falls between the two.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

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

# The contact said nothing at pickup, nor during or after the intro.
NO_REPLY = "no_reply"
# Verdicts that mean no person took the call: voicemail, a full or missing
# mailbox, a carrier announcement, call screening, or silence through the
# intro. A silent pickup ("silent") is not one: some phones send no sound
# for several seconds after answering (a French mobile spoke 7 s in), so it
# hears the intro and the reply decides.
NOT_A_PERSON: frozenset[str] = frozenset(
    {"machine-vm", "machine-unavailable", "machine-ivr", NO_REPLY}
)

# A person usually says "Hello?" within a second or two. No speech by then:
# the agent says the intro.
NO_SPEECH_SECONDS = 5.0
# Silence after speech that ends the greeting. Longer than a pause inside a
# recorded greeting ("Bonjour, ... vous êtes sur la messagerie de").
END_SILENCE_SECONDS = 0.8
# Cap on listening once speech starts. A voicemail greeting names itself
# early; a person does not talk this long before hearing anyone.
MAX_LISTEN_SECONDS = 8.0
TRACK_WAIT_SECONDS = 5.0
# Audio kept from before the VAD reports speech, so the first word is whole.
SPEECH_PREFIX_SECONDS = 0.5
# Stops a listen whose audio stalls (the VAD gets no frames to report on).
LISTEN_CAP_SECONDS = 60.0


async def _contact_audio(room: rtc.Room, identity: str) -> rtc.AudioStream | None:
    """The contact's audio as 16 kHz mono, or None when they publish none."""
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
        return None
    return rtc.AudioStream.from_track(
        track=publication.track,
        sample_rate=SAMPLE_RATE,
        num_channels=1,
        frame_size_ms=20,
        capacity=50,
    )


class ContactListener:
    """The contact's leg from pickup to the end of the check.

    ``pickup()`` returns an AMD category, ``"silent"`` when nobody spoke
    within ``NO_SPEECH_SECONDS``, or None when the check could not run.
    After a silent pickup the bridge says the intro and calls ``reply()``,
    which listens on the same stream and returns an AMD category,
    ``NO_REPLY``, or None. ``close()`` releases the streams.
    """

    def __init__(
        self,
        room: rtc.Room,
        identity: str,
        session: AgentSession,
        *,
        org_cfgs: dict[str, ResolvedLayer] | None = None,
        llm_override: bool = False,
    ) -> None:
        self._room = room
        self._identity = identity
        self._session = session
        self._org_cfgs = org_cfgs or {}
        self._llm_override = llm_override
        self._stream: rtc.AudioStream | None = None
        self._vad_stream = None
        self._feeder: asyncio.Task | None = None
        self._pcm = bytearray()
        self._capturing = False

    async def _open(self, vad: VAD) -> bool:
        self._stream = await _contact_audio(self._room, self._identity)
        if self._stream is None:
            return False
        self._vad_stream = vad.stream()
        self._feeder = asyncio.create_task(self._feed(), name="handover_screen_feed")
        return True

    async def _feed(self) -> None:
        prefix = int(SPEECH_PREFIX_SECONDS * SAMPLE_RATE) * 2
        try:
            async for event in self._stream:
                self._pcm.extend(event.frame.data.cast("B"))
                if not self._capturing and len(self._pcm) > 2 * prefix:
                    del self._pcm[:-prefix]
                self._vad_stream.push_frame(event.frame)
        finally:
            self._vad_stream.end_input()

    async def _listen(self, give_up: Callable[[float], bool]) -> bytes:
        """The contact's next speech as 16 kHz mono PCM, or b"" when
        ``give_up(now)`` turns true first or their audio ends."""
        if self._vad_stream is None:
            return b""
        loop = asyncio.get_running_loop()
        prefix = int(SPEECH_PREFIX_SECONDS * SAMPLE_RATE) * 2
        limit = int(MAX_LISTEN_SECONDS * SAMPLE_RATE) * 2
        speech_at: float | None = None
        silent_since: float | None = None
        start = 0
        try:
            async with asyncio.timeout(LISTEN_CAP_SECONDS):
                async for event in self._vad_stream:
                    now = loop.time()
                    if event.type == VADEventType.START_OF_SPEECH:
                        if speech_at is None:
                            speech_at, self._capturing = now, True
                            start = max(0, len(self._pcm) - prefix)
                        silent_since = None
                    elif event.type == VADEventType.END_OF_SPEECH:
                        silent_since = now
                    elif speech_at is None:
                        if give_up(now):
                            break
                    elif now - speech_at >= MAX_LISTEN_SECONDS or (
                        silent_since is not None
                        and now - silent_since >= END_SILENCE_SECONDS
                    ):
                        # The greeting ended, or it ran to the cap.
                        break
        except TimeoutError:
            pass
        if speech_at is None:
            return b""
        return bytes(self._pcm[start : start + limit])

    async def pickup(self) -> str | None:
        vad = self._session.vad
        if vad is None:
            logger.warning("handover pickup check unavailable (no VAD)")
            return None
        try:
            if not await self._open(vad):
                return "silent"
            started = asyncio.get_running_loop().time()
            pcm = await self._listen(lambda now: now - started >= NO_SPEECH_SECONDS)
            if not pcm:
                return "silent"
            return await self._classify(pcm)
        except Exception as exc:
            # Provider error bodies can contain audio or transcripts.
            logger.warning("handover pickup check failed (%s)", type(exc).__name__)
            return None

    async def reply(self, intro_done: asyncio.Event, seconds: float) -> str | None:
        """What the contact said during the intro or within ``seconds`` after
        it (``intro_done`` is set then), classified."""
        done_at: float | None = None

        def give_up(now: float) -> bool:
            nonlocal done_at
            if done_at is None and intro_done.is_set():
                done_at = now
            return done_at is not None and now - done_at >= seconds

        try:
            pcm = await self._listen(give_up)
            if not pcm:
                return NO_REPLY
            return await self._classify(pcm)
        except Exception as exc:
            logger.warning("handover reply check failed (%s)", type(exc).__name__)
            return None

    async def _classify(self, pcm: bytes) -> str | None:
        owned: list = []
        try:
            stt = _build_stt(self._org_cfgs)
            if stt is None:
                logger.warning("handover pickup check has no transcriber; skipping")
                return None
            owned.append(stt)
            llm, classifiers = build_classifier(
                self._session,
                org_cfgs=self._org_cfgs,
                llm_override=self._llm_override,
            )
            owned.extend(classifiers)
            if not isinstance(llm, LLM):
                logger.warning("handover pickup check has no classifier; skipping")
                return None
            return await classify_greeting(pcm, stt=stt, llm=llm)
        finally:
            await _close_all(owned)

    async def close(self) -> None:
        if self._feeder is not None:
            self._feeder.cancel()
            await asyncio.gather(self._feeder, return_exceptions=True)
        if self._stream is not None:
            await self._stream.aclose()
        if self._vad_stream is not None:
            await self._vad_stream.aclose()


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


__all__ = [
    "NOT_A_PERSON",
    "NO_REPLY",
    "ContactListener",
    "classify_greeting",
]
