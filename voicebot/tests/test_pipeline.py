"""Unit tests for ``hailhq.voicebot.pipeline``.

These tests exercise the LLM-construction logic in isolation, no
``AgentSession``, no DB, no real provider calls.

Note on API keys: the openai/google/anthropic plugin LLMs validate the
presence of an API key at construction time. We set placeholders via
``monkeypatch`` so the constructors don't error; no real network calls are
made because we never invoke ``.chat()``.
"""

from __future__ import annotations

import pytest
from hailhq.voicebot.pipeline import ResolvedLayer, build_llm, build_session, build_stt
from livekit.agents import AgentSession
from livekit.agents.llm import FallbackAdapter
from livekit.agents.stt import FallbackAdapter as stt_fallback
from livekit.plugins import deepgram as deepgram_plugin
from livekit.plugins import openai as openai_plugin


@pytest.fixture(autouse=True)
def _stub_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide placeholder API keys + settings so constructors don't bail."""
    from hailhq.core.config import settings

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-placeholder")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-placeholder")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-placeholder")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-placeholder")
    # The house chain skips a model whose key is empty in settings.
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test-placeholder")
    monkeypatch.setattr(settings, "anthropic_bedrock_region", "")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-placeholder")
    monkeypatch.setattr(settings, "openai_model", "gpt-4.1")
    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-5-5")
    # Plugin TTS constructors read their API key from the env at construction,
    # so these env vars must be set even though build_tts() gates on settings.
    monkeypatch.setenv("CARTESIA_API_KEY", "ct-test-placeholder")
    monkeypatch.setenv("ELEVEN_API_KEY", "el-test-placeholder")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "test-voice-id")
    # build_tts() gates on settings (loaded from env at import), so set the
    # singleton's fields directly, monkeypatch.setenv won't re-trigger load.
    monkeypatch.setattr(settings, "cartesia_api_key", "ct-test-placeholder")
    monkeypatch.setattr(settings, "cartesia_voice_id", "ct-voice-id")
    monkeypatch.setattr(settings, "cartesia_model", "sonic-3")
    monkeypatch.setattr(settings, "eleven_api_key", "el-test-placeholder")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "test-voice-id")
    monkeypatch.setattr(settings, "elevenlabs_model", "eleven_turbo_v2_5")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "sm-test-placeholder")
    monkeypatch.setattr(settings, "speechmatics_api_key", "sm-test-placeholder")
    monkeypatch.setattr(settings, "speechmatics_model", "linden-1")


@pytest.fixture(autouse=True)
def _fake_job_context():
    """``build_session``'s semantic turn-detection path constructs a
    ``MultilingualModel``, which reads ``get_job_context().inference_executor``
    at ``__init__`` time, it needs a job context even outside an active
    call. ``livekit.agents.testing.fake_job_context`` is the SDK's own
    in-process stand-in (installs a real ``JobContext`` with a no-op
    inference executor) for exactly this situation."""
    from livekit.agents.testing import fake_job_context

    with fake_job_context():
        yield


def test_build_llm_mode_a_returns_fallback_adapter(monkeypatch) -> None:
    """No ``llm`` config -> Hail's chain: Anthropic, its backup, OpenAI,
    then Gemini last.

    Inner-list attribute name (``_llm_instances``) verified against
    livekit-agents/livekit/agents/llm/fallback_adapter.py at HEAD on
    2026-04-28; revisit if it changes.
    """
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-5-5")
    monkeypatch.setattr(settings, "anthropic_backup_model", "claude-haiku-5-5")
    monkeypatch.setattr(settings, "openai_model", "gpt-4.1")
    monkeypatch.setattr(settings, "google_model", "gemini-3.5-flash-lite")
    monkeypatch.setattr(settings, "google_api_key", "test-google-placeholder")
    adapter = build_llm(None)
    assert isinstance(adapter, FallbackAdapter)
    inner = adapter._llm_instances
    assert [m.model for m in inner] == [
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "gpt-4.1",
        "gemini-3.5-flash-lite",
    ]


