"""Filler words said over the agent do not stop it.

While the agent speaks, a transcript made only of acknowledgments ("okay",
"mhmm", "yeah", a repeated "hello") is dropped before the session sees it, so
the agent keeps talking. Any other word passes: "stop", "no", "yes" and "wait"
stop the agent as before, also as a single word. Outside agent speech nothing
is dropped: an "okay" in a silence is an answer.

English only. In other languages every word still stops the agent.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import Any

from livekit.agents.stt import SpeechEventType

_TEXT_EVENTS = (
    SpeechEventType.INTERIM_TRANSCRIPT,
    SpeechEventType.PREFLIGHT_TRANSCRIPT,
    SpeechEventType.FINAL_TRANSCRIPT,
)
_ACKNOWLEDGMENTS = frozenset(
    [
        "ok",
        "okay",
        "k",
        "kay",
        "mhm",
        "mhmm",
        "mm",
        "mmm",
        "hmm",
        "hm",
        "uh",
        "huh",
        "uhhuh",
        "um",
        "umm",
        "yeah",
        "yep",
        "yup",
        "right",
        "alright",
        "sure",
        "hello",
        "hi",
        "hey",
        "oh",
        "ah",
        "i",
        "see",
        "got",
        "it",
        "cool",
        "great",
        "nice",
    ]
)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def only_acknowledgments(text: str) -> bool:
    words = [w.lower() for w in _WORD.findall(text or "")]
    return bool(words) and all(w in _ACKNOWLEDGMENTS for w in words)


async def drop_acknowledgments(
    events: AsyncIterable[Any], agent_speaking: Callable[[], bool]
) -> AsyncIterator[Any]:
    """Pass speech events through, without filler said over the agent."""
    async for event in events:
        if getattr(event, "type", None) in _TEXT_EVENTS and agent_speaking():
            alternatives = getattr(event, "alternatives", None) or []
            text = alternatives[0].text if alternatives else ""
            if only_acknowledgments(text):
                continue
        yield event
