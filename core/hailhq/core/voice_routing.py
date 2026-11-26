"""Resolve the speech configuration used by both calls and saved-line playback."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from hailhq.core.config import settings
from hailhq.core.languages import TTSProvider, tts_providers_for
from hailhq.core.provider_config import (
    TTS_PRESET_MODELS,
    ResolvedLayer,
    configured_house_voice_provider,
    is_gemini_voice,
    tts_preset,
    tts_preset_configured,
)

logger = logging.getLogger(__name__)

# Attribute names keep resolution tied to the current settings, including on
# tests and installations that only configure one provider.
_LEGACY_SETTINGS = {
    "cartesia": ("cartesia_model", "cartesia_voice_id", "cartesia_api_key"),
    "elevenlabs": ("elevenlabs_model", "elevenlabs_voice_id", "eleven_api_key"),
}
# Match the pinned LiveKit adapters for saved provider rows with no voice.
# The house chain separately uses the configured house defaults.
_PROVIDER_DEFAULT_VOICES = {
    "cartesia": "f786b574-daa5-4673-aa0c-cbe3e8534c02",
    "elevenlabs": "hpp4J3VqNfWAUOO0d1Us",
}
_HOUSE_ORDER = ("gemini", "cartesia", "elevenlabs")


class ProviderKeyError(RuntimeError):
    """The requested provider could not be configured."""


class VoiceLanguageError(ProviderKeyError):
    """The workspace provider cannot speak the language and fallback is off."""


class SavedVoiceError(ProviderKeyError):
    """A configured provider can speak the language but cannot use the override."""


@dataclass(frozen=True)
class SpeechRoute:
    provider: TTSProvider
    model: str
    voice_id: str | None
    language: str | None
    api_key: str | None = field(repr=False)
    uses_workspace_key: bool


def house_credentials_available(provider: str) -> bool:
    legacy = _LEGACY_SETTINGS.get(provider)
    return (
        bool(getattr(settings, legacy[2]))
        if legacy
        else provider in TTS_PRESET_MODELS and tts_preset_configured(provider)
    )


def resolve_provider_voice(
    provider: str,
    language: str | None,
    params: dict,
    *,
    api_key: str | None = None,
    voice_id: str | None = None,
    use_house_defaults: bool = False,
) -> SpeechRoute:
    voice_id = voice_id or params.get("voice_id")
    # The pinned Cartesia adapter treats a saved empty key as unset.
    if provider == "cartesia" and api_key == "":
        api_key = None
    uses_workspace_key = api_key is not None
    if provider in _LEGACY_SETTINGS:
        model_setting, voice_setting, key_setting = _LEGACY_SETTINGS[provider]
        key = api_key if uses_workspace_key else getattr(settings, key_setting)
        if not key:
            raise ProviderKeyError(f"no workspace or house {provider} key available")
        model = params.get("model") or getattr(settings, model_setting)
        house_voice = getattr(settings, voice_setting) if use_house_defaults else None
        voice_id = voice_id or house_voice or _PROVIDER_DEFAULT_VOICES[provider]
        return SpeechRoute(provider, model, voice_id, language, key, uses_workspace_key)
    if provider in TTS_PRESET_MODELS:
        model = TTS_PRESET_MODELS[provider]
        if params.get("model") not in (None, model):
            raise ValueError(f"{provider} speech uses {model}.")
        default_voice, house_key = tts_preset(provider)
        key = api_key if uses_workspace_key else house_key
        if provider == "gemini" and (params.get("project") or params.get("location")):
            raise ValueError("Use a Gemini API key for workspace speech.")
        house_vertex = (
            provider == "gemini"
            and not uses_workspace_key
            and settings.google_cloud_project
        )
        if not house_vertex and (key is None or not key.strip()):
            raise ValueError(
                f"No workspace or Hail key is available for {provider} speech."
            )
        return SpeechRoute(
            provider,
            model,
            voice_id or default_voice,
            language,
            None if house_vertex else key,
            uses_workspace_key,
        )
    raise ProviderKeyError(f"unknown workspace speech provider '{provider}'")


def _house_routes(
    voice_id: str | None,
    language: str | None,
    primary: SpeechRoute | None = None,
    *,
    include_gemini: bool = True,
) -> list[SpeechRoute]:
    allowed = tts_providers_for(language)
    # Legacy call payloads carry unscoped voice IDs. Preserve their existing
    # route, including the Charon-only self-host case.
    explicit_gemini_voice = is_gemini_voice(voice_id) and not any(
        house_credentials_available(provider) for provider in _LEGACY_SETTINGS
    )
    routes = [
        resolve_provider_voice(
            provider,
            language,
            {},
            voice_id=voice_id,
            use_house_defaults=True,
        )
        for provider in _HOUSE_ORDER
        if provider in allowed
        and house_credentials_available(provider)
        and (
            provider != "gemini"
            or include_gemini
            and (not voice_id or explicit_gemini_voice)
        )
    ]
    if primary and primary.provider in TTS_PRESET_MODELS:
        # Avoid identical Cloud routes and try a different endpoint first.
        routes = [route for route in routes if route != primary]
        routes.sort(key=lambda route: route.provider == primary.provider)
    return routes


def resolve_tts_routes(
    org: ResolvedLayer | None,
    voice_id: str | None,
    language: str | None,
    *,
    voice_id_explicit: bool = False,
) -> list[SpeechRoute]:
    """Return the primary voice and its permitted fallbacks in call order."""
    preset_provider = org is not None and org.provider in TTS_PRESET_MODELS
    fallback_voice = None if preset_provider else voice_id

    if org and language and org.provider not in tts_providers_for(language):
        if not org.fallback_enabled:
            raise VoiceLanguageError(
                f"org tts provider '{org.provider}' does not support language '{language}' and fallback is disabled"
            )
        logger.warning(
            "Workspace speech provider %s does not support %s. Using Hail fallback.",
            org.provider,
            language,
        )
        house = _house_routes(fallback_voice, language)
        if preset_provider and voice_id_explicit:
            provider = configured_house_voice_provider(voice_id, language)
            for route in house:
                if route.provider == provider:
                    selected = resolve_provider_voice(
                        provider, language, {}, voice_id=voice_id
                    )
                    if route == selected:
                        house.remove(route)
                    house.insert(0, selected)
                    break
    elif org:
        try:
            primary_voice = voice_id
            if (
                preset_provider
                and not voice_id_explicit
                and not (org.provider == "gemini" and is_gemini_voice(primary_voice))
                and primary_voice
                not in (tts_preset(org.provider)[0], org.params.get("voice_id"))
            ):
                primary_voice = None
            primary = resolve_provider_voice(
                org.provider,
                language,
                org.params,
                api_key=org.api_key,
                voice_id=primary_voice,
            )
        except ValueError:
            if not preset_provider or not org.fallback_enabled:
                raise
            logger.warning(
                "Could not configure workspace speech provider %s. Using Hail fallback.",
                org.provider,
            )
            house = _house_routes(fallback_voice, language)
        else:
            if not org.fallback_enabled:
                return [primary]
            explicit_legacy_voice = not preset_provider and org.params.get("voice_id")
            return [
                primary,
                *_house_routes(
                    fallback_voice,
                    language,
                    primary,
                    include_gemini=not explicit_legacy_voice,
                ),
            ]
    else:
        house = _house_routes(voice_id, language)

    if house:
        return house
    if voice_id and _house_routes(None, language):
        raise SavedVoiceError("No configured TTS provider can use the saved voice.")
    if _house_routes(None, None):
        providers = ", ".join(sorted(tts_providers_for(language)))
        raise ProviderKeyError(
            f"Configured TTS providers cannot speak language '{language}'; "
            f"language requires: {providers}."
        )
    raise ProviderKeyError(
        "No TTS provider configured: configure Gemini or set "
        "CARTESIA_API_KEY or ELEVEN_API_KEY."
    )