def test_house_chain_gives_each_model_one_try(monkeypatch) -> None:
    """One adapter-wide setting: no retry on a failed model, 10 s per try.
    Texts keep the default: no hand-over after a model sent text."""
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-5-5")
    adapter = build_llm(None)
    assert isinstance(adapter, FallbackAdapter)
    assert adapter._max_retry_per_llm == 0
    assert adapter._attempt_timeout == 10.0
    assert adapter._retry_on_chunk_sent is False


def test_call_chain_hands_over_after_text_was_sent(monkeypatch) -> None:
    """Calls build the chain with ``retry_on_chunk_sent``."""
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-5-5")
    adapter = build_llm(None, retry_on_chunk_sent=True)
    assert isinstance(adapter, FallbackAdapter)
    assert adapter._retry_on_chunk_sent is True


def test_build_session_hands_over_after_text_was_sent() -> None:
    from unittest.mock import MagicMock

    session = build_session(None, MagicMock())
    assert isinstance(session.llm, FallbackAdapter)
    assert session.llm._retry_on_chunk_sent is True
    # The session keeps LiveKit's retries; the call stream limits them to
    # turns where nothing was sent (test_llm_midstream_fallback.py).
    assert session.conn_options.llm_conn_options.max_retry == 3


def test_org_chain_with_fallback_gives_each_model_one_try(monkeypatch) -> None:
    """The org LLM with fallback uses the house chain's one-try settings."""
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-5-5")
    org = ResolvedLayer(
        provider="openai-compatible",
        api_key="org-key",
        params={"base_url": "https://llm.example.com/v1", "model": "m"},
        fallback_enabled=True,
    )
    adapter = build_llm(None, org, retry_on_chunk_sent=True)
    assert isinstance(adapter, FallbackAdapter)
    assert adapter._llm_instances[0].model == "m"
    assert adapter._retry_on_chunk_sent is True
    assert adapter._max_retry_per_llm == 0
    assert adapter._attempt_timeout == 10.0


@pytest.mark.parametrize(
    ("google_model", "google_api_key"),
    [("", "test-google-placeholder"), ("gemini-3.5-flash-lite", "")],
)
def test_build_llm_mode_a_skips_gemini_without_model_or_key(
    monkeypatch, google_model: str, google_api_key: str
) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-5-5")
    monkeypatch.setattr(settings, "anthropic_backup_model", "claude-haiku-5-5")
    monkeypatch.setattr(settings, "openai_model", "gpt-4.1")
    monkeypatch.setattr(settings, "google_model", google_model)
    monkeypatch.setattr(settings, "google_api_key", google_api_key)
    inner = build_llm(None)._llm_instances
    assert [m.model for m in inner] == [
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "gpt-4.1",
    ]


def test_build_llm_mode_a_without_anthropic_keys_uses_the_rest(monkeypatch) -> None:
    """A self-host with no Anthropic key still gets a chain."""
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "anthropic_backup_model", "claude-haiku-5-5")
    monkeypatch.setattr(settings, "google_model", "gemini-3.5-flash-lite")
    monkeypatch.setattr(settings, "google_api_key", "test-google-placeholder")
    inner = build_llm(None)._llm_instances
    assert [m.model for m in inner] == ["gpt-4.1", "gemini-3.5-flash-lite"]


def test_build_llm_mode_a_with_no_keys_raises(monkeypatch) -> None:
    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import ProviderKeyError

    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "google_api_key", "")
    with pytest.raises(ProviderKeyError):
        build_llm(None)


def test_thinking_off_per_model() -> None:
    from hailhq.voicebot.pipeline import _thinking_off

    assert _thinking_off("claude-sonnet-5-5") == {"type": "between_tools"}
    assert _thinking_off("eu.anthropic.claude-sonnet-5-5") == {"type": "between_tools"}
    assert _thinking_off("eu.anthropic.claude-haiku-5-5") == {"type": "disabled"}
    assert _thinking_off("global.anthropic.claude-haiku-5-5-v1:0") == {
        "type": "disabled"
    }
    assert _thinking_off("claude-opus-5-5") is None
    # Anthropic API ids can carry a date.
    assert _thinking_off("claude-sonnet-5-5-20261001") == {"type": "between_tools"}
    assert _thinking_off("eu.anthropic.claude-sonnet-5-5-20261001-v1:0") == {
        "type": "between_tools"
    }


