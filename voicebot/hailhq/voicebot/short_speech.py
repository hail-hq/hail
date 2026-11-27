"""Short sounds said over the agent do not stop it; insisting does.

Speech that starts while the agent is speaking reaches the session only once
it adds up to ``INSIST_SECONDS`` within a few seconds. An "okay", a cough or
one word from a television is shorter and is dropped, so the agent keeps
talking and does not answer it. Someone who keeps talking, or repeats a word,
takes over after about a second, and their first words are kept. The rule
uses the length of speech, never its words, so it works the same in every
language. Speech that starts while the agent is silent is never dropped.

Known limits:
- A one-word answer said while the agent is still talking is dropped. The
  agent pauses between reply parts (reply_limit.py), and a word said in a
  pause is heard.
- While a slow model is still writing a reply, the agent counts as speaking
  although its first part has already played.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import Any, Literal

from livekit.agents.stt import SpeechEventType

INSIST_SECONDS = 1.0
# Speech this recent still counts: a transcript arrives after its speech
# ended, and repeated short words add up.
RECENT_SECONDS = 3.0
# The session reports the end of speech this long after it ended (the VAD's
# silence wait), so a finished stretch is shorter than the time between events.
VAD_SILENCE_SECONDS = 0.55
_TEXT_EVENTS = (
    SpeechEventType.INTERIM_TRANSCRIPT,
    SpeechEventType.PREFLIGHT_TRANSCRIPT,
    SpeechEventType.FINAL_TRANSCRIPT,
)
Verdict = Literal["pass", "drop", "insist"]


class SpeechLength:
    """How long the other party has spoken over the agent, from the session's
    ``user_state_changed`` events."""

    def __init__(self) -> None:
        self._started: float | None = None
        self._over_agent = False
        # (ended, length, started over the agent)
        self._done: deque[tuple[float, float, bool]] = deque(maxlen=8)

    def on_state(
        self, state: str | None, now: float, agent_speaking: bool = False
    ) -> None:
        if state == "speaking":
            if self._started is None:
                self._started = now
                self._over_agent = agent_speaking
        elif self._started is not None:
            length = max(0.0, now - self._started - VAD_SILENCE_SECONDS)
            self._done.append((now, length, self._over_agent))
            self._started = None

    def verdict(self, now: float) -> Verdict:
        recent = [d for d in self._done if now - d[0] <= RECENT_SECONDS]
        if self._started is not None:
            if not self._over_agent:
                return "pass"
            total = now - self._started
        elif not recent or not recent[-1][2]:
            return "pass"  # nothing known, or they spoke into a silence
        else:
            total = 0.0
        total += sum(length for _, length, over in recent if over)
        return "insist" if total >= INSIST_SECONDS else "drop"


async def drop_short_speech(
    events: AsyncIterable[Any], verdict: Callable[[], Verdict]
) -> AsyncIterator[Any]:
    """Pass speech events through, without text that gets a "drop" verdict.

    Dropped final transcripts are kept for a moment: when the other party
    turns out to be insisting, their first words are passed on first.
    """
    held: deque[Any] = deque(maxlen=4)
    async for event in events:
        if getattr(event, "type", None) not in _TEXT_EVENTS:
            yield event
            continue
        decision = verdict()
        if decision == "drop":
            if event.type == SpeechEventType.FINAL_TRANSCRIPT:
                held.append(event)
            continue
        if decision == "insist":
            while held:
                yield held.popleft()
        held.clear()
        yield event
