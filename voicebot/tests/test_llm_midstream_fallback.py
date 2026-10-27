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
from hailhq.voicebot.pipeline import _after_repeat, _fallback_chain, _ToolCallSent
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


_Item = str | FunctionToolCall | tuple[str, FunctionToolCall]


class _ScriptedLLM(LLM):
    """Sends ``chunks`` (text, a tool call, or both in one chunk), then raises
    ``fail`` if given."""

    def __init__(self, chunks: list[_Item], fail: Exception | None = None) -> None:
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
            elif isinstance(item, tuple):
                delta = ChoiceDelta(
                    role="assistant", content=item[0], tool_calls=[item[1]]
                )
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
        # The session's default options (max_retry=3), without the wait.
        conn_options=APIConnectOptions(retry_interval=0),
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
    events: list[tuple[LLM, bool]] = []
    adapter.on(
        "llm_availability_changed", lambda e: events.append((e.llm, e.available))
    )
    tools: list[str] = []
    try:
        with pytest.raises(APIStatusError):
            await _reply(adapter, tools=tools)
        assert tools == ["send_sms"]
        assert second.calls == 0
        # The failed model is marked down; the unused one is not.
        assert [s.available for s in adapter._status] == [False, True]
        assert events == [(first, False)]
        assert adapter._status[0].recovering_task is not None
        assert adapter._status[1].recovering_task is None
    finally:
        await adapter.aclose()


async def test_tool_call_after_a_partial_repeat_drops_the_repeat() -> None:
    """The held text only repeats the words said, so it is not said again."""
    first = _ScriptedLLM(["Let me check that"], fail=_overloaded())
    second = _ScriptedLLM(["Let me check", _SMS])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    tools: list[str] = []
    try:
        assert await _reply(adapter, tools=tools) == "Let me check that"
        assert tools == ["send_sms"]
    finally:
        await adapter.aclose()


async def test_tool_call_in_a_held_chunk_is_sent() -> None:
    """OpenAI-style streams send text and a tool call in one chunk."""
    first = _ScriptedLLM(["Aw"], fail=_overloaded())
    second = _ScriptedLLM([("A", _SMS)])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    tools: list[str] = []
    try:
        assert await _reply(adapter, tools=tools) == "Aw"
        assert tools == ["send_sms"]
    finally:
        await adapter.aclose()


async def test_tool_call_on_the_last_model_raises_its_error() -> None:
    first = _ScriptedLLM(["Aw"], fail=_overloaded())
    second = _ScriptedLLM(["Aw, one moment.", _SMS], fail=_overloaded())
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    tools: list[str] = []
    try:
        with pytest.raises(APIStatusError):
            await _reply(adapter, tools=tools)
        assert tools == ["send_sms"]
    finally:
        await adapter.aclose()


async def test_tool_call_only_then_failure_does_not_hand_over_or_rerun() -> None:
    """A tool call alone is output: no hand-over and no session rerun (a
    rerun would end with "after 4 attempts", not the model's error)."""
    first = _ScriptedLLM([_SMS], fail=_overloaded())
    second = _ScriptedLLM([_SMS])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    tools: list[str] = []
    try:
        with pytest.raises(APIStatusError):
            await _reply(adapter, tools=tools)
        assert tools == ["send_sms"]
        assert second.calls == 0
    finally:
        await adapter.aclose()


async def test_tool_call_error_keeps_its_cause() -> None:
    try:
        try:
            raise OSError("connection reset")
        except OSError as e:
            raise APIConnectionError() from e
    except APIConnectionError as e:
        error = e
    first = _ScriptedLLM([_SMS], fail=error)
    adapter = _fallback_chain([first, _ScriptedLLM(["x"])], retry_on_chunk_sent=True)
    try:
        with pytest.raises(APIConnectionError) as raised:
            await _reply(adapter)
        assert isinstance(raised.value.__cause__, OSError)
    finally:
        await adapter.aclose()


async def test_tool_call_error_keeps_its_context() -> None:
    """With no cause, the error is raised as LiveKit hands it over: context
    shown, not replaced by the chain's stop. (LiveKit's own
    ``LLMStream.__anext__``, llm.py:511, already sets the context.)"""
    error = _overloaded()
    first = _ScriptedLLM([_SMS], fail=error)
    adapter = _fallback_chain([first, _ScriptedLLM(["x"])], retry_on_chunk_sent=True)
    try:
        with pytest.raises(APIStatusError) as raised:
            await _reply(adapter)
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__ is False
        assert not isinstance(raised.value.__context__, _ToolCallSent)
    finally:
        await adapter.aclose()


