"""Fake plugins for hermetic voicebot tests.

LiveKit Agents does not ship a public testing fixture module (the
``tests/fake_*.py`` files in the upstream repo are not packaged on PyPI).
This module reimplements the minimum needed to drive
``AgentSession.run(user_input=...)`` in text mode without hitting any real
provider:

* :class:`FakeLLM` — single canned assistant reply.

STT and TTS are intentionally not faked — ``AgentSession.run(user_input=...)``
runs in **text mode** per ``docs.livekit.io/agents/build/testing/`` (verified
2026-04-28), so neither component is exercised. If we ever assert audio
output, we'll need to vendor a fake TTS too.
"""

from __future__ import annotations

from typing import Any

from livekit.agents.llm import (
    LLM,
    ChatChunk,
    ChatContext,
    ChoiceDelta,
    LLMStream,
    Tool,
    ToolChoice,
)
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)


class FakeLLM(LLM):
    """An LLM that emits one canned assistant chunk regardless of input.

    Modeled on the upstream ``tests/fake_llm.py`` but stripped to the
    minimum: no ttft/duration tracking, no tool calls. Adequate for asserting
    "an assistant turn fired" and "the user turn was observed by our
    handlers".
    """

    def __init__(self, *, reply: str = "ack") -> None:
        super().__init__()
        self._reply = reply

    @property
    def reply(self) -> str:
        return self._reply

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> LLMStream:
        return _FakeLLMStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
        )


class _FakeLLMStream(LLMStream):
    def __init__(
        self,
        llm: FakeLLM,
        *,
        chat_ctx: ChatContext,
        tools: list[Tool],
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(llm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._fake = llm

    async def _run(self) -> None:
        # ``LLMStream`` exposes no public emit API; mirrors upstream's
        # tests/fake_llm.py (verified 2026-04-28) which writes to
        # ``self._event_ch`` directly.
        self._event_ch.send_nowait(
            ChatChunk(
                id=str(id(self)),
                delta=ChoiceDelta(
                    role="assistant",
                    content=self._fake.reply,
                    tool_calls=[],
                ),
            )
        )


__all__ = ["FakeLLM"]
