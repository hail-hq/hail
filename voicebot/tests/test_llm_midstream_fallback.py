"""A call's model that fails mid-reply hands the reply to the next one.

Production case: Sonnet sent "Aw", then an in-stream ``overloaded_error``
(HTTP 200). Without ``retry_on_chunk_sent`` the LiveKit ``FallbackAdapter``
re-raises and the turn is lost (livekit-agents 1.8.8,
``livekit/agents/llm/fallback_adapter.py``, ``_run``). The call chain
(``_CallLLMStream`` in ``pipeline.py``) hands over after text, drops the next
model's repeat of the words already sent, and never hands over after a tool
call.
"""

from __future__ import annotations

from typing import Any

import pytest
from hailhq.voicebot.pipeline import _after_repeat, _fallback_chain
from livekit.agents import APIConnectionError, APIConnectOptions, APIStatusError
from livekit.agents.llm import (
    LLM,
    ChatChunk,
    ChatContext,
    ChoiceDelta,
    FunctionToolCall,
    LLMStream,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN

_SMS = FunctionToolCall(name="send_sms", arguments="{}", call_id="c1")


class _ScriptedLLM(LLM):
    """Sends ``chunks`` (text, or a tool call), then raises ``fail`` if given."""

    def __init__(
        self, chunks: list[str | FunctionToolCall], fail: Exception | None = None
    ) -> None:
        super().__init__()
        self.chunks = chunks
        self.fail = fail
        self.calls = 0

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
        self.calls += 1
        return _ScriptedStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


class _ScriptedStream(LLMStream):
    async def _run(self) -> None:
        llm: _ScriptedLLM = self._llm  # type: ignore[assignment]
        for item in llm.chunks:
            if isinstance(item, str):
                delta = ChoiceDelta(role="assistant", content=item)
            else:
                delta = ChoiceDelta(role="assistant", tool_calls=[item])
            self._event_ch.send_nowait(ChatChunk(id=str(id(self)), delta=delta))
        if llm.fail is not None:
            raise llm.fail


def _overloaded() -> APIStatusError:
    # Anthropic sends overloaded_error inside a 200 stream.
    return APIStatusError("overloaded_error", status_code=200)


async def _reply(
    adapter: LLM, parts: list[str] | None = None, tools: list[str] | None = None
) -> str:
    parts = [] if parts is None else parts
    async with adapter.chat(
        chat_ctx=ChatContext.empty(),
        # build_session's options for a call chain: one run per turn.
        conn_options=APIConnectOptions(max_retry=0),
    ) as stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.content:
                parts.append(chunk.delta.content)
            if chunk.delta and tools is not None:
                tools.extend(c.name for c in chunk.delta.tool_calls)
    return "".join(parts)


async def test_next_model_replies_without_repeating_the_words_said() -> None:
    first = _ScriptedLLM(["Aw"], fail=_overloaded())
    second = _ScriptedLLM(["A", "w, ", "that is great."])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    try:
        assert await _reply(adapter) == "Aw, that is great."
    finally:
        await adapter.aclose()


async def test_repeat_match_ignores_case_and_spaces() -> None:
    first = _ScriptedLLM(["Oh no, "], fail=_overloaded())
    second = _ScriptedLLM(["oh  NO, I am sorry."])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    try:
        assert await _reply(adapter) == "Oh no, I am sorry."
    finally:
        await adapter.aclose()


async def test_next_model_text_that_does_not_repeat_is_kept() -> None:
    first = _ScriptedLLM(["Aw"], fail=_overloaded())
    second = _ScriptedLLM(["Oh, that is great."])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    try:
        # A space keeps the two words apart.
        assert await _reply(adapter) == "Aw Oh, that is great."
    finally:
        await adapter.aclose()


async def test_tool_call_then_failure_does_not_hand_over() -> None:
    """The tool already runs; the next model must not send it again."""
    first = _ScriptedLLM(["One moment.", _SMS], fail=_overloaded())
    second = _ScriptedLLM(["One moment.", _SMS])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    tools: list[str] = []
    try:
        with pytest.raises(APIStatusError):
            await _reply(adapter, tools=tools)
        assert tools == ["send_sms"]
        assert second.calls == 0
        # The failed model is marked down; the unused one is not.
        assert [s.available for s in adapter._status] == [False, True]
    finally:
        await adapter.aclose()


async def test_without_the_setting_the_reply_is_lost() -> None:
    """Control: texts keep LiveKit's default and raise."""
    first = _ScriptedLLM(["Aw"], fail=_overloaded())
    second = _ScriptedLLM(["Aw, that is great."])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=False)
    try:
        with pytest.raises(APIStatusError):
            await _reply(adapter)
        assert second.calls == 0
    finally:
        await adapter.aclose()


async def test_every_model_failing_mid_reply_runs_the_chain_once() -> None:
    """With the call session's options (``build_session``), a turn where
    every model fails after text runs the chain once."""
    first = _ScriptedLLM(["Aw"], fail=_overloaded())
    second = _ScriptedLLM(["Su"], fail=_overloaded())
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    parts: list[str] = []
    try:
        with pytest.raises(APIConnectionError):
            await _reply(adapter, parts)
        # One run: each model's words once (a rerun would repeat them).
        assert "".join(parts) == "Aw Su"
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    ("said", "new", "end"),
    [
        ("Aw", "Aw, hi", 2),
        ("Aw", "A", None),
        ("Aw", "Oh", -1),
        ("Hi there", "hi  there!", 9),
    ],
)
def test_after_repeat(said: str, new: str, end: int | None) -> None:
    assert _after_repeat(said, new) == end