async def test_tool_call_failure_logs_no_retry(caplog) -> None:
    first = _ScriptedLLM([_SMS], fail=_overloaded())
    adapter = _fallback_chain([first, _ScriptedLLM(["x"])], retry_on_chunk_sent=True)
    try:
        with pytest.raises(APIStatusError):
            await _reply(adapter)
        messages = [r.getMessage() for r in caplog.records]
        assert not any("retrying" in m for m in messages)
        assert any("no hand-over" in m for m in messages)
        # The next model was not picked for an attempt.
        assert adapter._status[1].selection_id == 0
    finally:
        await adapter.aclose()


async def test_no_space_before_punctuation_after_the_repeat() -> None:
    first = _ScriptedLLM(["Aw "], fail=_overloaded())
    second = _ScriptedLLM(["Aw", ", great."])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    try:
        assert await _reply(adapter) == "Aw, great."
    finally:
        await adapter.aclose()


async def test_space_after_the_repeat_in_a_later_chunk_is_dropped() -> None:
    first = _ScriptedLLM(["Oh no, "], fail=_overloaded())
    second = _ScriptedLLM(["Oh no,", " I am sorry."])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    try:
        assert await _reply(adapter) == "Oh no, I am sorry."
    finally:
        await adapter.aclose()


async def test_text_after_a_held_repeat_and_a_tool_call_is_spaced() -> None:
    first = _ScriptedLLM(["Let me check that"], fail=_overloaded())
    second = _ScriptedLLM(["Let me check", _SMS, "Sending now."])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    tools: list[str] = []
    try:
        assert await _reply(adapter, tools=tools) == "Let me check that Sending now."
        assert tools == ["send_sms"]
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


class _SilentOnceLLM(LLM):
    """Fails its first request before any output, then replies."""

    def __init__(self, reply: str) -> None:
        super().__init__()
        self.reply = reply
        self.calls = 0

    def chat(self, **kwargs: Any) -> LLMStream:
        self.calls += 1
        chunks: list[_Item] = [] if self.calls == 1 else [self.reply]
        fail = _overloaded() if self.calls == 1 else None
        return _ScriptedLLM(chunks, fail).chat(**kwargs)


async def test_every_model_failing_silent_reruns_the_chain() -> None:
    """Nothing was sent, so the session's retry (max_retry=3) runs the chain
    again and the person still gets a reply."""
    first = _SilentOnceLLM("Hello.")
    second = _ScriptedLLM([], fail=_overloaded())
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    try:
        assert await _reply(adapter) == "Hello."
    finally:
        await adapter.aclose()


async def test_every_model_failing_mid_reply_runs_the_chain_once() -> None:
    """Text was sent, so the session does not rerun the chain, even with
    max_retry=3: a rerun would say the words again."""
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


async def test_text_after_a_matched_repeat_and_a_tool_call_is_matched() -> None:
    first = _ScriptedLLM(["Sure"], fail=_overloaded())
    second = _ScriptedLLM(["Sure", _SMS, "Sure, sent."])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    tools: list[str] = []
    try:
        assert await _reply(adapter, tools=tools) == "Sure, sent."
        assert tools == ["send_sms"]
    finally:
        await adapter.aclose()


async def test_held_space_before_a_tool_call_is_said_once() -> None:
    first = _ScriptedLLM(["Sure"], fail=_overloaded())
    second = _ScriptedLLM(["Sure, one sec. ", _SMS, "Sending now."])
    adapter = _fallback_chain([first, second], retry_on_chunk_sent=True)
    try:
        assert await _reply(adapter) == "Sure, one sec. Sending now."
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    ("chunks", "reply"),
    [
        (["Hi.", " How"], "Hi. How"),
        (["Hi. ", "How"], "Hi. How"),
        (["Hello", "\n\n", "World"], "Hello\n\nWorld"),
        # Trailing spaces at the end of the reply are not said.
        (["Bye. "], "Bye."),
    ],
)
async def test_one_model_keeps_its_spaces(chunks: list[_Item], reply: str) -> None:
    adapter = _fallback_chain([_ScriptedLLM(chunks)], retry_on_chunk_sent=True)
    try:
        assert await _reply(adapter) == reply
    finally:
        await adapter.aclose()
