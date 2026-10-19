"""Provider deadlines must not override the local pickup-latency budget."""

import asyncio
from unittest.mock import Mock

import pytest
from google.genai import errors, types
from hailhq.voicebot import amd
from livekit.agents.llm import (
    ChatChunk,
    ChatContext,
    ChoiceDelta,
    LLMStream,
    function_tool,
)
from livekit.plugins import google

from ._fakes import FakeLLM


class Classifier(FakeLLM):
    def __init__(self, behavior="reply"):
        super().__init__()
        self.behavior = behavior
        self.attempts = []
        self.started = asyncio.Event()
        self.cancelled = False

    def chat(self, **kwargs):
        self.attempts.append(kwargs["conn_options"])
        return Stream(
            self,
            chat_ctx=kwargs["chat_ctx"],
            tools=kwargs["tools"],
            conn_options=kwargs["conn_options"],
        )


class Stream(LLMStream):
    async def _run(self):
        self._llm.started.set()
        if self._llm.behavior != "hang":
            self._event_ch.send_nowait(
                ChatChunk(
                    id="prediction", delta=ChoiceDelta(role="assistant", content="ok")
                )
            )
        if self._llm.behavior != "reply":
            try:
                await asyncio.Event().wait()
            finally:
                self._llm.cancelled = True


async def collect(classifier):
    async with classifier.chat(chat_ctx=ChatContext()) as stream:
        return await stream.collect()


async def test_google_request_uses_accepted_provider_deadline(monkeypatch):
    """Exercise the real plugin's conversion to Google's HTTP timeout."""
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
    primary = google.LLM(model="gemini-3.1-flash-lite", api_key="test-placeholder")
    backup = FakeLLM()
    backup.chat = Mock(wraps=backup.chat)
    deadlines = []

    async def generate(**kwargs):
        deadline = kwargs["config"].http_options.timeout
        deadlines.append(deadline)
        if deadline < 10_000:
            raise errors.ClientError(
                400,
                {"error": {"code": 400, "message": "Minimum allowed deadline is 10s."}},
            )

        async def chunks():
            yield types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        content=types.Content(
                            role="model",
                            parts=[
                                types.Part(
                                    function_call=types.FunctionCall(
                                        name="save_prediction",
                                        args={"category": "human"},
                                    )
                                )
                            ],
                        ),
                        finish_reason=types.FinishReason.STOP,
                    )
                ]
            )

        return chunks()

    monkeypatch.setattr(primary._client.aio.models, "generate_content_stream", generate)

    @function_tool
    async def save_prediction(category: str):
        """Record the greeting category."""

    classifier = amd.ClassifierFailover(primary, backup)
    try:
        async with classifier.chat(
            chat_ctx=ChatContext(), tools=[save_prediction], tool_choice="required"
        ) as stream:
            result = await stream.collect()
        assert deadlines == [10_000]
        assert [call.name for call in result.tool_calls] == ["save_prediction"]
        backup.chat.assert_not_called()
    finally:
        await primary.aclose()
        await backup.aclose()


async def test_local_deadline_cancels_slow_primary_and_uses_backup_once(monkeypatch):
    monkeypatch.setattr(amd, "CLASSIFIER_ATTEMPT_TIMEOUT_SECONDS", 0.05)
    primary, backup = Classifier("hang"), Classifier()
    await asyncio.wait_for(collect(amd.ClassifierFailover(primary, backup)), timeout=2)
    assert primary.cancelled
    assert len(primary.attempts) == len(backup.attempts) == 1
    assert all(options.max_retry == 0 for options in primary.attempts + backup.attempts)
    assert primary.attempts[0].timeout == 10


async def test_partial_response_does_not_switch_provider_on_timeout(monkeypatch):
    monkeypatch.setattr(amd, "CLASSIFIER_ATTEMPT_TIMEOUT_SECONDS", 0.05)
    primary, backup = Classifier("partial"), Classifier()
    async with asyncio.timeout(2):
        with pytest.raises(TimeoutError):
            await collect(amd.ClassifierFailover(primary, backup))
    assert primary.cancelled
    assert not backup.attempts


async def test_slow_backup_is_also_cancelled_without_retries(monkeypatch):
    monkeypatch.setattr(amd, "CLASSIFIER_ATTEMPT_TIMEOUT_SECONDS", 0.05)
    primary, backup = Classifier("hang"), Classifier("hang")
    async with asyncio.timeout(2):
        with pytest.raises(TimeoutError):
            await collect(amd.ClassifierFailover(primary, backup))
    assert primary.cancelled and backup.cancelled
    assert len(primary.attempts) == len(backup.attempts) == 1


async def test_amd_cancellation_closes_primary_without_starting_backup():
    primary, backup = Classifier("hang"), Classifier()
    task = asyncio.create_task(collect(amd.ClassifierFailover(primary, backup)))
    await asyncio.wait_for(primary.started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert primary.cancelled
    assert not backup.attempts
