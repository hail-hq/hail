"""Unit tests for ``hailhq.voicebot.pipeline``.

These tests exercise the LLM-construction logic in isolation — no
``AgentSession``, no DB, no real provider calls.

Note on API keys: the openai/google/anthropic plugin LLMs validate the
presence of an API key at construction time. We set placeholders via
``monkeypatch`` so the constructors don't error; no real network calls are
made because we never invoke ``.chat()``.
"""

from __future__ import annotations

import pytest
from hailhq.voicebot.pipeline import build_llm, build_session
from livekit.agents import AgentSession
from livekit.agents.llm import FallbackAdapter
from livekit.plugins import openai as openai_plugin


@pytest.fixture(autouse=True)
def _stub_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide placeholder API keys + settings so constructors don't bail."""
    from hailhq.core.config import settings

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-placeholder")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-placeholder")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-placeholder")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-placeholder")
    # Plugin TTS constructors read their API key from the env at construction,
    # so these env vars must be set even though build_tts() gates on settings.
    monkeypatch.setenv("CARTESIA_API_KEY", "ct-test-placeholder")
    monkeypatch.setenv("ELEVEN_API_KEY", "el-test-placeholder")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "test-voice-id")
    # build_tts() gates on settings (loaded from env at import), so set the
    # singleton's fields directly — monkeypatch.setenv won't re-trigger load.
    monkeypatch.setattr(settings, "cartesia_api_key", "ct-test-placeholder")
    monkeypatch.setattr(settings, "cartesia_voice_id", "ct-voice-id")
    monkeypatch.setattr(settings, "cartesia_model", "sonic-3")
    monkeypatch.setattr(settings, "eleven_api_key", "el-test-placeholder")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "test-voice-id")
    monkeypatch.setattr(settings, "elevenlabs_model", "eleven_turbo_v2_5")


def test_build_llm_mode_a_returns_fallback_adapter() -> None:
    """No ``llm`` config -> Hail's three-provider fallback chain.

    Inner-list attribute name (``_llm_instances``) verified against
    livekit-agents/livekit/agents/llm/fallback_adapter.py at HEAD on
    2026-04-28; revisit if it changes.
    """
    adapter = build_llm(None)
    assert isinstance(adapter, FallbackAdapter)
    inner = adapter._llm_instances
    assert len(inner) == 3, "fallback chain should compose three LLMs"


def test_build_llm_mode_b_returns_openai_with_overridden_endpoint() -> None:
    """A ``llm`` dict -> single OpenAI-compat client pointed at base_url."""
    cfg = {
        "base_url": "https://example.test/v1",
        "api_key": "sk-test-byo",
        "model": "custom/llama-3.1-70b",
    }
    inst = build_llm(cfg)
    assert isinstance(inst, openai_plugin.LLM)
    # Read back via the public ``model`` property — internal attr name varies.
    assert inst.model == "custom/llama-3.1-70b"


def test_build_session_smoke_constructs_full_pipeline() -> None:
    """Smoke-test ``build_session`` so the deepgram/cartesia/elevenlabs/silero
    constructor signatures are empirically exercised (not just the LLM half).

    Uses a stub VAD — Silero loading is heavy and unrelated to what this
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
    """No TTS provider configured -> a clear RuntimeError, never a None TTS."""
    from hailhq.core.config import settings
    from hailhq.voicebot.pipeline import build_tts

    monkeypatch.setattr(settings, "cartesia_api_key", "")
    monkeypatch.setattr(settings, "eleven_api_key", "")

    with pytest.raises(RuntimeError, match="No TTS provider configured"):
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
