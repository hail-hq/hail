"""BYO resolution: precedence, fail-fast, fallback wrapping, key decryption."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from hailhq.core.config import settings
from hailhq.core.models import OrgProviderConfig
from hailhq.core.secret_cipher import generate_key
from hailhq.voicebot import pipeline
from hailhq.voicebot.pipeline import (
    ProviderKeyError,
    ResolvedLayer,
    build_llm,
    build_stt,
    build_tts,
    decrypt_llm_metadata,
    resolve_org_configs,
)


@pytest.fixture(autouse=True)
def house_keys(monkeypatch):
    monkeypatch.setattr(settings, "cartesia_api_key", "house-cartesia")
    monkeypatch.setattr(settings, "cartesia_model", "sonic-3.5")
    monkeypatch.setattr(settings, "cartesia_voice_id", "house-voice")
    monkeypatch.setattr(settings, "eleven_api_key", "")
    monkeypatch.setattr(settings, "deepgram_model", "nova-3")
    monkeypatch.setattr(settings, "openai_model", "gpt-5.4-mini")
    monkeypatch.setattr(settings, "google_model", "gemini-3-flash")
    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-4-6")


@dataclass
class Captured:
    kwargs: dict


@pytest.fixture()
def captured_plugins(monkeypatch):
    """Stub every plugin constructor to capture kwargs instead of dialing out.

    The stub also carries the minimal attribute surface the real
    ``livekit.agents.tts.FallbackAdapter``/``stt.FallbackAdapter`` validate at
    construction time (``num_channels``, ``sample_rate``, ``capabilities``,
    ``label``, ``.on()``) — ``test_fallback_enabled_wraps_house_after_byo``
    exercises the real (unmocked) ``FallbackAdapter``, which reads these off
    each wrapped instance before ``build_tts``/``build_stt`` ever calls
    ``.synthesize()``/``.recognize()``.
    """
    calls: dict[str, list[Captured]] = {}

    def make(name):
        class Stub:
            num_channels = 1
            sample_rate = 24000

            class capabilities:
                streaming = False
                aligned_transcript = False
                interim_results = False
                diarization = False

            label = name

            def __init__(self, **kwargs):
                calls.setdefault(name, []).append(Captured(kwargs))

            def on(self, *args, **kwargs):
                pass

        return Stub

    monkeypatch.setattr(pipeline.cartesia_plugin, "TTS", make("cartesia"))
    monkeypatch.setattr(pipeline.elevenlabs_plugin, "TTS", make("elevenlabs"))
    monkeypatch.setattr(pipeline.deepgram_plugin, "STT", make("deepgram"))
    monkeypatch.setattr(pipeline.openai_plugin, "LLM", make("openai"))
    monkeypatch.setattr(pipeline.google_plugin, "LLM", make("google"))
    monkeypatch.setattr(pipeline.anthropic_plugin, "LLM", make("anthropic"))
    return calls


def test_tts_org_key_and_voice_passed_explicitly(captured_plugins) -> None:
    org = ResolvedLayer(
        provider="cartesia",
        api_key="org-key",
        params={"voice_id": "org-voice", "model": "sonic-3.5"},
        fallback_enabled=False,
    )
    build_tts(org, voice_id_override=None)
    kw = captured_plugins["cartesia"][0].kwargs
    assert kw["api_key"] == "org-key"
    assert kw["voice"] == "org-voice"


def test_per_call_voice_id_beats_org_params(captured_plugins) -> None:
    org = ResolvedLayer(
        provider="cartesia",
        api_key="org-key",
        params={"voice_id": "org-voice"},
        fallback_enabled=False,
    )
    build_tts(org, voice_id_override="per-call-voice")
    assert captured_plugins["cartesia"][0].kwargs["voice"] == "per-call-voice"


def test_house_default_when_no_org_config(captured_plugins) -> None:
    build_tts(None, voice_id_override=None)
    kw = captured_plugins["cartesia"][0].kwargs
    assert "api_key" not in kw  # house path keeps env-driven key sourcing
    assert kw["voice"] == "house-voice"


def test_llm_org_anthropic(captured_plugins) -> None:
    org = ResolvedLayer(
        provider="anthropic",
        api_key="org-anthropic",
        params={"model": "claude-sonnet-4-6"},
        fallback_enabled=False,
    )
    build_llm(None, org)
    kw = captured_plugins["anthropic"][0].kwargs
    assert kw["api_key"] == "org-anthropic"
    assert kw["model"] == "claude-sonnet-4-6"


def test_per_call_llm_beats_org_config(captured_plugins) -> None:
    org = ResolvedLayer(
        provider="google",
        api_key="g",
        params={"model": "gemini-3-flash"},
        fallback_enabled=False,
    )
    build_llm(
        {"base_url": "https://api.openai.com/v1", "api_key": "per-call", "model": "m"},
        org,
    )
    assert captured_plugins["openai"][0].kwargs["api_key"] == "per-call"
    assert "google" not in captured_plugins


def test_byo_layer_without_key_or_house_key_fails_fast(
    captured_plugins, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "cartesia_api_key", "")
    org = ResolvedLayer(
        provider="elevenlabs", api_key=None, params={}, fallback_enabled=False
    )
    # params-only row on a provider Hail has no house key for -> ProviderKeyError
    with pytest.raises(ProviderKeyError):
        build_tts(org, voice_id_override=None)


def test_build_stt_unknown_org_provider_fails_fast(captured_plugins) -> None:
    """An org stt row on a provider other than deepgram must fail fast, not
    silently bill Hail's house Deepgram key for a BYO org (parity with
    _org_llm/_org_tts)."""
    org = ResolvedLayer(
        provider="whisper-cloud", api_key="org-key", params={}, fallback_enabled=False
    )
    with pytest.raises(ProviderKeyError):
        build_stt(org)
    assert "deepgram" not in captured_plugins


def test_stt_org_speechmatics_key_used(captured_plugins) -> None:
    """``_api_key`` verified against the installed
    livekit-plugins-speechmatics==1.6.6 (``speechmatics.STT.__init__`` stores
    the resolved key on ``self._api_key``); revisit if it changes."""
    from hailhq.voicebot.pipeline import ResolvedLayer, build_stt

    org = ResolvedLayer(
        provider="speechmatics",
        api_key="sm-org-key",
        params={},
        fallback_enabled=False,
    )
    stt = build_stt(org=org, language="sv", provider="speechmatics")
    assert stt._api_key == "sm-org-key"


def test_stt_org_speechmatics_operating_point_standard(captured_plugins) -> None:
    from hailhq.voicebot.pipeline import ResolvedLayer, build_stt
    from livekit.plugins import speechmatics as speechmatics_plugin

    org = ResolvedLayer(
        provider="speechmatics",
        api_key="sm-org-key",
        params={"operating_point": "standard"},
        fallback_enabled=False,
    )
    stt = build_stt(org=org, language="sv", provider="speechmatics")
    assert (
        stt._stt_options.operating_point == speechmatics_plugin.OperatingPoint.STANDARD
    )


def test_stt_org_speechmatics_operating_point_defaults_enhanced(
    captured_plugins,
) -> None:
    from hailhq.voicebot.pipeline import ResolvedLayer, build_stt
    from livekit.plugins import speechmatics as speechmatics_plugin

    org = ResolvedLayer(
        provider="speechmatics", api_key="sm-org-key", params={}, fallback_enabled=False
    )
    stt = build_stt(org=org, language="sv", provider="speechmatics")
    assert (
        stt._stt_options.operating_point == speechmatics_plugin.OperatingPoint.ENHANCED
    )


def test_stt_org_row_ignored_when_pinned_to_other_provider(
    captured_plugins,
) -> None:
    from hailhq.voicebot.pipeline import ResolvedLayer, build_stt
    from livekit.plugins import deepgram as deepgram_plugin

    org = ResolvedLayer(
        provider="speechmatics",
        api_key="sm-org-key",
        params={},
        fallback_enabled=False,
    )
    # Caller pinned deepgram; the speechmatics org row must not be used.
    stt = build_stt(org=org, language="sv", provider="deepgram")
    assert isinstance(stt, deepgram_plugin.STT)


def test_fallback_enabled_wraps_house_after_byo(captured_plugins) -> None:
    org = ResolvedLayer(
        provider="cartesia", api_key="org-key", params={}, fallback_enabled=True
    )
    build_tts(org, voice_id_override=None)
    # two cartesia constructions: BYO first, house second, wrapped in a FallbackAdapter
    assert len(captured_plugins["cartesia"]) == 2
    assert captured_plugins["cartesia"][0].kwargs["api_key"] == "org-key"
    assert "api_key" not in captured_plugins["cartesia"][1].kwargs


async def test_resolve_org_configs_rejects_unsafe_llm_base_url(async_session) -> None:
    """The call-time SSRF guard runs inside resolve_org_configs (off the event
    loop), not inside _org_llm — a stored base_url pointing at cloud metadata
    must raise ProviderKeyError rather than build an LLM against it."""
    org_id = uuid.uuid4()
    async_session.add(
        OrgProviderConfig(
            organization_id=org_id,
            layer="llm",
            provider="openai-compatible",
            params={"base_url": "https://169.254.169.254/v1", "model": "m"},
            is_active=True,
        )
    )
    await async_session.commit()

    with pytest.raises(ProviderKeyError):
        await resolve_org_configs(org_id)


async def test_resolve_org_configs_canonicalizes_safe_llm_base_url(
    async_session,
) -> None:
    org_id = uuid.uuid4()
    async_session.add(
        OrgProviderConfig(
            organization_id=org_id,
            layer="llm",
            provider="openai-compatible",
            params={"base_url": "https://api.openai.com/v1/", "model": "m"},
            is_active=True,
        )
    )
    await async_session.commit()

    resolved = await resolve_org_configs(org_id)
    assert resolved["llm"].params["base_url"] == "https://api.openai.com/v1"


def test_decrypt_llm_metadata_roundtrip(monkeypatch) -> None:
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())
    from hailhq.core.provider_config import provider_cipher

    enc = provider_cipher().encrypt("sk-secret")
    out = decrypt_llm_metadata(
        {"base_url": "https://x.example/v1", "api_key_enc": enc, "model": "m"}
    )
    assert out == {
        "base_url": "https://x.example/v1",
        "api_key": "sk-secret",
        "model": "m",
    }
    # legacy plaintext passthrough
    legacy = {"base_url": "https://x.example/v1", "api_key": "pk", "model": "m"}
    assert decrypt_llm_metadata(dict(legacy)) == legacy
    assert decrypt_llm_metadata(None) is None
