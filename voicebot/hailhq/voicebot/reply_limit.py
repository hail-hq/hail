"""A reply is spoken two sentences at a time, with a short pause in between.

The other party cannot interrupt the agent while it speaks (speaking_gate.py).
So a long reply is cut into parts of about two sentences. After each part the
agent is silent for a moment and listens: when the other party speaks, the rest
is dropped and their turn is answered; when they stay quiet, the next part is
spoken. Tool calls in a reply are kept, and nothing is continued after one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import Any

logger = logging.getLogger("hailhq.voicebot")
SENTENCES = 2
MIN_CHARS = 60  # two very short sentences ("Sure. Okay.") do not end a part
MAX_CHARS = 140  # a long sentence ends a part alone
PAUSE = 0.9  # silence after a part, in which the other party can start
SETTLE = 1.0  # after a sound in that pause: wait this long for its words
_END = ".!?\u2026"
_END_NO_SPACE = "\u3002\uff01\uff1f"  # CJK full stops need no space after


class ReplyLimiter:
    """Feed reply text piece by piece; get back the part to speak now."""

    def __init__(self) -> None:
        self.done = False
        self.rest = ""  # text after the cut
        self._chars = 0
        self._sentences = 0
        self._held = ""  # a possible sentence end waiting for its next char

    def feed(self, text: str) -> str:
        if self.done:
            self.rest += text
            return ""
        out = []
        for i, ch in enumerate(text):
            if self._held:
                if ch.isspace():
                    self._held += ch
                    continue
                # "3.5", "e.g. the" and "www.x" are not sentence ends.
                ended = self._held[-1].isspace() and not ch.islower()
                out.append(self._flush(ended))
                if self.done:
                    self.rest += text[i:]
                    break
            if ch in _END:
                self._held += ch
            else:
                self._chars += 1
                out.append(ch)
                if ch in _END_NO_SPACE:
                    self._count()
                    if self.done:
                        self.rest += text[i + 1 :]
                        break
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
        """The text ended: a held sentence end is spoken as it is."""
        held, self._held = self._held, ""
        return "" if self.done else held.rstrip()


def split_reply(text: str) -> tuple[str, str]:
    """(the part to speak now, the rest)."""
    limiter = ReplyLimiter()
    part = limiter.feed(text) + limiter.finish()
    return part.strip(), limiter.rest.strip()


async def limit_reply(
    stream: AsyncIterable[Any], on_rest: Callable[[str], None] | None = None
) -> AsyncIterator[Any]:
    """Pass a reply stream through, holding back text after the first part.

    The stream is read to its end, so a tool call that follows the text (a
    goodbye, then a hang-up) still runs. ``on_rest`` gets the held-back text,
    or nothing when the reply called a tool.
    """
    limiter = ReplyLimiter()
    called_tool = False
    async for chunk in stream:
        if isinstance(chunk, str):
            if text := limiter.feed(chunk):
                yield text
            continue
        delta = getattr(chunk, "delta", None)
        if getattr(delta, "tool_calls", None):
            called_tool = True
        content = getattr(delta, "content", None)
        if not content:
            yield chunk
            continue
        delta.content = limiter.feed(content) or None
        if delta.content or getattr(delta, "tool_calls", None):
            yield chunk
    if tail := limiter.finish():
        yield tail
    if on_rest is not None and not called_tool and limiter.rest.strip():
        on_rest(limiter.rest)


class ReplyContinuation:
    """Speaks the held-back rest of a reply, part by part, unless they speak."""

    def __init__(
        self,
        session: Any,
        *,
        can_continue: Callable[[], bool],
        extra_pause: Callable[[], float] = lambda: 0.0,
    ) -> None:
        self.session = session
        self.can_continue = can_continue
        self.extra_pause = extra_pause  # an echo delay the input is silent for
        self.rest = ""
        self._heard = False
        self._task: asyncio.Task | None = None

        @session.on("conversation_item_added")
        def item_added(event) -> None:
            item = getattr(event, "item", None)
            role = getattr(item, "role", None)
            if role == "user" or getattr(item, "interrupted", False):
                self.drop()
            elif role == "assistant" and self.rest and self._task is None:
                self._task = asyncio.create_task(self._continue(), name="reply_rest")

        @session.on("user_input_transcribed")
        def heard(event) -> None:
            if (getattr(event, "transcript", "") or "").strip():
                self._heard = True

        @session.on("close")
        def closed(_) -> None:
            self.drop()

    def keep(self, rest: str) -> None:
        self.rest = rest.strip()
        self._heard = False

    def drop(self) -> None:
        self.rest = ""
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _user_speaking(self) -> bool:
        return getattr(self.session, "user_state", None) == "speaking"

    async def _continue(self) -> None:
        try:
            await asyncio.sleep(PAUSE + self.extra_pause())
            if self._user_speaking():
                # A sound is not yet a turn: give its words time to arrive.
                while self._user_speaking():
                    await asyncio.sleep(0.1)
                await asyncio.sleep(SETTLE)
            if (
                self._heard
                or not self.rest
                or not self.can_continue()
                or getattr(self.session, "agent_state", None) != "listening"
            ):
                self.rest = ""
                return
            part, self.rest = split_reply(self.rest)
            if part:
                self.session.say(part)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("could not continue the reply; waiting for them")
            self.rest = ""
        finally:
            self._task = None
