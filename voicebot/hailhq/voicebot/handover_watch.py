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
    AMDModels,
    build_classifier,
    recheck_stt,
)
from hailhq.voicebot.pickup import (
    RECHECK_TIMEOUT_SECONDS,
    SAMPLE_RATE,
    transcribe_and_classify,
)
from hailhq.voicebot.pipeline import ResolvedLayer
from livekit import rtc
from livekit.agents.llm import LLM
from livekit.agents.stt import STT
from livekit.agents.utils.participant import wait_for_track_publication
from livekit.agents.vad import VAD, VADEventType
from livekit.agents.voice import AgentSession
from livekit.agents.voice.amd import AMDCategory

logger = logging.getLogger("hailhq.voicebot")

# Each check sends all the audio so far, so their number is capped.
MAX_CHECKS = 3
# Check 1 waits for this much speech (VAD), so a blip or a one-word "Hi"
# does not use it up.
FIRST_CHECK_SPEECH_SECONDS = 2.0
# A later check starts at least this long after the previous one started,
# and never before it ends.
CHECK_EVERY_SECONDS = 2.0
# Then it waits this long for a pause, so a check rarely cuts a word. A
# pause counts only while the line is still silent.
PAUSE_WAIT_SECONDS = 1.0
#
# Timing, from the first speech. One check (one-shot transcription plus one
# classifier call) takes about 1.5 to 2.5 s, capped at
# ``RECHECK_TIMEOUT_SECONDS`` (5 s). The bridge decides at the end of its
# 10 s settle window, and waits up to 3 s more for a check still running
# (``window_seconds`` is 13 s in all), so a check starts only up to
# 13 - 5 = 8 s in. A voicemail speaks without long stops: check 1 at about
# 2 to 3 s (done by about 4 to 5 s), check 2 at about 4.5 to 6 s (done by
# about 7 to 8 s), check 3 at about 7 to 8 s, if time allows (done by about
# 10 s). A recorded greeting gives its cue ("you've reached", "leave a
# message") in its first seconds.
#
# Audio kept from before the first speech: VAD reports speech a little
# after it starts, and the greeting's first words must not be cut.
PREROLL_SECONDS = 1.0
# The line's audio kept for the check, from the preroll on. Longer than the
# window the checks run in.
MAX_AUDIO_SECONDS = 15.0
# The shared prompt is written for a live greeting with a postpone tool.
PARTIAL_NOTE = (
    "\nThis transcript is what the line has said so far. It may stop "
    "mid-greeting. Postponing is not available: answer uncertain when more "
    "words could still show a recording.\n"
)

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

    Always Hail's one-shot model, ``DEEPGRAM_MODEL``, as in the outbound
    recheck: an org's own Deepgram model may be streaming-only. The key is Hail's when the org has
    no STT of its own, as in the outbound recheck, and the org's own when it
    is on Deepgram. An org on another provider skips the check: its audio
    never goes to a provider it did not pick. Language detection is on: the
    greeting can be in any language, and Deepgram's default is English.
    """
    org_stt = org_cfgs.get("stt")
    if org_stt is None:
        api_key = settings.deepgram_api_key
    elif org_stt.provider == "deepgram":
        # Hail's key only when the org allows fallback to Hail.
        api_key = org_stt.api_key or (
            settings.deepgram_api_key if org_stt.fallback_enabled else ""
        )
    else:
        return None
    # The model the outbound recheck uses (amd.build_amd_models).
    model = settings.deepgram_model
    if not (model and api_key):
        return None
    return recheck_stt(model, api_key)


def build_check_models(
    session: AgentSession,
    *,
    org_cfgs: dict[str, ResolvedLayer] | None = None,
    llm_override: bool = False,
) -> AMDModels:
    """The handover check's clients, built once per call; close them with
    ``AMDModels.aclose`` at job end. ``recheck_stt`` is the transcriber and
    ``llm`` the classifier; either is None when unavailable, and then
    :func:`greeting_check` gives no check (the line connects as before)."""
    models = AMDModels(llm=None, stt=None)
    try:
        stt = _build_stt(org_cfgs or {})
        if stt is None:
            logger.warning("handover machine check has no transcriber; skipping")
            return models
        models.recheck_stt = stt
        models.owned.append(stt)
        llm, models.classifiers = build_classifier(
            session, org_cfgs=org_cfgs, llm_override=llm_override
        )
        if not isinstance(llm, LLM):
            logger.warning("handover machine check has no classifier; skipping")
            return models
        models.llm = llm
    except Exception:
        logger.exception("handover machine check setup failed; skipping")
    return models


def greeting_check(models: AMDModels) -> GreetingCheck | None:
    """The check for :class:`ContactLine`, or None without a transcriber or
    a classifier."""
    stt, llm = models.recheck_stt, models.llm
    if stt is None or not isinstance(llm, LLM):
        return None

    async def check(audio: rtc.AudioFrame) -> tuple[str, AMDCategory | None]:
        return await transcribe_and_classify(
            audio,
            stt=stt,
            llm=llm,
            prompt=GREETING_CLASSIFICATION_PROMPT,
            note=PARTIAL_NOTE,
        )

    return check


class ContactLine:
    """One read of the contact's leg for the whole check: VAD for speech,
    and the audio kept in memory for the mailbox check. Nothing is saved.

    :meth:`speaks` waits for the first speech. From then, when a ``check``
    is given, the line's audio so far is checked up to ``MAX_CHECKS``
    times, until a mailbox verdict, an error, the end of ``window_seconds``
    (from the first speech; None: no end), or :meth:`stop`.
    :meth:`machine` waits for that. The bridge records what it did with
    :meth:`decide`, and :meth:`verdict` is the call event row. The check's
    clients belong to the caller (one set per call).
    """

    def __init__(
        self,
        room: rtc.Room,
        identity: str,
        vad: VAD,
        *,
        check: GreetingCheck | None = None,
        window_seconds: float | None = None,
        no_check_reason: str = "detection_unavailable",
    ) -> None:
        self._room = room
        self._identity = identity
        self._vad = vad
        self._check = check
        self._window = window_seconds
        self._limit = int(MAX_AUDIO_SECONDS * SAMPLE_RATE) * 2
        self._preroll = int(PREROLL_SECONDS * SAMPLE_RATE) * 2
        self._pcm = bytearray()
        self._spoke = asyncio.Event()
        self._ended = asyncio.Event()
        # Set while the line is silent after speech (VAD end of speech, and
        # no start since).
        self._pause = asyncio.Event()
        self._failed = False
        self._stopped = False
        self._in_flight = False
        self._read: asyncio.Task | None = None
        self._checking: asyncio.Task | None = None
        self._spoke_at: float | None = None
        self._speech_seconds = 0.0
        self._speech_from: float | None = None
        self._category: AMDCategory | None = None
        self._transcript = ""
        self._reason = "window_ended" if check is not None else no_check_reason
        self._delay: float | None = None
        self._dropped = False

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
        without that verdict. Never raises. Cancelling it cancels the check
        (a check still running then is recorded as ``timeout``)."""
        if self._checking is None:
            return False
        await asyncio.gather(self._checking, return_exceptions=True)
        return self._category in MACHINE_HANGUP_CATEGORIES

    @property
    def is_mailbox(self) -> bool:
        """The last finished check said voicemail or an unavailable mailbox."""
        return self._category in MACHINE_HANGUP_CATEGORIES

    def stop(self, reason: str | None = None) -> None:
        """Stop the checks now, and start no new one. ``reason`` replaces
        the recorded reason (job shutdown: ``call_ending``)."""
        self._stopped = True
        if reason is not None:
            self._reason = reason
        if self._checking is not None and not self._checking.done():
            if self._in_flight and reason is None:
                self._reason = "timeout"
            self._checking.cancel()

    def decide(self, *, dropped: bool = False, reason: str | None = None) -> None:
        """What the bridge did: ``dropped`` when it took the caller back
        because of the verdict; ``reason`` for another end (``leg_ended``,
        ``call_ending``)."""
        self.stop()
        self._dropped = dropped
        if reason is not None:
            self._reason = reason

    def verdict(self) -> dict[str, Any] | None:
        """The ``handover_amd_result`` call event payload, shaped like
        ``amd_result`` plus ``dropped``. None when the contact never spoke."""
        if self._spoke_at is None:
            return None
        return {
            "category": self._category.value if self._category else None,
            "transcript": self._transcript[:500] or None,
            "reason": self._reason,
            "delay": round(self._delay, 3) if self._delay is not None else None,
            "dropped": self._dropped,
        }

    async def aclose(self) -> None:
        self.stop()
        tasks = [t for t in (self._checking, self._read) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pcm.clear()

    def _audio_failed(self) -> None:
        # Fail open: a read or VAD error is not a silent line.
        self._failed = True
        if self._spoke_at is not None and self._reason == "window_ended":
            self._reason = "audio_error"

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
                except Exception:
                    self._audio_failed()
                    logger.warning("handover line audio read failed", exc_info=True)
                finally:
                    vad_stream.end_input()

            feeder = asyncio.create_task(_feed(), name="handover_line_feed")
            async for event in vad_stream:
                if event.type == VADEventType.START_OF_SPEECH:
                    self._on_speech()
                elif event.type == VADEventType.END_OF_SPEECH:
                    self._on_silence()
        except Exception:
            self._audio_failed()
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
        # Speaking again: an earlier pause no longer counts.
        self._pause.clear()
        now = time.monotonic()
        if self._speech_from is None:
            self._speech_from = now
        if self._spoke.is_set():
            return
        self._spoke_at = now
        self._spoke.set()
        if self._check is not None and not self._stopped:
            self._checking = asyncio.create_task(
                self._check_greeting(), name="handover_machine_check"
            )

    def _on_silence(self) -> None:
        if self._speech_from is not None:
            self._speech_seconds += time.monotonic() - self._speech_from
            self._speech_from = None
        self._pause.set()

    def _speech_so_far(self) -> float:
        open_part = (
            time.monotonic() - self._speech_from if self._speech_from is not None else 0
        )
        return self._speech_seconds + open_part

    async def _wait_pause(self, seconds: float) -> None:
        """Up to ``seconds`` for the line to be silent; it may already be."""
        if seconds <= 0 or self._ended.is_set():
            return
        try:
            await asyncio.wait_for(self._pause.wait(), seconds)
        except TimeoutError:
            pass

    async def _check_greeting(self) -> None:
        try:
            await self._run_checks()
        except asyncio.CancelledError:
            # Cancelled mid-check (the bridge stopped waiting): the verdict
            # is the last one that finished.
            if self._in_flight and self._reason == "window_ended":
                self._reason = "timeout"
            raise
        finally:
            self._in_flight = False

    async def _run_checks(self) -> None:
        assert self._check is not None and self._spoke_at is not None
        last_start = (
            self._spoke_at + self._window - RECHECK_TIMEOUT_SECONDS
            if self._window is not None
            else None
        )

        def _late() -> bool:
            return last_start is not None and time.monotonic() > last_start

        def _left() -> float:
            if last_start is None:
                return PAUSE_WAIT_SECONDS
            return min(PAUSE_WAIT_SECONDS, last_start - time.monotonic())

        checked = 0
        started = self._spoke_at
        for n in range(MAX_CHECKS):
            if n == 0:
                # Enough speech first, then a pause.
                while self._speech_so_far() < FIRST_CHECK_SPEECH_SECONDS:
                    if self._ended.is_set() or _late():
                        break
                    await asyncio.sleep(0.05)
            else:
                await asyncio.sleep(
                    max(0.0, started + CHECK_EVERY_SECONDS - time.monotonic())
                )
            await self._wait_pause(_left())
            if _late():
                # It could not finish before the bridge decides.
                return
            if len(self._pcm) == checked:
                # No new audio: the line ended or the audio cap is full.
                return
            checked = len(self._pcm)
            started = time.monotonic()
            audio = rtc.AudioFrame(
                data=bytes(self._pcm),
                sample_rate=SAMPLE_RATE,
                num_channels=1,
                samples_per_channel=len(self._pcm) // 2,
            )
            # Left set on cancel: _check_greeting records the timeout.
            self._in_flight = True
            try:
                transcript, category = await self._check(audio)
            except Exception as exc:
                self._in_flight = False
                # Provider error bodies can contain audio or transcripts.
                logger.warning("handover machine check failed (%s)", type(exc).__name__)
                self._reason = "detection_failed"
                return
            self._in_flight = False
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
    "build_check_models",
    "greeting_check",
]
