"""Hold: the agent stays quiet while the other party looks something up.

The model calls ``wait_for_other_party`` when someone asks it to wait. While
holding, conversation recovery is paused and nothing prompts the agent:

- Words end the hold, except a recorded hold or queue announcement, which
  the remote-speech classifier labels ``machine-ivr``.
- A voice with no words (``QUIET_VOICE_SECONDS`` up to ``MUSIC_SECONDS``)
  ends the hold: the agent says it can't quite hear them.
- Audio with no words for ``MUSIC_SECONDS`` or longer is hold music. It
  keeps the hold, and later wordless audio in that hold is music too.
- VAD splits music and chatter into bursts: bursts less than ``GAP_SECONDS``
  apart are one stretch, measured with its pauses and judged once it ends.
  Any transcript in it, interim or final, makes it speech.
- After ``HOLD_SECONDS`` of silence, the agent asks once whether they are
  still there, and normal recovery resumes.

Prompts are instructions, so the model speaks them in the call's language
(like :mod:`hailhq.voicebot.recovery`).
https://docs.livekit.io/reference/agents/events/
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from uuid import UUID

from hailhq.voicebot.ivr import try_classify_remote_speech
from livekit.agents import AgentSession
from livekit.agents.llm import LLM
from livekit.agents.voice.amd import AMDCategory

logger = logging.getLogger("hailhq.voicebot")

# Silence on the line before the agent checks in once.
HOLD_SECONDS = 120.0
# Wordless audio this long is a person speaking too softly to transcribe.
QUIET_VOICE_SECONDS = 2.0
# Wordless audio this long is music, not a person.
MUSIC_SECONDS = 10.0
# Bursts closer than this are one stretch of audio. It is also the time a
# late transcript gets after the audio ends.
GAP_SECONDS = 2.5

CANT_HEAR = (
    "In the conversation's language, briefly tell them you can't quite hear "
    "them and ask them to speak up. One sentence."
)
CHECK_IN = (
    "They asked you to wait and the line has been silent since. In the "
    "conversation's language, briefly ask whether they are still there. One "
    "short sentence."
)
# Added to the turn that ends a hold with words.
HOLD_ENDED_NOTE = (
    "You were waiting on hold. If these words were not meant for you, such as "
    "talk with someone else, use wait_for_other_party again and say nothing."
)


class Hold:
    def __init__(
        self,
        session: AgentSession,
        *,
        classifier: Callable[[], LLM | None] = lambda: None,
        can_speak: Callable[[], bool] = lambda: True,
        call_id: UUID | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._classifier = classifier
        self._can_speak = can_speak
        self._call_id = call_id
        self._clock = clock
        self.active = False
        self._music = False
        self._last_audio = clock()
        self._burst_started: float | None = None
        self._last_end = -math.inf
        # Start of the current stretch of audio: VAD bursts less than
        # GAP_SECONDS apart, pauses included.
        self._stretch_started: float | None = None
        self._words_at = -math.inf
        self._task: asyncio.Task[None] | None = None
        self._settle_task: asyncio.Task[None] | None = None
        self._closed = False

        @session.on("user_state_changed")
        def on_user_state(event) -> None:
            self._on_user_state(event.new_state)

        @session.on("user_input_transcribed")
        def on_transcript(event) -> None:
            # Any transcript, interim or final, means words.
            if event.transcript.strip():
                self._words_at = self._last_audio = self._clock()

        @session.on("close")
        def on_close(_event) -> None:
            self._closed = True
            self.active = False
            for task in (self._task, self._settle_task):
                if task is not None:
                    task.cancel()

    async def start(self) -> None:
        """The ``wait_for_other_party`` tool: hold from now."""
        if self._closed:
            return
        now = self._clock()
        self.active = True
        self._music = False
        self._last_audio = now
        # Audio before the hold (their "please hold") is not part of it.
        self._stretch_started = now if self._burst_started is not None else None
        self._last_end = -math.inf
        if self._settle_task is not None:
            self._settle_task.cancel()
        logger.info("call_id=%s holding", self._call_id)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="hold")

    def stop(self) -> None:
        """End the hold without a prompt (a person just answered)."""
        self.active = False

    async def keep_holding(self, text: str) -> bool:
        """Words arrived while holding. True keeps the hold (a recorded
        announcement); False ends it so the agent answers."""
        if not self.active:
            return False
        self._last_audio = self._clock()
        llm = self._classifier()
        if llm is not None and text.strip():
            category = await try_classify_remote_speech(llm, text)
            if category == AMDCategory.MACHINE_IVR and self.active:
                self._music = True
                return True
        self.active = False
        return False

    def _on_user_state(self, state: str | None) -> None:
        now = self._clock()
        if state == "speaking":
            if self._stretch_started is None or now - self._last_end > GAP_SECONDS:
                self._stretch_started = now
            if self._settle_task is not None:
                self._settle_task.cancel()
            self._burst_started = self._last_audio = now
            return
        if self._burst_started is None:
            return
        self._burst_started = None
        self._last_end = self._last_audio = now
        if self.active:
            self._settle_task = asyncio.create_task(self._settle(now))

    async def _settle(self, ended: float) -> None:
        """Judge a stretch of audio once it is over: more audio can continue
        it, and a late transcript can still come."""
        await asyncio.sleep(GAP_SECONDS)
        started = self._stretch_started
        if not self.active or self._burst_started is not None or started is None:
            return
        if self._words_at >= started:
            return  # words: the turn itself ends or keeps the hold
        length = ended - started
        if length >= MUSIC_SECONDS:
            self._music = True
            return
        if self._music or length < QUIET_VOICE_SECONDS:
            return
        self.active = False
        logger.info("call_id=%s hold ended: voice without words", self._call_id)
        self._say(CANT_HEAR)

    async def _run(self) -> None:
        while not self._closed:
            await asyncio.sleep(1.0)
            try:
                self.check()
            except Exception:
                logger.exception("call_id=%s hold check failed", self._call_id)

    def check(self) -> None:
        if not self.active:
            return
        if self._burst_started is not None:
            self._last_audio = self._clock()
            return
        if self._clock() - self._last_audio < HOLD_SECONDS:
            return
        self.active = False
        logger.info("call_id=%s hold timed out; checking in", self._call_id)
        self._say(CHECK_IN)

    def _say(self, instructions: str) -> None:
        if self._closed or not self._can_speak():
            return
        if self._session.agent_state == "speaking":
            return
        # Instructions, never a bare reply: a reply request that ends with the
        # agent's own line is refused by Claude models (no prefill).
        self._session.generate_reply(instructions=instructions, tool_choice="none")

    async def aclose(self) -> None:
        self._closed = True
        self.active = False
        tasks = [t for t in (self._task, self._settle_task) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
