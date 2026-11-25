"""A soft typing sound while a slow tool runs, so the line is not dead air.

Only tool calls get it, and only after ``DELAY_SECONDS``: a quick lookup
stays silent, and the agent's own words are never covered. It plays on a
separate track through LiveKit's ``BackgroundAudioPlayer`` (livekit-agents
1.8.8, ``voice/background_audio.py``). Everything here fails open: a sound
that cannot start never affects the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from livekit import rtc
from livekit.agents import AgentSession
from livekit.agents.voice.background_audio import (
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    PlayHandle,
)

logger = logging.getLogger("hailhq.voicebot")

# A tool slower than this gets the sound.
DELAY_SECONDS = 1.5
# How often to look again while the agent is still speaking.
POLL_SECONDS = 0.25
# Publishing the track must not hold up the start of the call.
START_TIMEOUT_SECONDS = 2.0
SOUND = AudioConfig(
    BuiltinAudioClip.KEYBOARD_TYPING, volume=0.5, fade_in=0.3, fade_out=0.3
)


class BusySound:
    def __init__(
        self,
        *,
        can_play: Callable[[], bool] = lambda: True,
        player_factory: Callable[[], Any] = BackgroundAudioPlayer,
    ) -> None:
        self._can_play = can_play
        self._player_factory = player_factory
        self._player: Any = None
        self._session: AgentSession | None = None

    async def start(self, room: rtc.Room, session: AgentSession) -> None:
        player = None
        try:
            player = self._player_factory()
            await asyncio.wait_for(player.start(room=room), START_TIMEOUT_SECONDS)
        except Exception:
            logger.warning("busy sound unavailable", exc_info=True)
            if player is not None:
                try:
                    await player.aclose()
                except Exception:
                    logger.warning("busy sound close failed", exc_info=True)
            return
        self._player, self._session = player, session

    @contextlib.asynccontextmanager
    async def during(self) -> AsyncIterator[None]:
        """Play the sound while the body runs longer than DELAY_SECONDS."""
        if self._player is None:
            yield
            return
        task = asyncio.create_task(self._play_later())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _play_later(self) -> None:
        await asyncio.sleep(DELAY_SECONDS)
        session = self._session
        while session is not None and session.agent_state == "speaking":
            await asyncio.sleep(POLL_SECONDS)
        if not self._can_play():
            return
        handle: PlayHandle | None = None
        try:
            handle = self._player.play(SOUND, loop=True)
        except Exception:
            logger.warning("busy sound did not play", exc_info=True)
            return
        try:
            await asyncio.Event().wait()
        finally:
            try:
                handle.stop()
            except Exception:
                logger.warning("busy sound did not stop", exc_info=True)

    async def aclose(self) -> None:
        player, self._player = self._player, None
        if player is None:
            return
        try:
            await player.aclose()
        except Exception:
            logger.warning("busy sound close failed", exc_info=True)
