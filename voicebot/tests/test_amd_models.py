"""Multilingual AMD must not override a customer's provider selection."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from hailhq.core.config import settings
from hailhq.voicebot import amd, pipeline
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
    monkeypatch.setattr(settings, "deepgram_api_key", "house-deepgram")
    monkeypatch.setattr(settings, "deepgram_model", "nova-3")
    monkeypatch.setattr(settings, "openai_model", "house-chat-model")
    monkeypatch.setattr(settings, "google_api_key", "house-google")
    monkeypatch.setattr(settings, "google_amd_model", "gemini-3.1-flash-lite")
    # No Anthropic classifier unless a test sets one.
    monkeypatch.setattr(settings, "anthropic_model", "")
    monkeypatch.setattr(settings, "anthropic_backup_model", "")
    session = SimpleNamespace(llm=object(), stt=object(), vad=object())
    transcribe = Mock(return_value=object())
    google = Mock(return_value=FakeLLM())
    monkeypatch.setattr(amd.openai, "STT", transcribe)
    monkeypatch.setattr(amd.deepgram, "STT", Mock(return_value=object()))
    monkeypatch.setattr(amd.openai, "LLM", Mock(return_value=FakeLLM()))
    monkeypatch.setattr(pipeline.google_plugin, "LLM", google)
    return session, transcribe, google


def test_house_greeting_transcription_does_not_pin_the_call_language(layers):
    session, transcribe, google = layers
    result = amd.build_amd_models(session, language="en")
    assert result.stt is not session.stt
    primary = transcribe.call_args
    assert primary.kwargs["model"] == "gpt-live-transcribe"
    assert primary.kwargs["detect_language"] is True
    assert primary.kwargs["vad"] is session.vad
    recheck = amd.deepgram.STT.call_args
    assert recheck.kwargs["model"] == "nova-3"
    assert recheck.kwargs["detect_language"] is True
    assert "language" not in recheck.kwargs
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
    assert result.recheck_stt is None
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
    assert result.stt is session.stt and result.llm is None
    transcribe.assert_not_called()
    google.assert_not_called()


def test_optional_stt_setup_failure_keeps_the_working_session_stt(layers):
    session, transcribe, _ = layers
    transcribe.side_effect = ValueError("speech account not configured")
    result = amd.build_amd_models(session)
    assert result.stt is session.stt


def test_optional_recheck_setup_failure_keeps_the_working_live_stt(layers):
    session, transcribe, _ = layers
    primary = object()
    transcribe.return_value = primary
    amd.deepgram.STT.side_effect = ValueError("batch setup unavailable")
    result = amd.build_amd_models(session)
    assert result.stt is primary
    assert result.recheck_stt is None
    assert primary in result.owned


def test_optional_classifier_setup_failure_skips_that_classifier(layers):
    session, _, google = layers
    google.side_effect = ValueError("classifier account not configured")
    result = amd.build_amd_models(session)
    assert result.llm.primary is amd.openai.LLM.return_value


def test_no_classifier_never_falls_back_to_the_call_chain(layers, monkeypatch):
    """With no house classifier, AMD and the checks are skipped; the call's
    chain is never used."""
    session, _, _ = layers
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "google_api_key", "")
    assert amd.build_amd_models(session).llm is None
    assert amd.build_classifier(session) == (None, [])


async def test_no_classifier_skips_amd_and_releases_the_dial(layers):
    session, _, _ = layers
    released = []

    async def ready():
        released.append(True)

    models = amd.AMDModels(None, session.stt)
    assert (
        await amd.run_amd(session, UUID(int=1), models=models, on_ready=ready) is None
    )
    assert released == [True]


def test_customer_llm_stays_the_classifier(layers):
    session, _, _ = layers
    assert amd.build_classifier(session, llm_override=True) == (session.llm, [])


def test_haiku_classifies_first_then_every_usable_checker(layers, monkeypatch):
    """LiveKit's AMD forces its tool: Haiku 5.5 accepts that, Sonnet 5.5
    does not, so ANTHROPIC_BACKUP_MODEL goes first. Every usable checker is
    built: a fast failure leaves time for the third."""
    session, _, google = layers
    haiku = FakeLLM()
    built = []
    monkeypatch.setattr(settings, "anthropic_backup_model", "claude-haiku-5-5")
    monkeypatch.setattr(settings, "anthropic_bedrock_region", "eu-central-1")
    monkeypatch.setattr(
        pipeline, "house_anthropic", lambda model: built.append(model) or haiku
    )
    result = amd.build_amd_models(session)
    assert built == ["claude-haiku-5-5"]
    assert isinstance(result.llm, amd.ClassifierFailover)
    assert result.llm.primary is haiku
    assert result.llm.backups == (amd.openai.LLM.return_value, google.return_value)


def test_sonnet_never_checks_when_another_checker_exists(layers, monkeypatch):
    """No backup model: ANTHROPIC_MODEL (Sonnet 5.5) rejects a forced tool,
    so OpenAI and Gemini check without it."""
    session, _, google = layers
    built = []
    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-5-5")
    monkeypatch.setattr(settings, "anthropic_bedrock_region", "eu-central-1")
    monkeypatch.setattr(
        pipeline, "house_anthropic", lambda model: built.append(model) or FakeLLM()
    )
    llm, owned = amd.build_classifier(session)
    assert built == []
    assert llm.primary is amd.openai.LLM.return_value
    assert llm.backups == (google.return_value,)
    assert owned == [amd.openai.LLM.return_value, google.return_value]


def test_anthropic_only_uses_anthropic_model_as_checker(layers, monkeypatch):
    """No backup model, no OpenAI, no Gemini: ANTHROPIC_MODEL checks, so AMD
    still runs."""
    session, _, _ = layers
    checker = FakeLLM()
    built = []
    monkeypatch.setattr(settings, "anthropic_model", "claude-haiku-5-5")
    monkeypatch.setattr(settings, "anthropic_bedrock_region", "eu-central-1")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "google_api_key", "")
    monkeypatch.setattr(
        pipeline, "house_anthropic", lambda model: built.append(model) or checker
    )
    llm, owned = amd.build_classifier(session)
    assert built == ["claude-haiku-5-5"]
    assert isinstance(llm, amd.ClassifierFailover)
    assert llm.primary is checker
    assert llm.backups == ()
    assert owned == [checker]


def test_org_llm_with_fallback_checks_first_then_house_checkers(layers, monkeypatch):
    """The org's fallback chain holds Sonnet 5.5, which rejects a forced
    tool: the checks use the org's own LLM, then the house checkers."""
    session, _, google = layers
    haiku, org_client = FakeLLM(), FakeLLM()
    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-5-5")
    monkeypatch.setattr(settings, "anthropic_backup_model", "claude-haiku-5-5")
    monkeypatch.setattr(settings, "anthropic_bedrock_region", "eu-central-1")
    built = []
    monkeypatch.setattr(
        pipeline, "house_anthropic", lambda model: built.append(model) or haiku
    )
    monkeypatch.setattr(amd, "org_llm", lambda org: org_client)
    org = ResolvedLayer("anthropic", "org-llm", {"model": "org-model"}, True)
    llm, owned = amd.build_classifier(session, org_cfgs={"llm": org})
    assert llm is not session.llm
    assert built == ["claude-haiku-5-5"]
    assert llm.primary is org_client
    house = (haiku, amd.openai.LLM.return_value, google.return_value)
    assert llm.backups == house
    assert owned == [org_client, *house]
    assert session.llm not in owned


