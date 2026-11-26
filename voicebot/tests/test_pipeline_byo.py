"""BYO resolution: precedence, fail-fast, fallback wrapping, key decryption."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from importlib import import_module
from unittest.mock import ANY, Mock

import pytest
from hailhq.core.config import VoicePreference, settings
from hailhq.core.models import OrgProviderConfig
from hailhq.core.providers.tts import livekit_deadline
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
    monkeypatch.setattr(settings, "speechmatics_model", "linden-1")
    monkeypatch.setattr(settings, "openai_model", "gpt-4.1")
    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-5-5")
    monkeypatch.setattr(settings, "anthropic_backup_model", "claude-haiku-5-5")


@dataclass
class Captured:
    kwargs: dict


@pytest.fixture()
def captured_plugins(monkeypatch):
    """Stub every plugin constructor to capture kwargs instead of dialing out.

    The stub also carries the minimal attribute surface the real
    ``livekit.agents.tts.FallbackAdapter``/``stt.FallbackAdapter`` validate at
    construction time (``num_channels``, ``sample_rate``, ``capabilities``,
    ``label``, ``.on()``), ``test_fallback_enabled_wraps_house_after_byo``
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
    monkeypatch.setattr(livekit_deadline, "GeminiTTS", make("gemini"))
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
    org_llm/_org_tts)."""
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

    org = ResolvedLayer(
        provider="speechmatics",
        api_key="sm-org-key",
        params={"operating_point": "standard"},
        fallback_enabled=False,
    )
    stt = build_stt(org=org, language="sv", provider="speechmatics")
    assert stt._stt_options.model == "linden-1"


def test_stt_org_speechmatics_operating_point_defaults_enhanced(
    captured_plugins,
) -> None:
    from hailhq.voicebot.pipeline import ResolvedLayer, build_stt

    org = ResolvedLayer(
        provider="speechmatics", api_key="sm-org-key", params={}, fallback_enabled=False
    )
    stt = build_stt(org=org, language="sv", provider="speechmatics")
    assert stt._stt_options.model == "linden-1"


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


def test_build_tts_org_incapable_with_fallback_uses_house_only(
    captured_plugins,
) -> None:
    """Org elevenlabs row can't speak 'th' (Cartesia-only); fallback_enabled
    is consent to reroute, the BYO instance is skipped entirely and the
    house chain (Cartesia only, for 'th') is returned."""
    org = ResolvedLayer(
        provider="elevenlabs", api_key="org-key", params={}, fallback_enabled=True
    )
    tts = build_tts(org, language="th")
    assert "elevenlabs" not in captured_plugins
    assert len(captured_plugins["cartesia"]) == 1
    assert "api_key" not in captured_plugins["cartesia"][0].kwargs
    assert tts is not None


def test_build_tts_org_incapable_without_fallback_raises(captured_plugins) -> None:
    """Same incapable org row, but fallback_enabled=False: no consent to
    reroute, so this is a hard error naming the provider and language."""
    org = ResolvedLayer(
        provider="elevenlabs", api_key="org-key", params={}, fallback_enabled=False
    )
    with pytest.raises(ProviderKeyError, match="elevenlabs") as exc_info:
        build_tts(org, language="th")
    assert "th" in str(exc_info.value)
    assert "elevenlabs" not in captured_plugins


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
    loop), not inside org_llm, a stored base_url pointing at cloud metadata
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


def test_gemini_default_only_applies_to_inherited_english(
    monkeypatch, captured_plugins
):
    monkeypatch.setattr(settings, "hail_gemini_tts_enabled", True)
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    assert build_tts(language="en")._tts_instances[0].label == "gemini"
    assert captured_plugins["gemini"][0].kwargs == {
        "model": "gemini-3.8-flash-tts",
        "voice_name": "Charon",
        "vertexai": True,
        "project": "hail-speech",
        "location": settings.google_tts_location,
    }
    build_tts(voice_id_override="legacy-voice", language="en")
    assert captured_plugins["cartesia"][-1].kwargs["voice"] == "legacy-voice"
    build_tts(language="fr")
    assert captured_plugins["cartesia"][-1].kwargs["language"] == "fr"


def test_explicit_charon_works_when_gemini_is_the_only_house_provider(
    monkeypatch, captured_plugins
):
    monkeypatch.setattr(settings, "hail_gemini_tts_enabled", True)
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    monkeypatch.setattr(settings, "cartesia_api_key", "")
    monkeypatch.setattr(settings, "eleven_api_key", "")
    assert build_tts(voice_id_override="Charon", language="en").label == "gemini"


def test_workspace_gemini_can_fall_back_to_house_credentials(
    monkeypatch, captured_plugins
):
    monkeypatch.setattr(settings, "hail_gemini_tts_enabled", True)
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    monkeypatch.setattr(settings, "cartesia_api_key", "")
    monkeypatch.setattr(settings, "eleven_api_key", "")
    org = ResolvedLayer("gemini", "customer-key", {"project": "customer-project"}, True)
    customer, house = build_tts(org, language="en")._tts_instances
    assert (customer.speech.project, customer.speech.api_key) == (
        "customer-project",
        "customer-key",
    )
    assert house.label == "gemini"
    assert captured_plugins["gemini"][0].kwargs["project"] == "hail-speech"
    assert "api_key" not in captured_plugins["gemini"][0].kwargs


def test_workspace_gemini_tries_a_different_vendor_before_house_gemini(
    monkeypatch, captured_plugins
):
    monkeypatch.setattr(settings, "hail_gemini_tts_enabled", True)
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    org = ResolvedLayer("gemini", "customer-key", {"project": "customer-project"}, True)
    customer, cartesia, house = build_tts(org, language="en")._tts_instances
    assert customer.speech.project == "customer-project"
    assert cartesia.label == "cartesia"
    assert house.label == "gemini"
    assert captured_plugins["gemini"][0].kwargs["project"] == "hail-speech"


def test_disabling_inherited_gemini_default_keeps_an_explicit_selection(
    monkeypatch, captured_plugins
):
    monkeypatch.setattr(settings, "hail_gemini_tts_enabled", False)
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    org = ResolvedLayer("gemini", None, {"voice_id": "Charon"}, False)
    assert build_tts(org, language="en").label == "gemini"
    build_tts(language="en")
    assert len(captured_plugins["cartesia"]) == 1


@pytest.mark.parametrize("fallback", [False, True])
def test_workspace_gemini_does_not_receive_a_legacy_voice_id(
    monkeypatch, captured_plugins, fallback
):
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    monkeypatch.setattr(settings, "hail_gemini_tts_enabled", True)
    org = ResolvedLayer("gemini", None, {"voice_id": "Charon"}, fallback)

    renderer = build_tts(org, voice_id_override="saved-legacy", language="en")
    if fallback:
        primary, legacy = renderer._tts_instances
        assert legacy.label == "cartesia"
        assert captured_plugins["cartesia"][0].kwargs["voice"] == "house-voice"
    else:
        primary = renderer
        assert "cartesia" not in captured_plugins
    assert primary.label == "gemini"
    assert captured_plugins["gemini"][0].kwargs["voice_name"] == "Charon"


def test_workspace_gemini_uses_an_explicit_per_call_voice(
    monkeypatch, captured_plugins
):
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    org = ResolvedLayer("gemini", None, {"voice_id": "Charon"}, False)

    renderer = build_tts(org, "Kore", "en", voice_id_explicit=True)
    assert renderer.label == "gemini"
    assert captured_plugins["gemini"][0].kwargs["voice_name"] == "Kore"


@pytest.mark.parametrize(
    "voice_id",
    ["Charon", "21m00Tcm4TlvDq8ikWAM", "228fca29-3a0a-435c-8728-5cb483251068"],
)
def test_workspace_gemini_language_fallback_uses_configured_voices(
    monkeypatch, captured_plugins, voice_id
):
    org = ResolvedLayer("gemini", "workspace", {"voice_id": "Charon"}, True)
    monkeypatch.setattr(settings, "eleven_api_key", "house-eleven")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "eleven-house-voice")

    chain = build_tts(org, voice_id, "fr", voice_id_explicit=True)

    assert [item.label for item in chain._tts_instances] == ["cartesia", "elevenlabs"]
    assert captured_plugins["cartesia"][0].kwargs["voice"] == "house-voice"
    assert captured_plugins["elevenlabs"][0].kwargs["voice_id"] == "eleven-house-voice"


@pytest.mark.parametrize("provider", ["cartesia", "elevenlabs"])
def test_fallback_honors_a_voice_with_a_known_provider(
    monkeypatch, captured_plugins, provider
):
    voice = "configured-french-voice"
    org = ResolvedLayer("gemini", "workspace", {"voice_id": "Charon"}, True)
    monkeypatch.setattr(settings, "eleven_api_key", "house-eleven")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "eleven-house-voice")
    monkeypatch.setattr(
        settings,
        "voice_shortlist_preferences",
        {
            "fr": [
                VoicePreference(
                    provider=provider, voice_id=voice, reason="French calls"
                )
            ]
        },
    )

    chain = build_tts(org, voice, "fr", voice_id_explicit=True)
    field = "voice" if provider == "cartesia" else "voice_id"
    default = "house-voice" if provider == "cartesia" else "eleven-house-voice"

    assert chain._tts_instances[0].label == provider
    assert [item.kwargs[field] for item in captured_plugins[provider]] == [
        default,
        voice,
    ]


@pytest.mark.parametrize(
    ("provider", "voice", "model"),
    [("assemblyai", "aai-audrey", "assemblyai-tts-1"), ("xai", "ara", None)],
)
def test_workspace_uses_native_tts_with_the_workspace_key(
    monkeypatch, provider, voice, model
):
    constructor = Mock()
    if provider == "xai":
        monkeypatch.setattr(livekit_deadline, "XaiTTS", constructor)
    else:
        monkeypatch.setattr(
            import_module("livekit.plugins.assemblyai"), "TTS", constructor
        )
    org = ResolvedLayer(provider, "workspace-key", {"voice_id": voice}, False)

    assert build_tts(org, language="en") is constructor.return_value
    expected = {"api_key": "workspace-key", "voice": voice, "language": "en"}
    if provider == "xai":
        expected["tokenizer"] = ANY
    if model is not None:
        expected["model"] = model
    constructor.assert_called_once_with(**expected)


def test_invalid_assembly_voice_uses_a_house_voice_only_with_consent(
    captured_plugins,
):
    strict = ResolvedLayer("assemblyai", "workspace-key", {}, False)
    with pytest.raises(ValueError, match="voice"):
        build_tts(strict, " ", "en", voice_id_explicit=True)

    consent = ResolvedLayer("assemblyai", "workspace-key", {}, True)
    assert build_tts(consent, " ", "en", voice_id_explicit=True).label == "cartesia"
    assert captured_plugins["cartesia"][0].kwargs["voice"] == "house-voice"


def test_explicit_workspace_voice_keeps_its_legacy_fallback_order(
    monkeypatch, captured_plugins
):
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    monkeypatch.setattr(settings, "hail_gemini_tts_enabled", True)
    monkeypatch.setattr(settings, "eleven_api_key", "house-eleven")
    org = ResolvedLayer("cartesia", "workspace", {"voice_id": "saved"}, True)

    chain = build_tts(org, language="en")
    assert [provider.label for provider in chain._tts_instances] == [
        "cartesia",
        "cartesia",
        "elevenlabs",
    ]


def test_removed_xai_key_uses_house_speech_only_with_consent(
    monkeypatch, captured_plugins
):
    monkeypatch.setattr(settings, "xai_tts_api_key", "")
    org = ResolvedLayer("xai", None, {"voice_id": "ara"}, True)
    build_tts(org, language="en")
    assert captured_plugins["cartesia"][0].kwargs["voice"] == "house-voice"
    strict = ResolvedLayer("xai", None, {"voice_id": "ara"}, False)
    with pytest.raises(ValueError, match="No workspace or Hail key"):
        build_tts(strict, language="en")
