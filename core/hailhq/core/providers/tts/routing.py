"""Resolve the speech configuration used by both calls and saved-line playback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
from hailhq.core.config import settings
from hailhq.core.languages import SUPPORTED_LANGUAGES, TTSProvider, tts_providers_for
from hailhq.core.provider_config import ResolvedLayer
from hailhq.core.providers.tts.direct import (
    DIRECT_VOICES,
    DirectSpeech,
)
from hailhq.core.providers.tts.direct import (
    house_voice_ready as direct_credentials_available,
)

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
    direct: DirectSpeech | None = None


def house_credentials_available(provider: str) -> bool:
    legacy = _LEGACY_SETTINGS.get(provider)
    return (
        bool(getattr(settings, legacy[2]))
        if legacy
        else direct_credentials_available(provider)
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
    uses_workspace_key = api_key is not None
    if provider in _LEGACY_SETTINGS:
        model_setting, voice_setting, key_setting = _LEGACY_SETTINGS[provider]
        key = api_key if uses_workspace_key else getattr(settings, key_setting)
        if not key:
            raise ProviderKeyError(f"no workspace or house {provider} key available")
        model = params.get("model") or getattr(settings, model_setting)
        voice_id = voice_id or (
            getattr(settings, voice_setting)
            if use_house_defaults
            else _PROVIDER_DEFAULT_VOICES[provider]
        )
        return SpeechRoute(provider, model, voice_id, language, key, uses_workspace_key)
    if provider in DIRECT_VOICES:
        direct = DirectSpeech.resolve(provider, params, api_key, voice_id)
        return SpeechRoute(
            provider,
            direct.model,
            direct.voice,
            language,
            direct.api_key,
            uses_workspace_key,
            direct,
        )
    raise ProviderKeyError(f"unknown workspace speech provider '{provider}'")


def _house_routes(
    voice_id: str | None, language: str | None, primary: SpeechRoute | None = None
) -> list[SpeechRoute]:
    allowed = tts_providers_for(language)
    # Legacy call payloads carry unscoped voice IDs. Preserve their existing
    # route, including the Charon-only self-host case.
    explicit_house_charon = voice_id == DIRECT_VOICES["gemini"][1] and not any(
        house_credentials_available(provider) for provider in _LEGACY_SETTINGS
    )
    routes = [
        resolve_provider_voice(
            provider, language, {}, voice_id=voice_id, use_house_defaults=True
        )
        for provider in _HOUSE_ORDER
        if provider in allowed
        and house_credentials_available(provider)
        and (
            provider != "gemini"
            or settings.hail_gemini_tts_enabled
            and (not voice_id or explicit_house_charon)
        )
    ]
    if primary and primary.direct:
        # Avoid identical Cloud routes and try a different endpoint first.
        routes = [route for route in routes if route != primary]
        routes.sort(key=lambda route: route.provider == primary.provider)
    return routes


async def resolve_fallback_voice_provider(
    org: ResolvedLayer | None, voice_id: str | None, language: str | None
) -> str | None:
    """Check an old, unscoped voice ID before handing it to another provider."""
    if (
        org is None
        or org.provider not in DIRECT_VOICES
        or not org.fallback_enabled
        or language not in SUPPORTED_LANGUAGES
        or not voice_id
        or org.provider in tts_providers_for(language)
        or voice_id in (org.params.get("voice_id"), DIRECT_VOICES[org.provider][1])
    ):
        return None

    # Imported here because the catalogue also uses the house route to find
    # the default. Verification is only needed for old unscoped overrides.
    from hailhq.core.voice_catalog import validate_voice_language

    async def compatible(route: SpeechRoute) -> str | None:
        try:
            async with asyncio.timeout(0.8):
                await validate_voice_language(
                    route.provider, voice_id, route.model, route.api_key or "", language
                )
        except (TimeoutError, httpx.HTTPError, ValueError, AttributeError, TypeError):
            return None
        return route.provider

    providers = await asyncio.gather(
        *(
            compatible(route)
            for route in _house_routes(voice_id, language)
            if not route.direct
        )
    )
    return next((provider for provider in providers if provider), None)


def resolve_tts_routes(
    org: ResolvedLayer | None,
    voice_id: str | None,
    language: str | None,
    *,
    fallback_voice_provider: str | None = None,
) -> list[SpeechRoute]:
    """Return the primary voice and its permitted fallbacks in call order."""
    if org and language and org.provider not in tts_providers_for(language):
        if not org.fallback_enabled:
            raise VoiceLanguageError(
                f"org tts provider '{org.provider}' does not support language '{language}' and fallback is disabled"
            )
        if org.provider in DIRECT_VOICES:
            house = _house_routes(None, language)
            if voice_id and fallback_voice_provider:
                house.insert(
                    0,
                    resolve_provider_voice(
                        fallback_voice_provider, language, {}, voice_id=voice_id
                    ),
                )
        else:
            house = _house_routes(voice_id, language)
    elif org:
        try:
            primary = resolve_provider_voice(
                org.provider,
                language,
                org.params,
                api_key=org.api_key,
                voice_id=voice_id,
            )
        except (ValueError, ProviderKeyError):
            if not org.fallback_enabled:
                raise
            house = _house_routes(None, language)
        else:
            if not org.fallback_enabled:
                return [primary]
            fallback_voice_id = None if primary.direct else voice_id
            return [primary, *_house_routes(fallback_voice_id, language, primary)]
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
        "No TTS provider configured: enable Google Cloud speech or set "
        "CARTESIA_API_KEY or ELEVEN_API_KEY."
    )
