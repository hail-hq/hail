"""Bounded recovery when noise or a lost transcript stalls a live conversation.

VAD activity is deliberately not progress: otherwise continuous noise would
keep resetting the timer. Interim STT protects legitimate long utterances.
https://docs.livekit.io/reference/agents/events/
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from uuid import UUID

from livekit.agents import AgentSession

logger = logging.getLogger("hailhq.voicebot")


class ConversationRecovery:
    def __init__(
        self,
        session: AgentSession,
        *,
        on_exhausted: Callable[[], Awaitable[None]],
        can_recover: Callable[[], bool],
        timeout: float = 20.0,
        call_id: UUID | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._on_exhausted = on_exhausted
        self._can_recover = can_recover
        self._timeout = timeout
        self._clock = clock
        self._call_id = call_id
        self._last_progress = clock()
        self._attempted = False
        self._finished = False
        self._task: asyncio.Task | None = None

        @session.on("user_input_transcribed")
        def on_transcript(event) -> None:
            if event.transcript.strip():
                self._last_progress = clock()
                # Interim text protects an ongoing utterance, but may never
                # become a usable turn. Only final speech earns another prompt.
                if event.is_final:
                    self._attempted = False

        @session.on("agent_state_changed")
        def on_agent_state(event) -> None:
            if event.new_state in ("speaking", "thinking"):
                self._last_progress = clock()

        @session.on("close")
        def on_close(_event) -> None:
            was_finished = self._finished
            self._finished = True
            if self._task and not was_finished:
                self._task.cancel()

    def start(self) -> None:
        if self._task is None and self._timeout > 0 and not self._finished:
            self._last_progress = self._clock()
            self._task = asyncio.create_task(self._run(), name="conversation_recovery")

    async def check(self) -> None:
        if self._finished or self._timeout <= 0:
            return
        if not self._can_recover() or self._session.agent_state in (
            "speaking",
            "thinking",
        ):
            self._last_progress = self._clock()
            return
        if self._clock() - self._last_progress < self._timeout:
            return
        self._last_progress = self._clock()
        if self._attempted:
            self._finished = True
            logger.warning(
                "call_id=%s recovery exhausted; ending stalled call", self._call_id
            )
            await self._on_exhausted()
            return
        self._attempted = True
        logger.warning(
            "call_id=%s conversation stalled; asking caller to repeat", self._call_id
        )
        self._session.clear_user_turn()
        # No tools during a recovery prompt. Uninterruptible playout breaks a
        # repeated noise/false-interruption loop; the prompt uses the call language.
        self._session.generate_reply(
            instructions=(
                "In the conversation's language, briefly tell the caller you are "
                "having trouble hearing them and ask them to repeat. One sentence."
            ),
            tool_choice="none",
            allow_interruptions=False,
        )

    async def _run(self) -> None:
        while not self._finished:
            await asyncio.sleep(min(1.0, self._timeout))
            try:
                await self.check()
            except Exception:
                logger.exception("conversation recovery failed")

    async def aclose(self) -> None:
        self._finished = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
