"""A reply is spoken about two sentences at a time, with a short pause between.

A long reply leaves the other party no room to speak. So it is cut into parts
of about two sentences. After each part the agent is silent for a moment: when
the other party speaks, the rest is dropped and their turn is answered; when
they stay quiet, the next part is spoken. A reply that calls a tool is spoken
whole.

Known limits:
- The length limits count characters and are sized for Latin text. Chinese,
  Japanese and Thai replies are cut later than they should be.
- A short word said in the pause ("mhm") is a turn like any other: the rest is
  dropped and the model answers it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import Any

from livekit.agents.types import FlushSentinel

logger = logging.getLogger("hailhq.voicebot")
SENTENCES = 2
MIN_CHARS = 60  # two very short sentences ("Sure. Okay.") do not end a part
MAX_CHARS = 90  # a sentence this long (about 6 s of speech) ends a part alone
HARD_CHARS = 300  # with no sentence end by here, the next space ends the part
HARD_SLACK = 80  # and with no space either, the part ends this much later
PAUSE = 0.9  # silence after a part, in which the other party can start
SETTLE = 1.0  # after a sound in that pause: wait this long for its words
# Sentence ends that are followed by a space: Latin, Devanagari and Bengali
# danda, Arabic question mark and Urdu full stop.
_END = ".!?\u2026\u0964\u0965\u061f\u06d4"
_END_NO_SPACE = "\u3002\uff01\uff1f"  # CJK full stops need no space after
# A full stop after one of these, after a single letter ("p.m.", "e.g.") or
# after a number ("24. November") does not end a sentence.
_ABBREVIATIONS = frozenset(
    [
        "mr",
        "mrs",
        "ms",
        "dr",
        "st",
        "prof",
        "sr",
        "jr",
        "vs",
        "etc",
        "nr",
        "inc",
        "ltd",
        "co",
        "mt",
        "ave",
        "approx",
        "ca",
        "tel",
        "bzw",
        "usw",
        "sog",
        "ggf",
        "evtl",
        "str",
        "mme",
        "mlle",
        "sra",
        "srta",
    ]
)


class ReplyLimiter:
    """Feed reply text piece by piece; get back the part to speak now."""

    def __init__(self) -> None:
        self.done = False
        self.rest = ""  # text after the cut
        self._passthrough = False
        self._chars = 0
        self._sentences = 0
        self._held = ""  # a possible sentence end waiting for its next char
        self._word = ""  # the letters and digits right before this char

    def feed(self, text: str) -> str:
        if self._passthrough:
            return text
        if self.done:
            self.rest += text
            return ""
        out = []
        for i, ch in enumerate(text):
            if self._held:
                if ch.isspace():
                    self._held += ch
                    continue
                # "3.5", "www.x" and "Dec. 5" are not sentence ends.
                ended = (
                    self._held[-1].isspace() and not ch.islower() and not ch.isdigit()
                )
                out.append(self._flush(ended))
                if self.done:
                    self.rest += text[i:]
                    break
            if ch in _END and not (ch == "." and self._abbreviation()):
                self._held += ch
                self._word = ""
                continue
            self._word = self._word + ch if ch.isalnum() else ""
            self._chars += 1
            out.append(ch)
            if ch in _END_NO_SPACE:
                self._count()
            elif self._chars >= HARD_CHARS and (
                ch.isspace() or self._chars >= HARD_CHARS + HARD_SLACK
            ):
                # A run-on with no sentence end is still cut.
                self.done = True
            if self.done:
                self.rest += text[i + 1 :]
                break
        return "".join(out)

    def _abbreviation(self) -> bool:
        word = self._word
        return bool(word) and (
            len(word) == 1 or word.isdigit() or word.lower() in _ABBREVIATIONS
        )

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

    def release(self) -> str:
        """Stop limiting: returns the text held back so far."""
        self._passthrough = True
        if self.done:
            rest, self.rest = self.rest, ""
            return f" {rest}" if rest else ""
        held, self._held = self._held, ""
        return held

    def finish(self) -> str:
        """The text ended: a held sentence end is spoken as it is."""
        held, self._held = self._held, ""
        return "" if self.done or self._passthrough else held.rstrip()


def split_reply(text: str) -> tuple[str, str]:
    """(the part to speak now, the rest)."""
    limiter = ReplyLimiter()
    part = limiter.feed(text) + limiter.finish()
    return part.strip(), limiter.rest.strip()


# Tool-call syntax a model can leak into its text. Speech stops at the first
# marker (agent._sanitize_tts_stream), so nothing after one is kept either.
_SYNTAX_MARKERS = ("```", "\n{", "\n[")


def _speakable_rest(rest: str) -> str:
    rest = rest.strip()
    if rest[:1] in ("{", "[", "`"):
        return ""
    cuts = [i for i in (rest.find(m) for m in _SYNTAX_MARKERS) if i != -1]
    return rest[: min(cuts)].strip() if cuts else rest


def _with_text(chunk: Any, text: str | None) -> Any:
    """A copy of ``chunk`` with other text: the SDK also reads the original."""
    copy = getattr(chunk, "model_copy", None)
    out = copy(deep=True) if copy is not None else chunk
    out.delta.content = text
    return out


async def limit_reply(
    stream: AsyncIterable[Any], on_rest: Callable[[str, str], None] | None = None
) -> AsyncIterator[Any]:
    """Pass a reply stream through, holding back text after the first part.

    ``on_rest(part, rest)`` gets the spoken part and the held-back text. A
    reply that calls a tool is spoken whole: its text may carry what the tool
    acts on (a goodbye before a hang-up, a number before a text is sent), and
    nothing continues after it.
    """
    limiter = ReplyLimiter()
    called_tool = False
    flushed = False
    spoken: list[str] = []
    async for chunk in stream:
        if isinstance(chunk, str):
            if text := limiter.feed(chunk):
                spoken.append(text)
                yield text
        elif not hasattr(chunk, "delta"):
            yield chunk  # a flush or another marker: not ours to change
            continue
        else:
            delta = chunk.delta
            content = getattr(delta, "content", None)
            if getattr(delta, "tool_calls", None) and not called_tool:
                called_tool = True
                yield _with_text(chunk, (limiter.release() + (content or "")) or None)
                continue
            if called_tool or not content:
                yield chunk
                continue
            text = limiter.feed(content) or None
            if text:
                spoken.append(text)
            if text or getattr(chunk, "usage", None):
                yield _with_text(chunk, text)
        if limiter.done and not flushed and not called_tool:
            # End this part's speech input now: the voice must not wait for
            # text that is held back.
            flushed = True
            yield FlushSentinel()
    if tail := limiter.finish():
        spoken.append(tail)
        yield tail
    if on_rest is not None and not called_tool:
        rest = _speakable_rest(limiter.rest)
        if rest and all(m not in "".join(spoken) for m in _SYNTAX_MARKERS):
            on_rest("".join(spoken), rest)


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


class ReplyContinuation:
    """Speaks the held-back rest of a reply, part by part, unless they speak."""

    def __init__(
        self,
        session: Any,
        *,
        can_continue: Callable[[], bool],
    ) -> None:
        self.session = session
        self.can_continue = can_continue
        self.rest = ""
        self._part = ""  # the part whose end starts the pause
        self._spoken = False  # a part of the kept reply has been spoken
        self._in_pause = False
        self._started = False  # they began to speak in the pause
        self._heard = False  # and words of that speech arrived
        self._task: asyncio.Task | None = None

        @session.on("conversation_item_added")
        def item_added(event) -> None:
            item = getattr(event, "item", None)
            role = getattr(item, "role", None)
            if role == "user":
                # Before any part is spoken, this is the turn the reply
                # answers: with early generation the SDK adds it after the
                # reply text exists. After a part, they took a new turn.
                if self._spoken:
                    self.drop()
                return
            if role != "assistant" or not self.rest or self._task is not None:
                return
            # Other speech can finish first (a wait message, a spoken line):
            # only the kept reply's own part starts the pause.
            text = _norm(getattr(item, "text_content", None))
            if not self._part or not text.startswith(self._part[:60]):
                return
            if getattr(item, "interrupted", False):
                self.drop()
                return
            self._spoken = True
            # The pause starts now, not when the task first runs.
            self._started = self._heard = False
            self._in_pause = True
            self._task = asyncio.create_task(self._continue(), name="reply_rest")

        @session.on("user_state_changed")
        def user_state(event) -> None:
            if self._in_pause and getattr(event, "new_state", None) == "speaking":
                self._started = True

        @session.on("user_input_transcribed")
        def heard(event) -> None:
            # Only speech that began in the pause counts: the text of an
            # "okay" said during the part can arrive in the pause.
            if self._started and (getattr(event, "transcript", "") or "").strip():
                self._heard = True

        @session.on("close")
        def closed(_) -> None:
            self.drop()

    def keep(self, part: str, rest: str) -> None:
        self.rest = rest.strip()
        self._part = _norm(part)
        self._spoken = False

    def drop(self) -> None:
        self.rest = ""
        self._part = ""
        self._in_pause = False
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _user_speaking(self) -> bool:
        return getattr(self.session, "user_state", None) == "speaking"

    async def _continue(self) -> None:
        try:
            await asyncio.sleep(PAUSE)
            if self._started or self._user_speaking():
                self._started = True
                # A sound is not yet a turn: give its words time to arrive.
                while self._user_speaking():
                    await asyncio.sleep(0.1)
                await asyncio.sleep(SETTLE)
            self._in_pause = False
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
                self._part = _norm(part)
                self.session.say(part)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("could not continue the reply; waiting for them")
            self.rest = ""
        finally:
            self._in_pause = False
            self._task = None
