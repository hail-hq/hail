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

import asyncio
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


class FakeSpeechHandle:
    """Stand-in for livekit.agents.voice.SpeechHandle.

    Verified shape against
    .venv/lib/python3.11/site-packages/livekit/agents/voice/speech_handle.py:
    the surface we depend on is just ``await handle.wait_for_playout()``.
    """

    def __init__(self) -> None:
        self.played_out = False

    async def wait_for_playout(self) -> None:
        self.played_out = True

    def __await__(self) -> Any:
        # Mirrors the real SpeechHandle being awaitable directly (the
        # entrypoint does ``await session.say(...)`` without capturing a
        # handle). Resolving immediately is enough for ordering assertions.
        async def _resolve() -> "FakeSpeechHandle":
            return self

        return _resolve().__await__()


class FakeAnnouncingSession:
    """Stand-in for AgentSession that records ``.say()`` calls.

    Verified shape against
    .venv/lib/python3.11/site-packages/livekit/agents/voice/agent_session.py:
    the surface we depend on is just ``session.say(text, *, allow_interruptions)``
    returning a handle with ``wait_for_playout()``.
    """

    def __init__(self) -> None:
        self.say_calls: list[tuple[str, bool]] = []
        self.last_handle: FakeSpeechHandle | None = None

    def say(self, text: str, *, allow_interruptions: bool) -> FakeSpeechHandle:
        self.say_calls.append((text, allow_interruptions))
        handle = FakeSpeechHandle()
        self.last_handle = handle
        return handle


class FakeLocalParticipant:
    """Records ``publish_dtmf`` calls made by the DTMF handle.

    Verified shape against livekit/rtc/participant.py:
    ``publish_dtmf(*, code: int, digit: str)``.
    """

    def __init__(self) -> None:
        self.dtmf: list[tuple[int, str]] = []

    async def publish_dtmf(self, *, code: int, digit: str) -> None:
        self.dtmf.append((code, digit))


class FakeRoom:
    """Minimal ``ctx.room``: the local participant plus a name."""

    def __init__(self, name: str = "hail-test") -> None:
        self.name = name
        self.local_participant = FakeLocalParticipant()


class FakeJobContext:
    """Stand-in for livekit.agents.JobContext.

    Verified shape against livekit/agents/job.py: we need
    ``ctx.shutdown(reason=…)``, ``ctx.delete_room()`` (returns an
    awaitable resolving to the DeleteRoomResponse; a completed Future here),
    and ``ctx.room.local_participant`` for the DTMF handle.
    """

    def __init__(self) -> None:
        self.shutdown_calls: list[str] = []
        self.delete_room_calls: int = 0
        self.delete_room_error: Exception | None = None
        self.room = FakeRoom()

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_calls.append(reason)

    def delete_room(self) -> "asyncio.Future[None]":
        self.delete_room_calls += 1
        fut: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        if self.delete_room_error is not None:
            fut.set_exception(self.delete_room_error)
        else:
            fut.set_result(None)
        return fut


__all__ = [
    "FakeAnnouncingSession",
    "FakeJobContext",
    "FakeLLM",
    "FakeLocalParticipant",
    "FakeRoom",
    "FakeSpeechHandle",
]
