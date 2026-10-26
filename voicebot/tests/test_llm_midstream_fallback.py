"""A model that fails after its first words hands the reply to the next one.

Production case: Sonnet sent "Aw", then an in-stream ``overloaded_error``
(HTTP 200). Without ``retry_on_chunk_sent`` the LiveKit ``FallbackAdapter``
re-raises and the turn is lost (livekit-agents 1.8.8,
``livekit/agents/llm/fallback_adapter.py``, ``_run``).
"""

from __future__ import annotations

from typing import Any

import pytest
from hailhq.voicebot.pipeline import _fallback_chain
from livekit.agents import APIStatusError
from livekit.agents.llm import (
    LLM,
    ChatChunk,
    ChatContext,
    ChoiceDelta,
    LLMStream,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN


class _ScriptedLLM(LLM):
    """Sends ``chunks`` as text, then raises ``fail`` if given."""

    def __init__(self, chunks: list[str], fail: Exception | None = None) -> None:
        super().__init__()
        self.chunks = chunks
        self.fail = fail

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: Any = None,
        conn_options: Any = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: Any = NOT_GIVEN,
        tool_choice: Any = NOT_GIVEN,
        extra_kwargs: Any = NOT_GIVEN,
    ) -> LLMStream:
        return _ScriptedStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


class _ScriptedStream(LLMStream):
    async def _run(self) -> None:
        llm: _ScriptedLLM = self._llm  # type: ignore[assignment]
        for text in llm.chunks:
            self._event_ch.send_nowait(
                ChatChunk(
                    id=str(id(self)),
                    delta=ChoiceDelta(role="assistant", content=text),
                )
            )
        if llm.fail is not None:
            raise llm.fail


def _overloaded() -> APIStatusError:
    # Anthropic sends overloaded_error inside a 200 stream.
    return APIStatusError("overloaded_error", status_code=200)


async def _reply(adapter: LLM) -> str:
    parts: list[str] = []
    async with adapter.chat(chat_ctx=ChatContext.empty()) as stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.content:
                parts.append(chunk.delta.content)
    return "".join(parts)


async def test_next_model_replies_after_mid_reply_failure() -> None:
    first = _ScriptedLLM(["Aw"], fail=_overloaded())
    second = _ScriptedLLM(["Aw, ", "that is great."])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    try:
        # The next model restarts the reply: the failed model's words stay.
        assert await _reply(adapter) == "AwAw, that is great."
    finally:
        await adapter.aclose()


async def test_without_the_setting_the_reply_is_lost() -> None:
    """Control: proves the test above exercises ``retry_on_chunk_sent``."""
    first = _ScriptedLLM(["Aw"], fail=_overloaded())
    second = _ScriptedLLM(["Aw, that is great."])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=False)
    try:
        with pytest.raises(APIStatusError):
            await _reply(adapter)
    finally:
        await adapter.aclose()