def test_org_llm_without_fallback_is_the_only_checker(layers, monkeypatch):
    session, _, google = layers
    org = ResolvedLayer("anthropic", "org-llm", {"model": "org-model"}, False)
    assert amd.build_classifier(session, org_cfgs={"llm": org}) == (session.llm, [])
    google.assert_not_called()


def test_haiku_skipped_without_anthropic_keys(layers, monkeypatch):
    session, _, google = layers
    monkeypatch.setattr(settings, "anthropic_backup_model", "claude-haiku-5-5")
    monkeypatch.setattr(settings, "anthropic_bedrock_region", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    result = amd.build_amd_models(session)
    assert result.llm.primary is amd.openai.LLM.return_value
    assert result.llm.backups == (google.return_value,)


def test_openai_setup_failure_keeps_gemini_classifier(layers):
    session, _, google = layers
    amd.openai.LLM.side_effect = ValueError("backup not configured")
    result = amd.build_amd_models(session)
    assert result.llm.primary is google.return_value


def test_missing_openai_key_keeps_gemini_classifier(layers, monkeypatch):
    session, _, google = layers
    monkeypatch.setattr(settings, "openai_api_key", "")
    result = amd.build_amd_models(session)
    assert result.llm.primary is google.return_value
    amd.openai.LLM.assert_not_called()


async def test_overloaded_amd_classifier_fails_over_with_prediction_tools(layers):
    """Classification must finish on the backup when the first classifier
    returns HTTP 503."""
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
    amd.openai.LLM.return_value = primary
    google.return_value = backup
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
    monkeypatch.setattr(settings, "deepgram_api_key", "house-deepgram")
    monkeypatch.setattr(settings, "deepgram_model", "nova-3")
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
        assert isinstance(models.recheck_stt, deepgram.STT)
        assert models.recheck_stt is not session.stt
        assert models.recheck_stt._opts.detect_language is True
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
        assert "may be too slow for AMD" in caplog.text
    finally:
        await models.aclose()


class _Checker(FakeLLM):
    """``slow`` hangs, ``down`` fails at once, otherwise saves a verdict."""

    def __init__(self, mode, calls):
        super().__init__()
        self.mode = mode
        self.calls = calls

    def chat(self, **kwargs):
        self.calls.append(self)
        return _CheckerStream(
            self,
            chat_ctx=kwargs["chat_ctx"],
            tools=kwargs["tools"],
            conn_options=kwargs["conn_options"],
        )


class _CheckerStream(LLMStream):
    async def _run(self):
        if self._llm.mode == "slow":
            await asyncio.Event().wait()
        if self._llm.mode == "down":
            raise APIStatusError("forbidden", status_code=403)
        self._event_ch.send_nowait(
            ChatChunk(
                id="verdict",
                delta=ChoiceDelta(
                    role="assistant",
                    tool_calls=[
                        FunctionToolCall(
                            call_id="verdict",
                            name="save_prediction",
                            arguments='{"category":"human"}',
                        )
                    ],
                ),
            )
        )


async def test_slow_checkers_stop_at_one_total_in_the_amd_path(monkeypatch):
    """LiveKit's AMD calls chat().collect() with no timeout: the checker's own
    total stops three slow checkers."""
    monkeypatch.setattr(amd, "CLASSIFIER_ATTEMPT_TIMEOUT_SECONDS", 0.06)
    monkeypatch.setattr(amd, "CLASSIFY_TIMEOUT_SECONDS", 0.1)
    calls: list = []
    checkers = [_Checker("slow", calls) for _ in range(3)]
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        async with amd.ClassifierFailover(*checkers).chat(
            chat_ctx=ChatContext()
        ) as stream:
            await stream.collect()
    elapsed = time.monotonic() - started
    # Not 3 x 0.06: the third checker is never reached.
    assert 0.09 <= elapsed < 0.15
    assert calls == checkers[:2]


async def test_fast_failures_reach_the_third_checker():
    calls: list = []
    checkers = [
        _Checker("down", calls),
        _Checker("down", calls),
        _Checker("ok", calls),
    ]
    async with amd.ClassifierFailover(*checkers).chat(chat_ctx=ChatContext()) as stream:
        response = await stream.collect()
    assert calls == checkers
    assert [call.name for call in response.tool_calls] == ["save_prediction"]
