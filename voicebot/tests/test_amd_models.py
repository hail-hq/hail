"""Multilingual AMD must not override a customer's provider selection."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hailhq.core.config import settings
from hailhq.voicebot import amd
from hailhq.voicebot.pipeline import ResolvedLayer
from livekit.agents import APIStatusError
from livekit.agents.llm import (
    ChatChunk,
    ChatContext,
    ChoiceDelta,
    FunctionToolCall,
    LLMStream,
    function_tool,
)

from ._fakes import FakeLLM


@pytest.fixture
def layers(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "house-openai")
    monkeypatch.setattr(settings, "openai_transcribe_model", "gpt-live-transcribe")
    monkeypatch.setattr(settings, "openai_model", "house-chat-model")
    monkeypatch.setattr(settings, "google_api_key", "house-google")
    monkeypatch.setattr(settings, "google_amd_model", "gemini-3.1-flash-lite")
    session = SimpleNamespace(llm=object(), stt=object(), vad=object())
    transcribe = Mock(return_value=object())
    google = Mock(return_value=FakeLLM())
    monkeypatch.setattr(amd.openai, "STT", transcribe)
    monkeypatch.setattr(amd.openai, "LLM", Mock(return_value=FakeLLM()))
    monkeypatch.setattr(amd.google, "LLM", google)
    return session, transcribe, google


def test_house_greeting_transcription_does_not_pin_the_call_language(layers):
    session, transcribe, google = layers
    result = amd.build_amd_models(session, language="en")
    assert result.stt is not session.stt
    assert transcribe.call_args.kwargs["model"] == "gpt-live-transcribe"
    assert transcribe.call_args.kwargs["detect_language"] is True
    assert transcribe.call_args.kwargs["vad"] is session.vad
    assert google.call_args.kwargs["model"] == "gemini-3.1-flash-lite"


def test_byo_layers_keep_the_orgs_providers(layers):
    session, transcribe, google = layers
    orgs = {
        "stt": ResolvedLayer("speechmatics", "org-stt", {}, False),
        "llm": ResolvedLayer("anthropic", "org-llm", {}, False),
    }
    result = amd.build_amd_models(session, org_cfgs=orgs, language="fr")
    assert result.llm is session.llm
    assert result.stt is session.stt
    transcribe.assert_not_called()
    google.assert_not_called()


def test_per_call_llm_remains_the_amd_brain(layers):
    session, _, google = layers
    result = amd.build_amd_models(session, llm_override=True)
    assert result.llm is session.llm
    google.assert_not_called()


@pytest.mark.parametrize(
    "language,expected", [("fr", "multi"), ("ar", "ar"), ("da", "da")]
)
def test_byo_deepgram_multilingual_coverage_is_respected(
    layers, monkeypatch, language, expected
):
    session, transcribe, _ = layers
    build = Mock(return_value=object())
    monkeypatch.setattr(amd, "build_stt", build)
    org = ResolvedLayer("deepgram", "org-key", {"model": "nova-3"}, False)
    amd.build_amd_models(session, org_cfgs={"stt": org}, language=language)
    transcribe.assert_not_called()
    build.assert_called_once_with(org, language=expected, provider="deepgram")


def test_missing_house_credentials_keep_the_existing_pipeline(layers, monkeypatch):
    session, transcribe, google = layers
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "google_api_key", "")
    result = amd.build_amd_models(session)
    assert result.stt is session.stt and result.llm is session.llm
    transcribe.assert_not_called()
    google.assert_not_called()


def test_optional_stt_setup_failure_keeps_the_working_session_stt(layers):
    session, transcribe, _ = layers
    transcribe.side_effect = ValueError("speech account not configured")
    result = amd.build_amd_models(session)
    assert result.stt is session.stt


def test_optional_classifier_setup_failure_keeps_the_working_session_llm(layers):
    session, _, google = layers
    google.side_effect = ValueError("classifier account not configured")
    result = amd.build_amd_models(session)
    assert result.llm is session.llm


def test_optional_backup_setup_failure_keeps_primary_classifier(layers):
    session, _, google = layers
    amd.openai.LLM.side_effect = ValueError("backup not configured")
    result = amd.build_amd_models(session)
    assert result.llm is google.return_value


def test_missing_backup_credentials_keep_primary_classifier(layers, monkeypatch):
    session, _, google = layers
    monkeypatch.setattr(settings, "openai_api_key", "")
    result = amd.build_amd_models(session)
    assert result.llm is google.return_value
    amd.openai.LLM.assert_not_called()


async def test_overloaded_amd_classifier_fails_over_with_prediction_tools(layers):
    """Classification must finish on the backup when Gemini returns HTTP 503."""
    session, _, google = layers
    attempts = []

    class Classifier(FakeLLM):
        def __init__(self, overloaded):
            super().__init__()
            self.overloaded = overloaded

        def chat(self, **kwargs):
            attempts.append((self, kwargs))
            return Stream(
                self,
                chat_ctx=kwargs["chat_ctx"],
                tools=kwargs["tools"],
                conn_options=kwargs["conn_options"],
            )

    class Stream(LLMStream):
        async def _run(self):
            if self._llm.overloaded:
                raise APIStatusError("high demand", status_code=503)
            self._event_ch.send_nowait(
                ChatChunk(
                    id="backup-prediction",
                    delta=ChoiceDelta(
                        role="assistant",
                        tool_calls=[
                            FunctionToolCall(
                                call_id="prediction",
                                name="save_prediction",
                                arguments='{"category":"human"}',
                            )
                        ],
                    ),
                )
            )

    primary, backup = Classifier(True), Classifier(False)
    google.return_value = primary
    amd.openai.LLM.return_value = backup
    models = amd.build_amd_models(session)
    assert isinstance(models.llm, amd.ClassifierFailover)

    @function_tool
    async def save_prediction(category: str) -> None:
        """Record the greeting category."""

    try:
        async with models.llm.chat(
            chat_ctx=ChatContext(), tools=[save_prediction], tool_choice="required"
        ) as stream:
            response = await stream.collect()
        assert [tool.name for tool in response.tool_calls] == ["save_prediction"]
        # One try each: no background retry of the overloaded primary.
        assert [model for model, _ in attempts] == [primary, backup]
        assert all(kwargs["conn_options"].max_retry == 0 for _, kwargs in attempts)
        assert all(kwargs["conn_options"].timeout == 10.0 for _, kwargs in attempts)
        assert all(kwargs["tool_choice"] == "required" for _, kwargs in attempts)
        assert all(kwargs["tools"] == [save_prediction] for _, kwargs in attempts)
    finally:
        await models.aclose()


async def test_amd_fallback_cleanup_closes_owned_clients_only(layers):
    session, transcribe, google = layers
    session.llm = SimpleNamespace(aclose=AsyncMock())
    transcribe.return_value = SimpleNamespace(aclose=AsyncMock())
    primary, backup = google.return_value, amd.openai.LLM.return_value
    primary.aclose = AsyncMock()
    backup.aclose = AsyncMock()
    models = amd.build_amd_models(session)
    await models.aclose()
    await models.aclose()
    primary.aclose.assert_awaited_once()
    backup.aclose.assert_awaited_once()
    transcribe.return_value.aclose.assert_awaited_once()
    session.llm.aclose.assert_not_called()


async def test_only_amd_owned_models_are_closed(layers):
    from unittest.mock import AsyncMock

    session, transcribe, google = layers
    owned = SimpleNamespace(aclose=AsyncMock())
    transcribe.return_value = owned
    result = amd.build_amd_models(session, llm_override=True)
    await result.aclose()
    owned.aclose.assert_awaited_once()
    google.assert_not_called()


async def test_real_provider_constructors_support_independent_autodetection(
    monkeypatch,
):
    from livekit.agents import AgentSession
    from livekit.plugins import deepgram, openai

    from ._fakes import FakeLLM

    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-placeholder")
    monkeypatch.setattr(settings, "openai_transcribe_model", "gpt-live-transcribe")
    monkeypatch.setattr(settings, "google_api_key", "google-test-placeholder")
    monkeypatch.setattr(settings, "google_amd_model", "gemini-3.1-flash-lite")
    session = AgentSession(
        vad=Mock(),
        llm=FakeLLM(),
        stt=deepgram.STT(api_key="deepgram-test-placeholder", language="en"),
    )
    models = amd.build_amd_models(session, language="en")
    try:
        assert isinstance(models.stt, openai.STT)
        assert models.stt.model == "gpt-live-transcribe"
        assert models.stt._opts.languages == []  # wire config must not force English
        assert models.stt._vad is session.vad
        assert models.stt is not session.stt
    finally:
        await models.aclose()
        await models.aclose()  # safe when detection and shutdown both clean up


async def test_slow_amd_backup_logs_a_clear_warning(layers, caplog):
    session, _, google = layers

    class Down(FakeLLM):
        def chat(self, **kwargs):
            return Stream(
                self,
                chat_ctx=kwargs["chat_ctx"],
                tools=kwargs["tools"],
                conn_options=kwargs["conn_options"],
            )

    class Stream(LLMStream):
        async def _run(self):
            raise APIStatusError("timed out", status_code=504)

    google.return_value = Down()
    amd.openai.LLM.return_value = Down()
    models = amd.build_amd_models(session)
    try:
        with pytest.raises(APIStatusError):
            async with models.llm.chat(chat_ctx=ChatContext()) as stream:
                await stream.collect()
        assert "OPENAI_MODEL may be too slow for AMD" in caplog.text
    finally:
        await models.aclose()
