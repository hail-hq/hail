"""A function name written as speech must not strand a finished call."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from hailhq.core.agent_tools import end_call
from hailhq.core.agent_tools.spec import ToolContext
from hailhq.voicebot.agent import (
    SpeechSanitizingAgent,
    _sanitize_tts_stream,
    speech_text,
)
from hailhq.voicebot.tools import STILL_TALKING, wrap_tool
from livekit.agents import AgentSession, ModelSettings, llm

from ._fakes import FakeLLM

LEAKED_REPLY = "You bet. Take care.\n\n\nend_call"


async def chunks(*parts):
    for part in parts:
        yield part


@pytest.mark.parametrize("split", range(len(LEAKED_REPLY) + 1))
async def test_command_is_never_spoken_even_across_chunks(split):
    spoken = "".join(
        [
            part
            async for part in _sanitize_tts_stream(
                chunks(LEAKED_REPLY[:split], LEAKED_REPLY[split:])
            )
        ]
    )
    assert spoken.strip() == "You bet. Take care."
    assert speech_text(LEAKED_REPLY) == "You bet. Take care."


@pytest.mark.parametrize("caller_keeps_talking", [False, True])
async def test_sdk_executes_recovered_tool_with_existing_hangup_guards(
    caller_keeps_talking,
):
    hangup = AsyncMock()
    tctx = ToolContext(
        call_id=UUID(int=1),
        organization_id=UUID(int=2),
        api=None,
        hangup=hangup,
        send_dtmf=None,
        spoke_since=lambda _since: caller_keeps_talking,
    )
    tool = wrap_tool(end_call.SPEC, tctx)
    # One recovery is sufficient. A failed/interrupted hangup gets a normal
    # model reply, rather than another fake command on every tool-result turn.
    model = FakeLLM(reply=LEAKED_REPLY)
    original_chat = model.chat

    def chat(**kwargs):
        if any(
            item.type == "function_call_output" for item in kwargs["chat_ctx"].items
        ):
            model._reply = "Okay, I'm listening."
        return original_chat(**kwargs)

    model.chat = chat
    agent = SpeechSanitizingAgent(instructions="test", tools=[tool])
    async with AgentSession(llm=model) as session:
        await session.start(agent=agent)
        executed = []
        session.on("function_tools_executed", lambda event: executed.append(event))
        await session.run(user_input="That is all, goodbye.")
        assert len(executed) == 1
        assert [call.name for call in executed[0].function_calls] == ["end_call"]
        if caller_keeps_talking:
            hangup.assert_not_awaited()
            assert executed[0].function_call_outputs[0].output == STILL_TALKING
        else:
            hangup.assert_awaited_once()
        assert all(
            "end_call" not in (item.text_content or "")
            for item in session.history.items
            if item.type == "message" and item.role == "assistant"
        )


async def end_call_tool():
    """Placeholder overridden with the real tool name below."""


def allowed_tool():
    return llm.function_tool(end_call_tool, name="end_call")


async def repaired(*parts, tools=None, tool_choice="auto"):
    from hailhq.voicebot.end_call_repair import repair_end_call_stream

    return [
        part
        async for part in repair_end_call_stream(
            chunks(*parts),
            tools=[allowed_tool()] if tools is None else tools,
            model_settings=ModelSettings(tool_choice=tool_choice),
        )
    ]


def tool_calls(parts):
    return [
        call
        for part in parts
        if isinstance(part, llm.ChatChunk) and part.delta
        for call in part.delta.tool_calls
    ]


@pytest.mark.parametrize("split", range(len(LEAKED_REPLY) + 1))
async def test_repair_handles_chat_chunks_and_preserves_usage(split):
    usage = llm.ChatChunk(
        id="response",
        usage=llm.CompletionUsage(
            completion_tokens=8, prompt_tokens=10, total_tokens=18
        ),
    )
    parts = await repaired(
        llm.ChatChunk(
            id="response", delta=llm.ChoiceDelta(content=LEAKED_REPLY[:split])
        ),
        llm.ChatChunk(
            id="response", delta=llm.ChoiceDelta(content=LEAKED_REPLY[split:])
        ),
        usage,
    )
    calls = tool_calls(parts)
    assert len(calls) == 1
    assert calls[0].name == "end_call"
    assert calls[0].arguments == "{}"
    assert calls[0].call_id
    assert usage in parts
    text = "".join(
        part.delta.content or ""
        for part in parts
        if isinstance(part, llm.ChatChunk) and part.delta
    )
    assert text.strip() == "You bet. Take care."


@pytest.mark.parametrize(
    "text",
    [
        "Goodbye.",
        "Use the end_call function.",
        "end_call later",
        "weekend_call",
        "end_call\nWait, one more thing.",
        "```\nend_call\n```",
        '{"text": "bye"}\nend_call',
    ],
)
async def test_ordinary_speech_and_structured_text_never_trigger_hangup(text):
    assert tool_calls(await repaired(text)) == []


@pytest.mark.parametrize(
    "choice", ["none", {"type": "function", "function": {"name": "send_dtmf"}}]
)
async def test_disabled_or_different_forced_tool_is_respected(choice):
    assert tool_calls(await repaired(LEAKED_REPLY, tool_choice=choice)) == []


async def test_unavailable_tool_is_never_invented():
    parts = await repaired(LEAKED_REPLY, tools=[])
    assert tool_calls(parts) == []
    assert "end_call" not in "".join(part for part in parts if isinstance(part, str))


async def test_native_tool_call_is_not_duplicated_or_reordered():
    native = llm.FunctionToolCall(name="end_call", arguments="{}", call_id="original")
    parts = await repaired(
        LEAKED_REPLY,
        llm.ChatChunk(id="response", delta=llm.ChoiceDelta(tool_calls=[native])),
    )
    assert tool_calls(parts) == [native]


async def test_cancelled_generation_does_not_run_recovery():
    from hailhq.voicebot.end_call_repair import repair_end_call_stream

    async def interrupted():
        yield LEAKED_REPLY
        raise asyncio.CancelledError()

    seen = []
    with pytest.raises(asyncio.CancelledError):
        async for part in repair_end_call_stream(
            interrupted(), tools=[allowed_tool()], model_settings=ModelSettings()
        ):
            seen.append(part)
    assert tool_calls(seen) == []


async def test_normal_speech_starts_before_model_finishes():
    from hailhq.voicebot.end_call_repair import repair_end_call_stream

    async def unfinished():
        yield "Let me check your order."
        pytest.fail("The filter buffered ordinary speech until the next model chunk")

    stream = repair_end_call_stream(
        unfinished(),
        tools=[allowed_tool()],
        model_settings=SimpleNamespace(tool_choice="auto"),
    )
    try:
        assert await anext(stream) == "Let me check your order."
    finally:
        await stream.aclose()


@pytest.mark.parametrize(
    "text",
    [
        "end_call",
        "end_call()",
        "Goodbye Joe.\n\n\nend_call",
        "Au revoir.\r\n \tend_call() \r\n",
        "さようなら。\nend_call\n\n",
        "Bye.\n\u00a0end_call",
        "Bye.\rend_call",
        "Bye.\u2028end_call",
    ],
)
async def test_repair_accepts_only_complete_terminal_command_lines(text):
    parts = await repaired(*text)
    assert len(tool_calls(parts)) == 1
    clean = "".join(part for part in parts if isinstance(part, str))
    assert "end_call" not in clean
    spoken = "".join([part async for part in _sanitize_tts_stream(chunks(*text))])
    assert "end_call" not in spoken


@pytest.mark.parametrize(
    "choice",
    ["auto", "required", {"type": "function", "function": {"name": "end_call"}}],
)
async def test_allowed_tool_choices_support_recovery(choice):
    assert len(tool_calls(await repaired(LEAKED_REPLY, tool_choice=choice))) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Use the end_call function.",
        "end_call later",
        "end_calling",
        "End the call.",
        "end_call(42)",
        "end_call {}",
    ],
)
async def test_ordinary_mentions_remain_unchanged_at_every_split(text):
    for split in range(len(text) + 1):
        parts = await repaired(text[:split], text[split:])
        assert tool_calls(parts) == []
        assert "".join(parts) == text
        spoken = "".join(
            [
                part
                async for part in _sanitize_tts_stream(
                    chunks(text[:split], text[split:])
                )
            ]
        )
        assert spoken == text


async def test_other_native_tools_prevent_recovery_too():
    native = llm.FunctionToolCall(
        name="send_dtmf", arguments='{"digits":"1"}', call_id="original"
    )
    parts = await repaired(
        LEAKED_REPLY,
        llm.ChatChunk(id="response", delta=llm.ChoiceDelta(tool_calls=[native])),
    )
    assert tool_calls(parts) == [native]


async def test_failed_generation_does_not_hang_up():
    from hailhq.voicebot.end_call_repair import repair_end_call_stream

    async def failed():
        yield LEAKED_REPLY
        raise RuntimeError("provider stopped")

    seen = []
    with pytest.raises(RuntimeError, match="provider stopped"):
        async for part in repair_end_call_stream(
            failed(), tools=[allowed_tool()], model_settings=ModelSettings()
        ):
            seen.append(part)
    assert tool_calls(seen) == []


async def test_stopping_repair_closes_the_provider_stream():
    from hailhq.voicebot.end_call_repair import repair_end_call_stream

    closed = False

    async def provider():
        nonlocal closed
        try:
            yield "Let me check."
            await asyncio.Event().wait()
        finally:
            closed = True

    source = provider()
    stream = repair_end_call_stream(
        source, tools=[allowed_tool()], model_settings=ModelSettings()
    )
    try:
        assert await anext(stream) == "Let me check."
        await stream.aclose()
        assert closed, "Stopping a reply must also close its provider request"
    finally:
        await source.aclose()
