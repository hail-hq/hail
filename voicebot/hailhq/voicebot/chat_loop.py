"""One reply from the model, with tool rounds. Shared by the text and email
workers: each brings its own LLM, tools, and tool runner."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from livekit.agents.llm import (
    ChatContext,
    FunctionCall,
    FunctionCallOutput,
    FunctionToolCall,
)

logger = logging.getLogger("hailhq.voicebot.chat_loop")

# Model calls per reply. The last one may not call a tool, so it writes text.
MAX_TOOL_ROUNDS = 3
# What the model reads when a tool call fails or names a tool it does not have.
TOOL_APOLOGY = "Sorry, the earlier messages could not be read."

ToolRunner = Callable[[FunctionToolCall], Awaitable[tuple[str, bool]]]


def chat_context(messages: list[dict[str, Any]]) -> ChatContext:
    ctx = ChatContext.empty()
    for m in messages:
        ctx.add_message(role=m["role"], content=m["content"])
    return ctx


async def run_chat(
    llm: Any,
    messages: list[dict[str, Any]],
    tools: list[Any],
    run_tool: ToolRunner,
    *,
    max_rounds: int,
    log_label: str,
) -> str:
    """The model's text over the chat. Each tool result goes back into the chat
    and the model is asked again, for at most ``max_rounds`` model calls. Closes
    ``llm``: plugin LLMs own an HTTP client each, and one is built per reply."""
    chat_ctx = chat_context(messages)
    text = ""
    try:
        for round_no in range(1, max_rounds + 1):
            last = round_no == max_rounds
            parts: list[str] = []
            calls: list[FunctionToolCall] = []
            # Providers reject tool_choice when there are no tools.
            options = (
                {"tools": tools, "tool_choice": "none" if last else "auto"}
                if tools
                else {}
            )
            async with llm.chat(chat_ctx=chat_ctx, **options) as stream:
                async for chunk in stream:
                    delta = getattr(chunk, "delta", None)
                    if delta is None:
                        continue
                    if delta.content:
                        parts.append(delta.content)
                    calls.extend(delta.tool_calls or [])
            # Text beside a tool call ("let me check") is not the reply.
            text = "".join(parts).strip()
            if last and calls and not text:
                # The model ignored tool_choice="none"; retry, do not skip.
                raise RuntimeError("model made a tool call on the last round")
            if not calls or last:
                break
            for call in calls:
                output, is_error = await run_tool(call)
                logger.info(
                    "%s agent tool=%s round=%d error=%s",
                    log_label,
                    call.name,
                    round_no,
                    is_error,
                )
                chat_ctx.insert(
                    FunctionCall(
                        call_id=call.call_id,
                        name=call.name,
                        arguments=call.arguments,
                        extra=call.extra or {},
                    )
                )
                chat_ctx.insert(
                    FunctionCallOutput(
                        call_id=call.call_id,
                        name=call.name,
                        output=output,
                        is_error=is_error,
                    )
                )
    finally:
        await llm.aclose()
    return text