def test_thinking_off_unknown_model_warns_and_disables(caplog) -> None:
    """An unknown id never stops the voicebot: thinking disabled, one warning."""
    from hailhq.voicebot import pipeline

    pipeline._warned_models.clear()
    with caplog.at_level("WARNING", logger="hailhq.voicebot"):
        # Not a near match: "sonnet-5-5" inside a longer id does not count.
        assert pipeline._thinking_off("eu.anthropic.claude-sonnet-5-5-mini") == {
            "type": "disabled"
        }
        assert pipeline._thinking_off("claude-sonnet-4-6") == {"type": "disabled"}
        assert pipeline._thinking_off("claude-sonnet-4-6") == {"type": "disabled"}
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 2
    assert "claude-sonnet-5-5-mini" in warnings[0]
    assert "claude-sonnet-4-6" in warnings[1]


def test_stt_takes_keyterms_only_on_nova_3(monkeypatch) -> None:
    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import stt_takes_keyterms

    monkeypatch.setattr(settings, "deepgram_api_key", "dg-test")
    nova3 = deepgram_plugin.STT(model="nova-3", api_key="dg-test")
    nova2 = deepgram_plugin.STT(model="nova-2", api_key="dg-test")
    assert stt_takes_keyterms(nova3)
    assert not stt_takes_keyterms(nova2)
    assert not stt_takes_keyterms(stt_fallback([nova2, nova3]))
    assert stt_takes_keyterms(stt_fallback([nova3, nova3]))
    assert not stt_takes_keyterms(None)
    assert not stt_takes_keyterms(build_stt(language="da", provider="speechmatics"))


def test_keyterms_reach_each_deepgram_inside_the_stt_fallback(monkeypatch) -> None:
    """``session.update_options(keyterms=...)`` hands the terms to the STT's
    ``_update_session_keyterms``; the fallback forwards them to each STT."""
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "deepgram_api_key", "dg-house")
    monkeypatch.setattr(settings, "deepgram_model", "nova-3")
    org = ResolvedLayer("deepgram", "dg-org", {"model": "nova-3"}, True)
    stt = build_stt(org, language="en")
    assert isinstance(stt, stt_fallback)
    stt._update_session_keyterms(["Ana Lopez"])
    instances = stt._stt_instances
    assert len(instances) == 2
    assert all(s._opts.keyterm == ["Ana Lopez"] for s in instances)


def test_build_llm_mode_b_returns_openai_with_overridden_endpoint() -> None:
    """A ``llm`` dict -> single OpenAI-compat client pointed at base_url."""
    cfg = {
        "base_url": "https://example.test/v1",
        "api_key": "sk-test-byo",
        "model": "custom/llama-3.1-70b",
    }
    inst = build_llm(cfg)
    assert isinstance(inst, openai_plugin.LLM)
    # Read back via the public ``model`` property, internal attr name varies.
    assert inst.model == "custom/llama-3.1-70b"


def test_build_session_smoke_constructs_full_pipeline() -> None:
    """Smoke-test ``build_session`` so the deepgram/cartesia/elevenlabs/silero
    constructor signatures are empirically exercised (not just the LLM half).

    Uses a stub VAD, Silero loading is heavy and unrelated to what this
    asserts. Mode A so the LLM path also runs.
    """

    class _StubVAD:
        pass

    session = build_session(llm_cfg=None, vad=_StubVAD())  # type: ignore[arg-type]
    assert isinstance(session, AgentSession)


