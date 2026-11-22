"""Only the first two sentences of a reply are spoken.

The other party cannot interrupt the agent while it speaks (speaking_gate.py),
so each reply must be short. The voice instructions ask for one or two
sentences; this enforces it when the model writes more. Tool calls in the same
reply are kept.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

SENTENCES = 2
MIN_CHARS = 60  # two very short sentences ("Sure. Okay.") do not end a reply
MAX_CHARS = 240  # one long run-on ends at its next sentence end
_END = ".!?\u2026"
_END_NO_SPACE = "\u3002\uff01\uff1f"  # CJK full stops need no space after


class ReplyLimiter:
    """Feed reply text piece by piece; get back the part to speak."""

    def __init__(self) -> None:
        self.done = False
        self._chars = 0
        self._sentences = 0
        self._held = ""  # a possible sentence end waiting for its next char

    def feed(self, text: str) -> str:
        if self.done:
            return ""
        out = []
        for ch in text:
            if self._held:
                if ch.isspace():
                    self._held += ch
                    continue
                # "3.5", "e.g. the" and "www.x" are not sentence ends.
                ended = self._held[-1].isspace() and not ch.islower()
                out.append(self._flush(ended))
                if self.done:
                    break
            if ch in _END:
                self._held += ch
            elif ch in _END_NO_SPACE:
                self._chars += 1
                out.append(ch)
                self._count()
                if self.done:
                    break
            else:
                self._chars += 1
                out.append(ch)
        return "".join(out)

    def _flush(self, ended: bool) -> str:
        held, self._held = self._held, ""
        if not ended:
            self._chars += len(held)
            return held
        mark = held.rstrip()
        self._chars += len(mark)
        self._count()
        return mark if self.done else held

    def _count(self) -> None:
        self._sentences += 1
        if (self._sentences >= SENTENCES and self._chars >= MIN_CHARS) or (
            self._chars >= MAX_CHARS
        ):
            self.done = True

    def finish(self) -> str:
        """The stream ended: a held sentence end is spoken as it is."""
        held, self._held = self._held, ""
        return "" if self.done else held.rstrip()


async def limit_reply(stream: AsyncIterable[Any]) -> AsyncIterator[Any]:
    """Pass a reply stream through, dropping text after the limit.

    The stream is read to its end, so a tool call that follows the text (a
    goodbye, then a hang-up) still runs.
    """
    limiter = ReplyLimiter()
    async for chunk in stream:
        if isinstance(chunk, str):
            if text := limiter.feed(chunk):
                yield text
            continue
        delta = getattr(chunk, "delta", None)
        content = getattr(delta, "content", None)
        if not content:
            yield chunk
            continue
        delta.content = limiter.feed(content) or None
        if delta.content or getattr(delta, "tool_calls", None):
            yield chunk
    if tail := limiter.finish():
        yield tail
