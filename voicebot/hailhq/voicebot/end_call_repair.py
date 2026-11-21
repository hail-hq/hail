"""Recover a bare closing command through LiveKit's normal tool pipeline.

Only an exact standalone end_call line is control syntax. Ordinary mentions
remain speech, and the recovery never grants a tool the call did not have.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Sequence
from contextlib import aclosing
from uuid import uuid4

from livekit.agents import FlushSentinel, ModelSettings, llm
from livekit.agents.types import NOT_GIVEN

logger = logging.getLogger("hailhq.voicebot")
_COMMANDS = ("end_call", "end_call()")


class EndCallTextFilter:
    """Hold only a possible command at the start of a line, never a turn."""

    def __init__(self) -> None:
        self._pending = ""
        self._passthrough = False

    def finish(self) -> str:
        pending, self._pending = self._pending, ""
        self._passthrough = False
        return "" if pending.strip() in _COMMANDS else pending

    def feed(self, text: str) -> str:
        out: list[str] = []
        lines = text.split("\n")
        for index, part in enumerate(lines):
            if self._passthrough:
                out.append(part)
            else:
                self._pending += part
                value = self._pending.lstrip(" \t\r")
                if not any(
                    command.startswith(value) or value.rstrip(" \t\r") == command
                    for command in _COMMANDS
                ):
                    out.append(self._pending)
                    self._pending = ""
                    self._passthrough = True
            if index < len(lines) - 1:
                out.extend((self.finish(), "\n"))
        return "".join(out)


def strip_end_call_text(text: str) -> str:
    filt = EndCallTextFilter()
    return filt.feed(text) + filt.finish()


async def strip_end_call_stream(source: AsyncIterable[str]) -> AsyncIterator[str]:
    filt = EndCallTextFilter()
    async for text in source:
        if clean := filt.feed(text):
            yield clean
    if tail := filt.finish():
        yield tail


def _ends_with_command(text: str) -> bool:
    text = text.strip()
    # JSON and code are already rejected by the speech sanitizer. A command
    # inside either is not a request to execute a function.
    if not text or text[0] in "{[`" or "```" in text:
        return False
    if re.search(r"\n\s*[\[{]", text):
        return False
    return text.splitlines()[-1].strip() in _COMMANDS


async def repair_end_call_stream(
    source: AsyncGenerator[llm.ChatChunk | str | FlushSentinel, None],
    *,
    tools: Sequence[llm.Tool],
    model_settings: ModelSettings | None,
) -> AsyncIterator[llm.ChatChunk | str | FlushSentinel]:
    """Strip the spoken name and recover one allowed, terminal tool call.

    Yield a structured function call only after successful generation, and
    only when the model issued no real tools. LiveKit then invokes the same
    registered handler that waits for goodbye playout and checks caller speech.
    Cancellation, stream errors, and disabled tools cannot trigger recovery.
    """
    choice = model_settings.tool_choice if model_settings else NOT_GIVEN
    allowed = choice in (NOT_GIVEN, "auto", "required") or (
        isinstance(choice, dict)
        and choice.get("function", {}).get("name") == "end_call"
    )
    allowed = (
        allowed and llm.ToolContext(tools).get_function_tool("end_call") is not None
    )
    filt = EndCallTextFilter()
    text: list[str] = []
    native_tool = False
    response_id = ""
    async with aclosing(source):
        async for part in source:
            if isinstance(part, str):
                text.append(part)
                if clean := filt.feed(part):
                    yield clean
            elif isinstance(part, llm.ChatChunk):
                response_id = part.id
                if part.delta:
                    native_tool = native_tool or bool(part.delta.tool_calls)
                    if part.delta.content:
                        text.append(part.delta.content)
                        part = part.model_copy(
                            update={
                                "delta": part.delta.model_copy(
                                    update={
                                        "content": filt.feed(part.delta.content) or None
                                    }
                                )
                            }
                        )
                yield part
            else:
                # FlushSentinel passes through unchanged.
                yield part
    if tail := filt.finish():
        yield tail
    if allowed and not native_tool and _ends_with_command("".join(text)):
        logger.warning("recovered end_call from model text")
        yield llm.ChatChunk(
            id=response_id or uuid4().hex,
            delta=llm.ChoiceDelta(
                role="assistant",
                tool_calls=[
                    llm.FunctionToolCall(
                        name="end_call", arguments="{}", call_id=f"hail_{uuid4().hex}"
                    )
                ],
            ),
        )