def test_build_tts_both_keys_returns_fallback_adapter() -> None:
    """Cartesia + ElevenLabs configured -> FallbackAdapter, Cartesia primary.

    Inner-list attribute name (``_tts_instances``) verified against
    livekit/agents/tts/fallback_adapter.py (self._tts_instances) on
    2026-06-17; revisit if it changes.
    """
    from hailhq.voicebot.pipeline import build_tts
    from livekit.agents.tts import FallbackAdapter
    from livekit.plugins import cartesia as cartesia_plugin

    adapter = build_tts()
    assert isinstance(adapter, FallbackAdapter)
    inner = adapter._tts_instances
    assert len(inner) == 2, "both providers should be wrapped"
    assert isinstance(inner[0], cartesia_plugin.TTS), "Cartesia must be primary"


def test_build_tts_cartesia_only_returns_single_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only Cartesia configured -> the Cartesia TTS directly, no adapter."""
    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import build_tts
    from livekit.agents.tts import FallbackAdapter
    from livekit.plugins import cartesia as cartesia_plugin

    monkeypatch.setattr(settings, "eleven_api_key", "")

    inst = build_tts()
    assert isinstance(inst, cartesia_plugin.TTS)
    assert not isinstance(inst, FallbackAdapter)


def test_build_tts_elevenlabs_only_returns_single_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ElevenLabs configured -> the ElevenLabs TTS directly, no adapter."""
    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import build_tts
    from livekit.plugins import elevenlabs as elevenlabs_plugin

    monkeypatch.setattr(settings, "cartesia_api_key", "")

    inst = build_tts()
    assert isinstance(inst, elevenlabs_plugin.TTS)


def test_build_tts_no_keys_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """No TTS provider configured -> a clear ProviderKeyError, never a None TTS."""
    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import ProviderKeyError, build_tts

    monkeypatch.setattr(settings, "cartesia_api_key", "")
    monkeypatch.setattr(settings, "eleven_api_key", "")

    with pytest.raises(ProviderKeyError, match="No TTS provider configured"):
        build_tts()


def test_build_tts_language_applies_to_every_instance() -> None:
    """Per-call language reaches both house instances, so a Cartesia ->
    ElevenLabs failover keeps speaking the call's language.

    ``_opts.language`` attribute verified against the installed cartesia and
    elevenlabs plugins on 2026-07-23; revisit if it changes.
    """
    from hailhq.voicebot.pipeline import build_tts

    adapter = build_tts(language="fr")
    for inst in adapter._tts_instances:
        assert inst._opts.language == "fr"


def test_build_tts_no_language_keeps_plugin_default() -> None:
    """language=None -> the Cartesia plugin default ('en'), not None."""
    from hailhq.voicebot.pipeline import build_tts
    from livekit.plugins import cartesia as cartesia_plugin

    adapter = build_tts()
    cartesia_inst = adapter._tts_instances[0]
    assert isinstance(cartesia_inst, cartesia_plugin.TTS)
    assert cartesia_inst._opts.language == "en"


def test_build_stt_language_pins_deepgram() -> None:
    """Per-call language reaches the Deepgram instance; None keeps en-US."""
    from hailhq.voicebot.pipeline import build_stt

    assert build_stt(language="fr")._opts.language == "fr"
    assert build_stt()._opts.language == "en-US"


def test_build_stt_speechmatics_house() -> None:
    """``_stt_options`` attrs verified against the installed
    livekit-plugins-speechmatics==1.6.6 (``speechmatics.STT.__init__`` stores
    the resolved options dataclass on ``self._stt_options``); revisit if it
    changes."""
    from hailhq.voicebot.pipeline import build_stt
    from livekit.plugins import speechmatics as speechmatics_plugin

    stt = build_stt(language="da", provider="speechmatics")
    assert isinstance(stt, speechmatics_plugin.STT)
    assert stt._stt_options.language == "da"
    assert stt._stt_options.model == "linden-1"


def test_build_stt_deepgram_still_default_shape() -> None:
    from hailhq.voicebot.pipeline import build_stt
    from livekit.plugins import deepgram as deepgram_plugin

    stt = build_stt(language="en", provider="deepgram")
    assert isinstance(stt, deepgram_plugin.STT)
    assert stt._opts.language == "en"


