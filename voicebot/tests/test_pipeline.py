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
from livekit.agents import AgentSession
from livekit.agents.llm import FallbackAdapter
from livekit.plugins import openai as openai_plugin

from hailhq.voicebot.pipeline import build_llm, build_session


@pytest.fixture(autouse=True)
def _stub_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide placeholder API keys so plugin constructors don't bail."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-placeholder")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-placeholder")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-placeholder")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-placeholder")
    monkeypatch.setenv("ELEVEN_API_KEY", "el-test-placeholder")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "test-voice-id")


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
    """Smoke-test ``build_session`` so the deepgram/elevenlabs/silero
    constructor signatures are empirically exercised (not just the LLM half).

    Uses a stub VAD — Silero loading is heavy and unrelated to what this
    asserts. Mode A so the LLM path also runs.
    """

    class _StubVAD:
        pass

    session = build_session(llm_cfg=None, vad=_StubVAD())  # type: ignore[arg-type]
    assert isinstance(session, AgentSession)
