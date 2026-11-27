"""Short sounds said over the agent do not stop it; insisting does.

While the agent speaks, what the other party says reaches the session only
once they have spoken for ``INSIST_SECONDS`` without a break. An "okay", a
cough or one word from a television is shorter and is dropped, so the agent
keeps talking. Someone who keeps talking takes over after about a second.
The rule uses the length of speech, never its words, so it works the same in
every language. Outside agent speech nothing is dropped.

Known limit: a one-word answer said while the agent is still talking is
dropped too. The agent pauses between reply parts (reply_limit.py), and a
word said in a pause is heard.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import Any

from livekit.agents.stt import SpeechEventType

INSIST_SECONDS = 1.0
# A transcript arrives after its speech ended: how long that speech still counts.
RECENT_SECONDS = 3.0
_TEXT_EVENTS = (
    SpeechEventType.INTERIM_TRANSCRIPT,
    SpeechEventType.PREFLIGHT_TRANSCRIPT,
    SpeechEventType.FINAL_TRANSCRIPT,
)


class SpeechLength:
    """How long the other party has been speaking, from the session's
    ``user_state_changed`` events."""

    def __init__(self) -> None:
        self._started: float | None = None
        self._last_length = 0.0
        self._last_ended = -math.inf

    def on_state(self, state: str | None, now: float) -> None:
        if state == "speaking":
            if self._started is None:
                self._started = now
        elif self._started is not None:
            self._last_length = now - self._started
            self._last_ended = now
            self._started = None

    def insisting(self, now: float) -> bool:
        if self._started is not None:
            return now - self._started >= INSIST_SECONDS
        return (
            now - self._last_ended <= RECENT_SECONDS
            and self._last_length >= INSIST_SECONDS
        )


async def drop_short_speech(
    events: AsyncIterable[Any], drop: Callable[[], bool]
) -> AsyncIterator[Any]:
    """Pass speech events through, without the text ``drop()`` rejects."""
    async for event in events:
        if getattr(event, "type", None) in _TEXT_EVENTS and drop():
            continue
        yield event