def test_build_stt_speechmatics_without_any_key_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import ProviderKeyError, build_stt

    monkeypatch.setattr(settings, "speechmatics_api_key", "")
    with pytest.raises(ProviderKeyError):
        build_stt(language="da", provider="speechmatics")


def _make_session(language, org_cfgs=None):
    from unittest.mock import MagicMock

    from hailhq.voicebot.pipeline import build_session

    return build_session(None, MagicMock(), org_cfgs=org_cfgs, language=language)


@pytest.mark.parametrize("explicit,expected", [(None, "fr"), ("en", "en")])
def test_workspace_language_reaches_both_speech_layers(explicit, expected):
    from unittest.mock import MagicMock, patch

    from hailhq.voicebot.pipeline import ResolvedLayer

    org_tts = ResolvedLayer(
        "cartesia", None, {"language": "fr", "voice_id": "chosen"}, True
    )
    with (
        patch("hailhq.voicebot.pipeline.build_stt") as stt,
        patch("hailhq.voicebot.pipeline.build_tts") as tts,
        patch("hailhq.voicebot.pipeline.AgentSession"),
    ):
        build_session(None, MagicMock(), org_cfgs={"tts": org_tts}, language=explicit)
    assert stt.call_args.args[1] == expected
    assert tts.call_args.args[2] == expected


def test_session_semantic_turns_for_covered_language() -> None:
    from livekit.agents.inference import TurnDetector

    session = _make_session("fr")
    assert isinstance(session.turn_detection, TurnDetector)


def test_session_stt_turns_for_speechmatics_language() -> None:
    from livekit.plugins import speechmatics as speechmatics_plugin

    session = _make_session("da")
    assert session.turn_detection == "stt"
    assert isinstance(session.stt, speechmatics_plugin.STT)


def test_session_org_deepgram_row_overrides_auto_speechmatics_routing() -> None:
    """No per-call STT knob exists anymore: an org BYO deepgram row wins
    over the language auto-route (which would otherwise pick speechmatics
    for 'da'), matching resolve_stt_provider's org-row > auto precedence."""
    from hailhq.voicebot.pipeline import ResolvedLayer
    from livekit.plugins import deepgram as deepgram_plugin

    org_cfgs = {
        "stt": ResolvedLayer(
            provider="deepgram",
            api_key="org-deepgram-key",
            params={},
            fallback_enabled=False,
        )
    }
    session = _make_session("da", org_cfgs=org_cfgs)
    assert session.turn_detection == "vad"
    assert isinstance(session.stt, deepgram_plugin.STT)


def test_session_auto_falls_back_to_deepgram_without_speechmatics_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tenet 4: a deepgram-only self-host still serves 'da' (VAD turns)."""
    import logging

    from hailhq.core.config import settings
    from livekit.plugins import deepgram as deepgram_plugin

    monkeypatch.setattr(settings, "speechmatics_api_key", "")
    with caplog.at_level(logging.WARNING, logger="hailhq.voicebot"):
        session = _make_session("da")
    assert isinstance(session.stt, deepgram_plugin.STT)
    assert session.turn_detection == "vad"
    assert any(
        "da" in record.message and "speechmatics" in record.message
        for record in caplog.records
    )


def test_session_org_speechmatics_row_no_key_without_fallback_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An org speechmatics row with no stored key and no house key is a
    key-absence failure, not a capability mismatch, but it must still
    respect the consent flag: fallback_enabled=False means the org has NOT
    consented to a silent deepgram reroute, so this must raise
    ProviderKeyError (from build_stt's own fail-fast) rather than degrade."""
    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import ProviderKeyError, ResolvedLayer

    monkeypatch.setattr(settings, "speechmatics_api_key", "")
    org_cfgs = {
        "stt": ResolvedLayer(
            provider="speechmatics",
            api_key=None,
            params={},
            fallback_enabled=False,
        )
    }
    with pytest.raises(ProviderKeyError, match="speechmatics"):
        _make_session("da", org_cfgs=org_cfgs)


