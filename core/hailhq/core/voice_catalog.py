"""Provider catalogue discovery and a small, language-aware listening list.

No editorial voice IDs or description-keyword rankings. Walk all catalogue
pages, normalize verified language/accent data, then apply optional operator
preferences and deterministic metadata diversity. Availability is not a quality
score. See docs/voice-shortlists.md for sources and selection policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import defaultdict
from typing import get_args

import httpx
from hailhq.core.config import settings
from hailhq.core.languages import (
    CARTESIA_MODEL_LANGUAGES,
    SUPPORTED_LANGUAGES,
    TTSProvider,
    elevenlabs_language,
)
from hailhq.core.providers.tts.direct import DIRECT_VOICES
from hailhq.core.providers.tts.routing import (
    ProviderKeyError,
    house_credentials_available,
    resolve_provider_voice,
    resolve_tts_routes,
)

log = logging.getLogger(__name__)
HEADERS_VERSION = "2026-08-14"
_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_catalogue_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def language_options() -> list[dict]:
    """The pipeline registry, including languages with no house suggestions."""
    return [
        {"code": code, "name": caps.name}
        for code, caps in sorted(
            SUPPORTED_LANGUAGES.items(), key=lambda row: row[1].name
        )
    ]


def _base(locale: str) -> str:
    return locale.replace("_", "-").split("-")[0].lower()


def _accent(accents: list[dict]) -> dict | None:
    if len(accents) != 1:
        return None
    accent = accents[0]
    locale = str(accent.get("locale", "")).replace("_", "-").split("-")
    region = locale[-1] if len(locale) > 1 else ""
    region = (
        region.upper()
        if len(region) == 2 and region.isascii() and region.isalpha()
        else None
    )
    label = str(accent.get("accent") or "").replace("-", " ").strip().title()
    return {"label": label, "country_code": region} if label else None


def _matching_accents(row: dict, language: str) -> list[dict]:
    return [
        a
        for a in (row.get("accents") or [])
        if isinstance(a, dict) and _base(str(a.get("locale", ""))) == language
    ]


def voice_accent(row: dict, language: str) -> dict | None:
    """Report a supported target accent, including multilingual adaptations.

    is_native describes the original accent, not all supported languages.
    Avoid a flag when the base-language request has an ambiguous accent.
    """
    accents = _matching_accents(row, language)
    return _accent(accents)


def cartesia_choices(
    rows: list[dict], language: str, model: str, *, allow_private: bool = False
) -> list[dict]:
    allowed = settings.cartesia_model_languages.get(
        model, CARTESIA_MODEL_LANGUAGES.get(model, ())
    )
    if language not in allowed:
        return []
    choices = []
    for row in rows:
        if (
            (
                not allow_private
                and (row.get("is_owner") is True or row.get("access") != "public")
            )
            or row.get("status") != "active"
            or not row.get("id")
            or not row.get("name")
        ):
            continue
        accents = _matching_accents(row, language)
        # The primary language is provider metadata too; legacy entries may
        # have no accents. If accents exist, they must confirm the request.
        if not accents and (row.get("accents") or row.get("language") != language):
            continue
        choices.append(
            {
                "id": row["id"],
                "provider": "cartesia",
                "model": model,
                "name": row["name"],
                "description": row.get("description") or "",
                "gender": row.get("gender") or "",
                "language": language,
                "accent": voice_accent(row, language),
                "original_language": row.get("language") == language,
            }
        )
    return choices


def elevenlabs_choices(
    rows: list[dict],
    models: list[dict],
    language: str,
    model: str,
    *,
    allow_private: bool = False,
) -> list[dict]:
    capability = next(
        (
            m
            for m in models
            if m.get("model_id") == model and m.get("can_do_text_to_speech") is True
        ),
        None,
    )
    code = elevenlabs_language(language)
    if not capability or code not in {
        entry.get("language_id") for entry in capability.get("languages", [])
    }:
        return []
    choices = []
    for row in rows:
        # Default/premade voices avoid exposing house-account private clones
        # or assuming a shared library voice is accessible on a customer's key.
        if (
            (not allow_private and row.get("category") != "premade")
            or not row.get("voice_id")
            or not row.get("name")
        ):
            continue
        verified = [
            v
            for v in (row.get("verified_languages") or [])
            if isinstance(v, dict)
            and v.get("language") == code
            and v.get("model_id") == model
        ]
        if not verified:
            continue
        labels = row.get("labels") or {}
        choices.append(
            {
                "id": row["voice_id"],
                "provider": "elevenlabs",
                "model": model,
                "name": row["name"],
                "description": row.get("description") or "",
                "gender": {
                    "male": "masculine",
                    "female": "feminine",
                    "neutral": "gender_neutral",
                }.get(labels.get("gender"), ""),
                "language": language,
                "accent": _accent(verified),
                "original_language": labels.get("language") == code,
            }
        )
    return choices


def shortlist(
    choices: list[dict],
    language: str,
    *,
    preferred_voices: tuple[tuple[str, str], ...] = (),
) -> list[dict]:
    """Rank the rollout shortlist, configured preferences, and three other voices."""

    def identity(voice):
        return voice["provider"], voice["id"]

    available = {identity(v): v for v in choices if v["language"] == language}
    selected: list[dict] = []
    preferred = [
        *preferred_voices,
        *(
            (p.provider, p.voice_id)
            for p in settings.voice_shortlist_preferences.get(language, [])
        ),
    ]
    # Preserve a compatible configured default without pinning any voice ID.
    preferred.extend(
        [
            ("cartesia", settings.cartesia_voice_id),
            ("elevenlabs", settings.elevenlabs_voice_id),
        ]
    )
    capacity = 3 + len(preferred_voices)
    for key in preferred:
        if (
            key in available
            and available[key] not in selected
            and len(selected) < capacity
        ):
            selected.append(available[key])
    while len(selected) < capacity:
        candidates = [v for v in available.values() if v not in selected]
        if not candidates:
            break

        def rank(voice):
            genders = {v["gender"] for v in selected if v["gender"]}
            providers = {v["provider"] for v in selected}
            accents = {(v.get("accent") or {}).get("country_code") for v in selected}
            return (
                not voice.get("original_language", False),
                not bool(voice["gender"]) or voice["gender"] in genders,
                voice["provider"] in providers,
                (voice.get("accent") or {}).get("country_code") in accents,
                voice["name"].casefold(),
                voice["provider"],
                voice["id"],
            )

        selected.append(min(candidates, key=rank))
    return [
        {k: v for k, v in row.items() if k != "original_language"} for row in selected
    ]


async def _pages(client: httpx.AsyncClient, provider: str) -> list[dict]:
    """Never silently rank only page one or accept a looping/broken cursor."""
    if provider == "cartesia":
        url, items, cursor = "https://api.cartesia.ai/voices", "data", "starting_after"
        params = {"limit": 100, "is_owner": "false"}
    else:
        url, items, cursor = (
            "https://api.elevenlabs.io/v2/voices",
            "voices",
            "next_page_token",
        )
        params = {
            "page_size": 100,
            "voice_type": "default",
            "include_total_count": "false",
        }
    rows, seen = [], set()
    for _ in range(100):
        response = await client.get(url, params=params)
        response.raise_for_status()
        body = response.json()
        page = body.get(items)
        if not isinstance(page, list) or not all(isinstance(row, dict) for row in page):
            raise ValueError("Invalid voice catalogue page")
        rows.extend(page)
        if not body.get("has_more"):
            return rows
        token = body.get("next_page" if provider == "cartesia" else "next_page_token")
        if not token or token in seen:
            raise ValueError("Voice catalogue pagination did not advance")
        seen.add(token)
        params[cursor] = token
    raise ValueError("Voice catalogue pagination exceeded its request budget")


async def _provider_catalogue(provider: str, model: str, key: str) -> dict:
    cache_key = (provider, model, hashlib.sha256(key.encode()).hexdigest())
    async with _catalogue_locks[provider]:
        cached = _cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 900:
            return cached[1]
        headers = (
            {"Authorization": f"Bearer {key}", "Cartesia-Version": HEADERS_VERSION}
            if provider == "cartesia"
            else {"xi-api-key": key}
        )
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            rows = await _pages(client, provider)
            models = []
            if provider == "elevenlabs":
                response = await client.get("https://api.elevenlabs.io/v1/models")
                response.raise_for_status()
                models = response.json()
                if not isinstance(models, list) or not all(
                    isinstance(row, dict) for row in models
                ):
                    raise ValueError("Invalid model catalogue")
        result = {"rows": rows, "models": models}
        _cache[cache_key] = (time.monotonic(), result)
        if len(_cache) > 8:
            del _cache[next(iter(_cache))]
        return result


async def _catalogue_choices(provider: str, language: str) -> list[dict]:
    route = resolve_provider_voice(provider, language, {}, use_house_defaults=True)
    if route.direct:
        _, _, name, gender = DIRECT_VOICES[provider]
        return [
            {
                "provider": provider,
                "model": route.model,
                "id": route.voice_id,
                "name": name,
                "gender": gender,
                "language": language,
                "description": "",
                "accent": None,
            }
        ]
    if not route.model or not route.api_key:
        return []
    result = await _provider_catalogue(provider, route.model, route.api_key)
    if provider == "cartesia":
        return cartesia_choices(result["rows"], language, route.model)
    return elevenlabs_choices(result["rows"], result["models"], language, route.model)


async def voice_catalogue(language: str, *, include_direct: bool = False) -> dict:
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError("Choose a supported language")

    # The flag keeps older consoles, which validate two provider IDs, working
    # while the API and console roll out independently.
    providers = [
        provider
        for provider in get_args(TTSProvider)
        if provider in SUPPORTED_LANGUAGES[language].tts
        and house_credentials_available(provider)
        and (include_direct or provider not in DIRECT_VOICES)
    ]
    results = await asyncio.gather(
        *(_catalogue_choices(provider, language) for provider in providers),
        return_exceptions=True,
    )
    choices, partial = [], False
    for provider, result in zip(providers, results, strict=True):
        if isinstance(result, Exception):
            log.warning("Voice catalogue could not load for %s", provider)
            partial = True
        else:
            choices.extend(result)

    rollout_choices = tuple(
        (provider, voice_id)
        for provider, (_, voice_id, _, _) in DIRECT_VOICES.items()
        if any(v["provider"] == provider and v["id"] == voice_id for v in choices)
    )
    voices = shortlist(choices, language, preferred_voices=rollout_choices)
    try:
        house = resolve_tts_routes(None, None, language)
    except ProviderKeyError:
        house = []
    primary = next((route for route in house if route.provider in providers), None)
    default = next(
        (
            {"provider": voice["provider"], "id": voice["id"]}
            for voice in voices
            if primary
            and (voice["provider"], voice["id"], voice["model"])
            == (primary.provider, primary.voice_id, primary.model)
        ),
        None,
    )
    return {
        "voices": voices,
        "languages": language_options(),
        "partial": partial,
        "default_voice": default,
    }


def _voice_metadata(response: httpx.Response):
    try:
        return response.json()
    except ValueError as exc:
        raise httpx.DecodingError("Invalid voice metadata from provider.") from exc


async def validate_voice_language(
    provider: str, voice_id: str, model: str, key: str, language: str
) -> None:
    """Verify an explicit picker save with the key/model that will actually run.

    Private BYO voices can be retained if their metadata confirms support.
    Advanced provider editing remains available without this picker check.
    """
    import re

    if not key or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", voice_id):
        raise ValueError(
            "Voice support could not be verified. Choose a suggested voice or update Advanced."
        )
    headers = (
        {"Authorization": f"Bearer {key}", "Cartesia-Version": HEADERS_VERSION}
        if provider == "cartesia"
        else {"xi-api-key": key}
    )
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        if provider == "cartesia":
            response = await client.get(f"https://api.cartesia.ai/voices/{voice_id}")
            response.raise_for_status()
            voice = _voice_metadata(response)
            if not isinstance(voice, dict):
                raise httpx.DecodingError("Cartesia returned invalid voice metadata.")
            choices = cartesia_choices([voice], language, model, allow_private=True)
        elif provider == "elevenlabs":
            response = await client.get(
                f"https://api.elevenlabs.io/v1/voices/{voice_id}"
            )
            response.raise_for_status()
            models = await client.get("https://api.elevenlabs.io/v1/models")
            models.raise_for_status()
            voice = _voice_metadata(response)
            model_rows = _voice_metadata(models)
            if (
                not isinstance(voice, dict)
                or not isinstance(model_rows, list)
                or not all(isinstance(row, dict) for row in model_rows)
            ):
                raise httpx.DecodingError("ElevenLabs returned invalid voice metadata.")
            choices = elevenlabs_choices(
                [voice], model_rows, language, model, allow_private=True
            )
        else:
            choices = []
    if not choices:
        raise ValueError(
            "This voice's support for the language and model could not be verified. Choose a suggested voice or update Advanced."
        )
