"""Listen to the handover contact's leg after pickup: who answered?

The handover is a plain forward: when the phone picks up, the two people
hear each other at once. The bridge listens to the contact's leg (nothing
is said) and answers two questions:

* Does anyone speak? Some mailboxes answer and stay silent, which would
  leave the caller on a dead line. No speech in time: the caller is taken
  back.
* Is it a mailbox? From the first speech, the line's audio so far is
  transcribed once and classified with the outbound AMD prompt, up to
  ``MAX_CHECKS`` times, a few seconds apart. Voicemail or a full or unset
  mailbox (outbound AMD's hang-up verdicts) takes the caller back.
  Anything else (a person, a phone menu, unsure, an error, no transcriber)
  keeps the two people connected, as before this check: a switchboard
  menu stays connected so the caller can press keys.

LiveKit's ``AMD`` cannot run here: it reads the session's audio input,
which is linked to the caller, not to the contact's second SIP leg
(livekit-agents 1.8.8 ``voice/audio_recognition.py`` feeds
``session.amd.push_audio``). So this reads the contact's track directly
and reuses AMD's classifier, prompt and one-shot transcriber
(:mod:`hailhq.voicebot.amd`, :mod:`hailhq.voicebot.pickup`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from hailhq.core.config import settings
from hailhq.voicebot.amd import (
    GREETING_CLASSIFICATION_PROMPT,
    MACHINE_HANGUP_CATEGORIES,
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
from livekit.agents.voice.amd import AMDCategory

logger = logging.getLogger("hailhq.voicebot")

# Each check sends all the audio so far, so their number is capped. Three
# checks about 2 to 3 seconds apart cover the first 7 to 9 seconds of a
# greeting, inside the bridge's 10-second settle window. A recorded
# greeting gives its cues ("you've reached", "leave a message") early.
MAX_CHECKS = 3
# Least time from the first speech, or from the previous check's start, to
# the next check. A person's verdict never ends the checks (a greeting can
# open like a person, "Hi, this is Sam"), so there is no rush to check a
# short hello.
CHECK_EVERY_SECONDS = 2.0
# After that, wait this long for a pause, so a check rarely cuts a word.
PAUSE_WAIT_SECONDS = 1.0
# Audio kept from before the first speech: VAD reports speech a little
# after it starts, and the greeting's first words must not be cut.
PREROLL_SECONDS = 1.0
# The line's audio kept for the check, from the preroll on. Longer than the
# settle window the check runs in.
MAX_AUDIO_SECONDS = 15.0
# The shared prompt is written for a live greeting with a postpone tool.
PARTIAL_NOTE = (
    "\nThis transcript is what the line has said so far. It may stop "
    "mid-greeting. Postponing is not available: answer uncertain when more "
    "words could still show a recording.\n"
)
# Deepgram models that also take a finished recording, with language
# detection. The same family check as ``amd.build_amd_models``; others,
# such as streaming-only ones, get Hail's configured model instead.
ONE_SHOT_MODEL_PREFIX = "nova-3"

# Transcribe the line's audio, then classify it: (transcript, category).
GreetingCheck = Callable[[rtc.AudioFrame], Awaitable[tuple[str, AMDCategory | None]]]


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


def _build_stt(org_cfgs: dict[str, ResolvedLayer]) -> STT | None:
    """A one-shot transcriber that follows the org's STT choice.

    Hail's Deepgram only when the org has no STT of its own, as in the
    outbound recheck (``amd.build_amd_models``). An org on Deepgram uses its
    own key, and its model when that model takes a recording
    (``ONE_SHOT_MODEL_PREFIX``). An org on another provider skips the
    check: its audio never goes to a provider it did not pick. Language
    detection is on: the greeting can be in any language, and Deepgram's
    default is English.
    """
    org_stt = org_cfgs.get("stt")
    if org_stt is None:
        model, api_key = settings.deepgram_model, settings.deepgram_api_key
    elif org_stt.provider == "deepgram":
        model = org_stt.params.get("model") or ""
        if not model.startswith(ONE_SHOT_MODEL_PREFIX):
            model = settings.deepgram_model
        # Hail's key only when the org allows fallback to Hail.
        api_key = org_stt.api_key or (
            settings.deepgram_api_key if org_stt.fallback_enabled else ""
        )
    else:
        return None
    if not (model and api_key):
        return None
    return recheck_stt(model, api_key)


def build_greeting_check(
    session: AgentSession,
    *,
    org_cfgs: dict[str, ResolvedLayer] | None = None,
    llm_override: bool = False,
) -> tuple[GreetingCheck | None, list]:
    """The check for :class:`ContactLine` and the clients to close at the
    end of the call (``amd._close_all``). Built once per call.

    None (no check, the line connects as before) when there is no
    transcriber or no classifier, or when building one fails.
    """
    owned: list = []
    try:
        stt = _build_stt(org_cfgs or {})
        if stt is None:
            logger.warning("handover machine check has no transcriber; skipping")
            return None, owned
        owned.append(stt)
        llm, classifiers = build_classifier(
            session, org_cfgs=org_cfgs, llm_override=llm_override
        )
        owned.extend(classifiers)
        if not isinstance(llm, LLM):
            logger.warning("handover machine check has no classifier; skipping")
            return None, owned
    except Exception:
        logger.exception("handover machine check setup failed; skipping")
        return None, owned

    async def check(audio: rtc.AudioFrame) -> tuple[str, AMDCategory | None]:
        return await transcribe_and_classify(
            audio,
            stt=stt,
            llm=llm,
            prompt=GREETING_CLASSIFICATION_PROMPT,
            note=PARTIAL_NOTE,
        )

    return check, owned


class ContactLine:
    """One read of the contact's leg for the whole check: VAD for speech,
    and the audio kept in memory for the mailbox check. Nothing is saved.

    :meth:`speaks` waits for the first speech. From then, when a ``check``
    is given, the line's audio so far is checked up to ``MAX_CHECKS``
    times, until a mailbox verdict, an error, or :meth:`aclose`.
    :meth:`machine` waits for that. The check's clients belong to the
    caller (one set per call).
    """

    def __init__(
        self,
        room: rtc.Room,
        identity: str,
        vad: VAD,
        *,
        check: GreetingCheck | None = None,
    ) -> None:
        self._room = room
        self._identity = identity
        self._vad = vad
        self._check = check
        self._limit = int(MAX_AUDIO_SECONDS * SAMPLE_RATE) * 2
        self._preroll = int(PREROLL_SECONDS * SAMPLE_RATE) * 2
        self._pcm = bytearray()
        self._spoke = asyncio.Event()
        self._ended = asyncio.Event()
        self._pause = asyncio.Event()
        self._failed = False
        self._read: asyncio.Task | None = None
        self._checking: asyncio.Task | None = None
        self._spoke_at: float | None = None
        self._category: AMDCategory | None = None
        self._transcript = ""
        self._reason = "window_ended"
        self._delay: float | None = None

    async def speaks(self, seconds: float) -> bool:
        """True when the contact starts speaking within ``seconds`` from now,
        or when the line's audio could not be read (connect, as before this
        check). False when they stay silent, publish no audio, or leave."""
        if self._read is None:
            self._read = asyncio.create_task(self._listen(), name="handover_line")
        spoke = asyncio.ensure_future(self._spoke.wait())
        ended = asyncio.ensure_future(self._ended.wait())
        try:
            await asyncio.wait(
                {spoke, ended}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            spoke.cancel()
            ended.cancel()
            await asyncio.gather(spoke, ended, return_exceptions=True)
        return self._spoke.is_set() or self._failed

    async def machine(self) -> bool:
        """True when the check said voicemail or an unavailable mailbox.
        False when it said anything else, failed, did not run, or stopped
        without that verdict. Never raises."""
        if self._checking is None:
            return False
        await asyncio.gather(self._checking, return_exceptions=True)
        return self._category in MACHINE_HANGUP_CATEGORIES

    def verdict(self) -> dict[str, Any] | None:
        """The ``handover_amd_result`` call event payload, shaped like
        ``amd_result``. None when the contact never spoke."""
        if self._spoke_at is None:
            return None
        return {
            "category": self._category.value if self._category else None,
            "transcript": self._transcript[:500] or None,
            "reason": self._reason if self._check else "detection_unavailable",
            "delay": round(self._delay, 3) if self._delay is not None else None,
        }

    async def aclose(self) -> None:
        tasks = [t for t in (self._checking, self._read) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pcm.clear()

    async def _listen(self) -> None:
        stream: rtc.AudioStream | None = None
        vad_stream = None
        feeder: asyncio.Task | None = None
        try:
            stream = await _contact_audio(self._room, self._identity)
            if stream is None:
                return
            vad_stream = self._vad.stream()

            async def _feed() -> None:
                try:
                    async for event in stream:
                        self._keep(event.frame)
                        vad_stream.push_frame(event.frame)
                finally:
                    vad_stream.end_input()

            feeder = asyncio.create_task(_feed(), name="handover_line_feed")
            async for event in vad_stream:
                if event.type == VADEventType.START_OF_SPEECH:
                    self._on_speech()
                elif event.type == VADEventType.END_OF_SPEECH:
                    self._pause.set()
        except Exception:
            # Fail open: a read or VAD error is not a silent line.
            self._failed = True
            logger.warning("handover line audio unavailable", exc_info=True)
        finally:
            # The contact's audio ended (they left) or the read failed.
            self._ended.set()
            self._pause.set()
            if feeder is not None:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)
            if stream is not None:
                await stream.aclose()
            if vad_stream is not None:
                await vad_stream.aclose()

    def _keep(self, frame: rtc.AudioFrame) -> None:
        """Keep the frame for the check: only the preroll until the first
        speech, then up to ``MAX_AUDIO_SECONDS``."""
        if len(self._pcm) >= self._limit:
            return
        self._pcm.extend(frame.data.cast("B")[: self._limit - len(self._pcm)])
        if not self._spoke.is_set() and len(self._pcm) > self._preroll:
            del self._pcm[: len(self._pcm) - self._preroll]

    def _on_speech(self) -> None:
        if self._spoke.is_set():
            return
        self._spoke_at = time.monotonic()
        self._spoke.set()
        if self._check is not None:
            self._checking = asyncio.create_task(
                self._check_greeting(), name="handover_machine_check"
            )

    async def _check_greeting(self) -> None:
        assert self._check is not None and self._spoke_at is not None
        checked = 0
        last = self._spoke_at
        for _ in range(MAX_CHECKS):
            await asyncio.sleep(max(0.0, last + CHECK_EVERY_SECONDS - time.monotonic()))
            if not self._ended.is_set():
                self._pause.clear()
                try:
                    await asyncio.wait_for(self._pause.wait(), PAUSE_WAIT_SECONDS)
                except TimeoutError:
                    pass
            if len(self._pcm) == checked:
                # No new audio: the line ended or the audio cap is full.
                return
            checked = len(self._pcm)
            last = time.monotonic()
            audio = rtc.AudioFrame(
                data=bytes(self._pcm),
                sample_rate=SAMPLE_RATE,
                num_channels=1,
                samples_per_channel=len(self._pcm) // 2,
            )
            try:
                transcript, category = await self._check(audio)
            except Exception as exc:
                # Provider error bodies can contain audio or transcripts.
                logger.warning("handover machine check failed (%s)", type(exc).__name__)
                self._reason = "detection_failed"
                return
            if transcript:
                self._transcript = transcript
            self._category = category
            if category in MACHINE_HANGUP_CATEGORIES:
                self._reason = "greeting_check"
                self._delay = time.monotonic() - self._spoke_at
                return


__all__ = [
    "CHECK_EVERY_SECONDS",
    "MAX_CHECKS",
    "ContactLine",
    "build_greeting_check",
]