def test_session_org_speechmatics_row_no_key_with_fallback_degrades(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same no-key org row, but fallback_enabled=True: consent to reroute,
    so this degrades to deepgram + VAD turns with the existing warning."""
    import logging

    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import ResolvedLayer
    from livekit.plugins import deepgram as deepgram_plugin

    monkeypatch.setattr(settings, "speechmatics_api_key", "")
    org_cfgs = {
        "stt": ResolvedLayer(
            provider="speechmatics",
            api_key=None,
            params={},
            fallback_enabled=True,
        )
    }
    with caplog.at_level(logging.WARNING, logger="hailhq.voicebot"):
        session = _make_session("da", org_cfgs=org_cfgs)
    assert isinstance(session.stt, deepgram_plugin.STT)
    assert session.turn_detection == "vad"
    assert any(
        "da" in record.message and "speechmatics" in record.message
        for record in caplog.records
    )


def test_session_org_stt_incapable_with_fallback_degrades(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """'gu' (Gujarati) has no Speechmatics coverage (``_NO_SPEECHMATICS``).
    An org's console BYO STT row can still name speechmatics generally
    (it's not language-scoped); with ``fallback_enabled=True`` (consent to
    house-provider rerouting) ``build_session`` degrades to deepgram rather
    than construct an incompatible provider, and warns."""
    import logging

    from hailhq.voicebot.pipeline import ResolvedLayer
    from livekit.plugins import deepgram as deepgram_plugin

    org_cfgs = {
        "stt": ResolvedLayer(
            provider="speechmatics",
            api_key="org-sm-key",
            params={},
            fallback_enabled=True,
        )
    }
    with caplog.at_level(logging.WARNING, logger="hailhq.voicebot"):
        session = _make_session("gu", org_cfgs=org_cfgs)
    assert isinstance(session.stt, deepgram_plugin.STT)
    assert any(
        "gu" in record.message and "speechmatics" in record.message
        for record in caplog.records
    )


def test_session_org_stt_incapable_without_fallback_raises() -> None:
    """Same incapable org row as above, but ``fallback_enabled=False``: no
    consent to reroute to deepgram, so this is a hard ``ProviderKeyError``
    naming the provider and language, never a silent degrade."""
    from hailhq.voicebot.pipeline import ProviderKeyError, ResolvedLayer

    org_cfgs = {
        "stt": ResolvedLayer(
            provider="speechmatics",
            api_key="org-sm-key",
            params={},
            fallback_enabled=False,
        )
    }
    with pytest.raises(ProviderKeyError, match="speechmatics") as exc_info:
        _make_session("gu", org_cfgs=org_cfgs)
    assert "gu" in str(exc_info.value)


def test_session_unknown_language_degrades_to_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raw dispatch-metadata language outside SUPPORTED_LANGUAGES must not
    crash the call, degrade to provider defaults (English-ish) with a
    warning, per the Task 1 review's known watch item."""
    import logging

    from livekit.agents.inference import TurnDetector

    with caplog.at_level(logging.WARNING, logger="hailhq.voicebot"):
        session = _make_session("xx-not-a-real-code")
    assert isinstance(session.turn_detection, TurnDetector)
    assert any("xx-not-a-real-code" in record.message for record in caplog.records)


def test_build_session_raises_typeerror_for_unhashable_language() -> None:
    """A malformed ``voice_config.language`` (e.g. a dict from mis-shapen
    dispatch metadata, rather than a string code) makes the
    ``SUPPORTED_LANGUAGES`` membership check raise ``TypeError`` (unhashable
    type), not silently misbehave. ``entrypoint``'s guard in agent.py
    converts this to ``ProviderKeyError`` so it never escapes raw, see
    ``test_entrypoint_build_session_type_error_finalizes_cleanly`` in
    test_agent.py for the end-to-end conversion."""
    with pytest.raises(TypeError):
        _make_session({"unexpected": "shape"})


def test_house_tts_trims_elevenlabs_for_cartesia_only_language() -> None:
    from hailhq.voicebot.pipeline import build_tts
    from livekit.plugins import cartesia as cartesia_plugin

    tts = build_tts(language="th")  # th: no elevenlabs support
    assert isinstance(tts, cartesia_plugin.TTS)  # single instance, no adapter


def test_house_tts_keeps_fallback_for_dual_provider_language() -> None:
    from hailhq.voicebot.pipeline import build_tts
    from livekit.agents import tts as agents_tts

    tts = build_tts(language="da")
    assert isinstance(tts, agents_tts.FallbackAdapter)


def test_build_tts_byo_cartesia_with_fallback_and_cartesia_only_language() -> None:
    """BYO Cartesia + fallback_enabled + Cartesia-only language (th) should
    return FallbackAdapter(byo_cartesia, house_cartesia) with NO ElevenLabs."""
    from hailhq.voicebot.pipeline import ResolvedLayer, build_tts
    from livekit.agents import tts as agents_tts
    from livekit.plugins import cartesia as cartesia_plugin

    org = ResolvedLayer(
        provider="cartesia",
        api_key="org-cartesia-key",
        params={"voice_id": "org-voice", "model": "sonic-3.5"},
        fallback_enabled=True,
    )
    tts = build_tts(org=org, language="th")
    assert isinstance(tts, agents_tts.FallbackAdapter)
    inner = tts._tts_instances
    assert len(inner) == 2, "BYO + house fallback should have 2 instances"
    assert all(isinstance(inst, cartesia_plugin.TTS) for inst in inner)


def test_build_tts_only_elevenlabs_key_with_cartesia_only_language_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ELEVEN_API_KEY set, Cartesia-only language (th) → ProviderKeyError
    (not RuntimeError that escapes entrypoint)."""
    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import ProviderKeyError, build_tts

    monkeypatch.setattr(settings, "cartesia_api_key", "")
    monkeypatch.setattr(settings, "eleven_api_key", "el-test-placeholder")

    with pytest.raises(ProviderKeyError, match="cannot speak language"):
        build_tts(language="th")


def test_startup_capability_warnings_both_keys_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both TTS keys configured → no warnings."""
    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import startup_capability_warnings

    monkeypatch.setattr(settings, "cartesia_api_key", "ct-key")
    monkeypatch.setattr(settings, "eleven_api_key", "el-key")
    monkeypatch.setattr(settings, "speechmatics_api_key", "sm-key")

    warnings = startup_capability_warnings()
    assert warnings == []


def test_startup_capability_warnings_cartesia_empty_eleven_set_lists_cartesia_only_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cartesia empty + ElevenLabs set → message listing exactly the 7
    Cartesia-only languages (those in _NO_ELEVENLABS)."""
    import re

    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import startup_capability_warnings

    monkeypatch.setattr(settings, "cartesia_api_key", "")
    monkeypatch.setattr(settings, "eleven_api_key", "el-key")
    monkeypatch.setattr(settings, "speechmatics_api_key", "sm-key")

    warnings = startup_capability_warnings()
    assert len(warnings) == 1
    msg = warnings[0]

    # Extract language codes from the comma-separated list
    # Format: "...: bn, gu, he, ..." (codes after colon, comma-space separated)
    match = re.search(r":\s*(.+)$", msg)
    assert match, f"Could not extract language list from message: {msg}"
    langs_str = match.group(1)
    extracted_langs = {code.strip() for code in langs_str.split(", ")}

    # Should list exactly the 7 Cartesia-only languages
    expected_langs = {"bn", "gu", "he", "kn", "te", "th", "mr"}
    assert (
        extracted_langs == expected_langs
    ), f"Expected {expected_langs}, got {extracted_langs}"
    assert len(extracted_langs) == 7


def test_startup_capability_warnings_cartesia_empty_eleven_empty_lists_all_tts_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cartesia empty + ElevenLabs empty → message listing all TTS-requiring
    languages (all 39 supported languages)."""
    import re

    from hailhq.core.config import settings
    from hailhq.core.languages import SUPPORTED_LANGUAGES
    from hailhq.voicebot.pipeline import startup_capability_warnings

    monkeypatch.setattr(settings, "cartesia_api_key", "")
    monkeypatch.setattr(settings, "eleven_api_key", "")
    monkeypatch.setattr(settings, "speechmatics_api_key", "sm-key")

    warnings = startup_capability_warnings()
    assert len(warnings) == 1
    msg = warnings[0]

    # Extract language codes from the comma-separated list
    match = re.search(r":\s*(.+)$", msg)
    assert match, f"Could not extract language list from message: {msg}"
    langs_str = match.group(1)
    extracted_langs = {code.strip() for code in langs_str.split(", ")}

    # Should list all 39 supported languages
    expected_langs = set(SUPPORTED_LANGUAGES.keys())
    assert (
        extracted_langs == expected_langs
    ), f"Expected {expected_langs}, got {extracted_langs}"
    assert len(extracted_langs) == 39


def test_startup_capability_warnings_speechmatics_empty_lists_22_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Speechmatics empty → message about 22 speechmatics-routed languages
    falling back to Deepgram + VAD turn detection."""
    import re

    from hailhq.core.config import settings
    from hailhq.core.languages import SUPPORTED_LANGUAGES, default_stt_for
    from hailhq.voicebot.pipeline import startup_capability_warnings

    monkeypatch.setattr(settings, "cartesia_api_key", "ct-key")
    monkeypatch.setattr(settings, "eleven_api_key", "el-key")
    monkeypatch.setattr(settings, "speechmatics_api_key", "")

    warnings = startup_capability_warnings()
    assert len(warnings) == 1
    msg = warnings[0]

    # Should mention Deepgram and VAD
    assert "Deepgram" in msg
    assert "VAD" in msg or "silence" in msg.lower()

    # Extract language codes from the comma-separated list
    match = re.search(r":\s*(.+)$", msg)
    assert match, f"Could not extract language list from message: {msg}"
    langs_str = match.group(1)
    extracted_langs = {code.strip() for code in langs_str.split(", ")}

    # Compute expected speechmatics-routed languages
    expected_langs = {
        code for code in SUPPORTED_LANGUAGES if default_stt_for(code) == "speechmatics"
    }

    assert (
        extracted_langs == expected_langs
    ), f"Expected {expected_langs}, got {extracted_langs}"
    assert len(extracted_langs) == 22


def test_startup_capability_warnings_both_keys_empty_returns_both_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both TTS and Speechmatics keys empty → two warnings."""
    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import startup_capability_warnings

    monkeypatch.setattr(settings, "cartesia_api_key", "")
    monkeypatch.setattr(settings, "eleven_api_key", "")
    monkeypatch.setattr(settings, "speechmatics_api_key", "")

    warnings = startup_capability_warnings()
    assert len(warnings) == 2


def test_house_key_voice_row_without_model_follows_house_model_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A picker save stores no model, so CARTESIA_MODEL changes reach the org."""
    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import ResolvedLayer, build_tts
    from livekit.plugins import cartesia as cartesia_plugin

    org = ResolvedLayer(
        provider="cartesia",
        api_key=None,
        params={"voice_id": "picked-voice", "language": "fr"},
        fallback_enabled=False,
    )
    monkeypatch.setattr(settings, "cartesia_model", "sonic-3.6")
    tts = build_tts(org=org, language="fr")
    assert isinstance(tts, cartesia_plugin.TTS)
    assert tts.model == "sonic-3.6"


def test_elevenlabs_tagalog_sends_the_filipino_code() -> None:
    """ElevenLabs spells Tagalog "fil", on the org row and on the house chain."""
    from hailhq.voicebot.pipeline import ResolvedLayer, build_tts
    from livekit.agents import tts as agents_tts
    from livekit.plugins import elevenlabs as elevenlabs_plugin

    org = ResolvedLayer(
        provider="elevenlabs",
        api_key=None,
        params={"voice_id": "picked-voice"},
        fallback_enabled=True,
    )
    tts = build_tts(org=org, language="tl")
    assert isinstance(tts, agents_tts.FallbackAdapter)
    eleven = [
        inst for inst in tts._tts_instances if isinstance(inst, elevenlabs_plugin.TTS)
    ]
    assert len(eleven) == 2
    assert {inst._opts.language.language for inst in eleven} == {"fil"}
