"""Provider catalogue discovery and a small, language-aware listening list.

No editorial voice IDs or description-keyword rankings. Walk all catalogue
pages, normalize verified language/accent data, then apply optional operator
preferences and deterministic metadata diversity. Availability is not a quality
score. See docs/voice-shortlists.md for sources and selection policy.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from collections import defaultdict
from functools import partial

import google.auth
import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from hailhq.core.config import settings
from hailhq.core.languages import (
    CARTESIA_MODEL_LANGUAGES,
    SUPPORTED_LANGUAGES,
    elevenlabs_language,
    xai_language,
)
from hailhq.core.provider_config import (
    TTS_PRESET_MODELS,
    gemini_vertex_location,
    tts_preset,
    tts_preset_configured,
)

log = logging.getLogger(__name__)
HEADERS_VERSION = "2026-08-14"
_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_catalogue_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
# Each preset provider keeps one saved default voice (tts_preset). Every other
# voice comes from the provider's own list, cached here. AssemblyAI has no list.
_PRESET_LIST_TTL = 900.0
_PRESET_LIST_RETRY_SECONDS = 60.0
_preset_lists: dict[str, tuple[float, list[dict]]] = {}
# When a provider's last list load failed. A failed load keeps the old list.
_preset_failed_at: dict[str, float] = {}
_preset_tasks: dict[str, asyncio.Task] = {}
_GOOGLE_AUTH_TIMEOUT = 4.0
# Reuse one credentials object (it refreshes its own token). After a failed
# lookup, wait before asking again so picker loads do not each wait on it.
_GOOGLE_AUTH_RETRY_SECONDS = 60.0
_vertex_auth: dict[str, object] = {"credentials": None, "failed_at": None}
SAMPLE_RATE = 24000


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
        l.get("language_id") for l in capability.get("languages", [])
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


def shortlist(choices: list[dict], language: str) -> list[dict]:
    """Up to three compatible choices; curation, current default, then variety."""

    def identity(voice):
        return voice["provider"], voice["id"]

    available = {identity(v): v for v in choices if v["language"] == language}
    selected: list[dict] = []
    preferred = [
        (p.provider, p.voice_id)
        for p in settings.voice_shortlist_preferences.get(language, [])
    ]
    # Preserve a compatible configured default without pinning any voice ID.
    preferred.extend(
        [
            ("cartesia", settings.cartesia_voice_id),
            ("elevenlabs", settings.elevenlabs_voice_id),
        ]
    )
    for key in preferred:
        if key in available and available[key] not in selected and len(selected) < 3:
            selected.append(available[key])
    while len(selected) < 3:
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


async def voice_catalogue(language: str, *, include_direct: bool = False) -> dict:
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError("Choose a supported language")
    providers = [
        (p, model, key)
        for p, model, key in [
            ("cartesia", settings.cartesia_model, settings.cartesia_api_key),
            ("elevenlabs", settings.elevenlabs_model, settings.eleven_api_key),
        ]
        if key and model and p in SUPPORTED_LANGUAGES[language].tts
    ]
    results = await asyncio.gather(
        *[_provider_catalogue(*p) for p in providers], return_exceptions=True
    )
    choices, partial = [], False
    for (provider, model, _), result in zip(providers, results, strict=True):
        if isinstance(result, Exception):
            log.warning("Voice catalogue could not load for %s", provider)
            partial = True
            continue
        if provider == "cartesia":
            choices.extend(cartesia_choices(result["rows"], language, model))
        else:
            choices.extend(
                elevenlabs_choices(result["rows"], result["models"], language, model)
            )
    voices = shortlist(choices, language)
    default = next(
        (
            v
            for v in voices
            if v["provider"] == "cartesia" and v["id"] == settings.cartesia_voice_id
        ),
        None,
    )
    if not default and not settings.cartesia_api_key:
        default = next(
            (
                v
                for v in voices
                if v["provider"] == "elevenlabs"
                and v["id"] == settings.elevenlabs_voice_id
            ),
            None,
        )
    if include_direct:
        # Older consoles only accept the two original provider IDs.
        presets = [
            choice
            for provider in TTS_PRESET_MODELS
            if (choice := await _preset_choice(provider, language))
        ]
        # Only English calls inherit Gemini. Other languages keep their default,
        # and consoles that pick the first voice keep picking the same one.
        if language == "en":
            if presets and presets[0]["provider"] == "gemini":
                default = presets[0]
            voices = [*presets, *voices]
        else:
            voices = [*voices, *presets]
    return {
        "voices": voices,
        "languages": language_options(),
        "partial": partial,
        "default_voice": (
            {"provider": default["provider"], "id": default["id"]} if default else None
        ),
    }


def _gemini_list_key() -> str:
    # The list needs a Gemini API key. Calls on a Cloud project may not speak
    # every listed voice, so those keep the saved default only.
    return "" if settings.google_cloud_project else settings.google_api_key.strip()


def _list_key(provider: str) -> str:
    if provider == "gemini":
        return _gemini_list_key()
    return settings.xai_api_key.strip() if provider == "xai" else ""


async def _fetch_preset_voices(provider: str, key: str) -> list[dict]:
    """One provider's voices as {id, name, gender, locale}; locale None = any."""
    async with httpx.AsyncClient(timeout=15) as client:
        if provider == "xai":
            # https://docs.x.ai/developers/rest-api-reference/inference/voice
            response = await client.get(
                "https://api.x.ai/v1/tts/voices",
                headers={"Authorization": f"Bearer {key}"},
            )
            response.raise_for_status()
            body = response.json()
            rows = body.get("voices") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                raise TypeError("Invalid voice list")
            # Every xAI voice speaks every language xAI supports.
            return [
                {
                    "id": row["voice_id"],
                    "name": row.get("name") or row["voice_id"],
                    "gender": "",
                    "locale": None,
                }
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("voice_id"), str)
            ]
        # https://ai.google.dev/gemini-api/docs/speech-generation (GET voices)
        voices, token, seen = [], None, set()
        for _ in range(20):
            params: dict[str, str | int] = {"page_size": 1000}
            if token:
                params["page_token"] = token
            response = await client.get(
                "https://generativelanguage.googleapis.com/v1beta/voices",
                headers={"x-goog-api-key": key},
                params=params,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise TypeError("Invalid voice list")
            rows = body.get("voices")
            if not isinstance(rows, list):
                raise TypeError("Invalid voice list")
            voices.extend(
                {
                    "id": row["id"],
                    "name": row.get("display_name") or row["id"],
                    "gender": row.get("gender") or "",
                    "locale": row.get("language_code") or None,
                }
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("id"), str)
            )
            token = body.get("next_page_token")
            if not token:
                return voices
            if token in seen:
                raise ValueError("Voice list pagination did not advance")
            seen.add(token)
        raise ValueError("Voice list pagination exceeded its request budget")


async def _refresh_preset_voices(provider: str, key: str) -> list[dict]:
    try:
        rows = await _fetch_preset_voices(provider, key)
    except Exception:
        # Fail open: keep the last good list, and wait before asking again.
        log.warning("Voice list could not load for %s", provider, exc_info=True)
        _preset_failed_at[provider] = time.monotonic()
        cached = _preset_lists.get(provider)
        return cached[1] if cached else []
    _preset_failed_at.pop(provider, None)
    _preset_lists[provider] = (time.monotonic(), rows)
    return rows


async def preset_voices(provider: str, *, timeout: float | None = 10.0) -> list[dict]:
    """The provider's live voices; [] when it has no list or it is unavailable.

    A failed load is not retried for a minute and keeps the last good list.
    An old list is returned at once while a new one loads. A load that
    outlives ``timeout`` keeps running, so the next caller gets its result.
    """
    key = _list_key(provider)
    if not key:
        return []
    cached = _preset_lists.get(provider)
    now = time.monotonic()
    if cached and now - cached[0] < _PRESET_LIST_TTL:
        return cached[1]
    failed_at = _preset_failed_at.get(provider)
    if failed_at is not None and now - failed_at < _PRESET_LIST_RETRY_SECONDS:
        return cached[1] if cached else []
    loop = asyncio.get_running_loop()
    task = _preset_tasks.get(provider)
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(_refresh_preset_voices(provider, key))
        _preset_tasks[provider] = task
    if cached:
        return cached[1]
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except TimeoutError:
        return []


async def warm_preset_voices(timeout: float) -> None:
    """Load every voice list once, so a new call worker starts with them."""
    await asyncio.gather(
        *(preset_voices(p, timeout=timeout) for p in TTS_PRESET_MODELS)
    )


def reset_preset_voice_cache() -> None:
    """Forget loaded voice lists (tests, and a key change in one process)."""
    _preset_lists.clear()
    _preset_failed_at.clear()
    _preset_tasks.clear()


def _same_voice(provider: str, a: str, b: str) -> bool:
    # Gemini voice names match in any case ("Charon" and "charon").
    return a.casefold() == b.casefold() if provider == "gemini" else a == b


def known_preset_voice_owner(voice_id: str | None) -> str | None:
    """The owner from the saved defaults and loaded lists, without loading."""
    if not isinstance(voice_id, str) or not voice_id:
        return None
    for provider in TTS_PRESET_MODELS:
        if _same_voice(provider, voice_id, tts_preset(provider)[0]):
            return provider
    for provider in TTS_PRESET_MODELS:
        rows = _preset_lists.get(provider, (0.0, []))[1]
        if any(_same_voice(provider, voice_id, row["id"]) for row in rows):
            return provider
    return None


async def preset_voice_owner(voice_id: str | None, *, timeout: float = 1.0):
    """The preset provider that has ``voice_id``, or None when none is known."""
    if not voice_id:
        return None
    for provider in TTS_PRESET_MODELS:
        if _same_voice(provider, voice_id, tts_preset(provider)[0]):
            return provider
    listed = await asyncio.gather(
        *(preset_voices(p, timeout=timeout) for p in TTS_PRESET_MODELS)
    )
    for provider, rows in zip(TTS_PRESET_MODELS, listed, strict=True):
        if any(_same_voice(provider, voice_id, row["id"]) for row in rows):
            return provider
    return None


async def is_preset_voice(provider: str, voice_id: str) -> bool:
    if _same_voice(provider, voice_id, tts_preset(provider)[0]):
        return True
    rows = await preset_voices(provider)
    return any(_same_voice(provider, voice_id, row["id"]) for row in rows)


def _voice_label(voice_id: str) -> str:
    return voice_id.removeprefix("aai-").replace("-", " ").title()


async def _preset_choice(provider: str, language: str) -> dict | None:
    if provider not in SUPPORTED_LANGUAGES[language].tts:
        return None
    if not await preset_credentials_available(provider):
        return None
    default = tts_preset(provider)[0]
    rows = await preset_voices(provider)
    # A voice recorded in the call's language, its main region first.
    native = sorted(
        (
            row
            for row in rows
            if language != "en"
            and (row["locale"] or "").split("-")[0].casefold() == language
        ),
        key=lambda row: (
            row["locale"].casefold() != f"{language}-{language}",
            row["id"],
        ),
    )
    saved = next(
        (row for row in rows if _same_voice(provider, row["id"], default)), None
    )
    if native:
        voice = native[0]
    else:
        voice = {**(saved or {}), "id": default}
    return {
        "provider": provider,
        "model": TTS_PRESET_MODELS[provider],
        "id": voice["id"],
        "name": voice.get("name") or _voice_label(default),
        "gender": voice.get("gender") or "",
        "language": language,
        "description": "",
        "accent": None,
    }


def _vertex_credentials():
    if _vertex_auth["credentials"] is not None:
        return _vertex_auth["credentials"]
    failed_at = _vertex_auth["failed_at"]
    if failed_at is not None and time.monotonic() - failed_at < (
        _GOOGLE_AUTH_RETRY_SECONDS
    ):
        raise GoogleAuthError("Google Cloud credentials were unavailable.")
    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    except (GoogleAuthError, OSError):
        _vertex_auth["failed_at"] = time.monotonic()
        raise
    _vertex_auth.update(credentials=credentials, failed_at=None)
    return credentials


async def preset_credentials_available(provider: str) -> bool:
    if provider != "gemini" or not settings.google_cloud_project:
        return tts_preset_configured(provider)
    try:
        gemini_vertex_location()
        async with asyncio.timeout(_GOOGLE_AUTH_TIMEOUT):
            await asyncio.to_thread(_vertex_credentials)
    except TimeoutError:
        # The lookup thread keeps running; do not start another one per load.
        if _vertex_auth["credentials"] is None:
            _vertex_auth["failed_at"] = time.monotonic()
        return False
    except (GoogleAuthError, OSError, ValueError):
        return False
    return True


def _vertex_headers(url: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    _vertex_credentials().before_request(
        partial(Request(), timeout=_GOOGLE_AUTH_TIMEOUT), "POST", url, headers
    )
    return headers


async def synthesize_preset(
    client: httpx.AsyncClient,
    provider: str,
    voice: str,
    text: str,
    *,
    language: str = "en",
    api_key: str | None = None,
) -> bytes:
    """Fetch PCM for short signed previews and the explicit Test key action."""
    key = tts_preset(provider)[1] if api_key is None else api_key
    if provider == "gemini":
        model = TTS_PRESET_MODELS[provider]
        audio_format = {"audio": {"mimeType": "AUDIO_L16"}}
        use_vertex = api_key is None and bool(settings.google_cloud_project)
        if use_vertex:
            location = gemini_vertex_location()
            host = (
                "aiplatform.googleapis.com"
                if location == "global"
                else f"aiplatform.{location}.rep.googleapis.com"
            )
            project = settings.google_cloud_project
            url = (
                f"https://{host}/v1/projects/{project}/locations/{location}"
                f"/publishers/google/models/{model}:generateContent"
            )
            try:
                async with asyncio.timeout(_GOOGLE_AUTH_TIMEOUT):
                    headers = await asyncio.to_thread(_vertex_headers, url)
            except (GoogleAuthError, OSError, TimeoutError) as exc:
                if (
                    isinstance(exc, TimeoutError)
                    and _vertex_auth["credentials"] is None
                ):
                    # The lookup thread keeps running; do not start one per preview.
                    _vertex_auth["failed_at"] = time.monotonic()
                raise ValueError(
                    "Google Cloud speech credentials are unavailable."
                ) from exc
        else:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            headers = {"x-goog-api-key": key}
        response = await client.post(
            url,
            headers=headers,
            json={
                "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "responseFormat": [audio_format] if use_vertex else audio_format,
                    "speechConfig": {"voiceConfig": {"voice": voice}},
                },
            },
        )
        response.raise_for_status()
        return b"".join(
            base64.b64decode(inline["data"], validate=True)
            for candidate in response.json().get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
            if (inline := part.get("inlineData")) and inline.get("data")
        )

    headers = {"Authorization": f"Bearer {key}"}
    if provider == "xai":
        response = await client.post(
            "https://api.x.ai/v1/tts",
            headers=headers,
            json={
                "text": text,
                "voice_id": voice,
                "language": xai_language(language),
                "output_format": {"codec": "pcm", "sample_rate": SAMPLE_RATE},
            },
        )
    else:
        response = await client.post(
            "https://tts.us.assemblyai.com/v1/openai/audio/speech",
            headers=headers,
            json={
                "input": text,
                "voice": voice,
                "model": TTS_PRESET_MODELS[provider],
                "language": language,
                "response_format": "pcm",
                "sample_rate": SAMPLE_RATE,
            },
        )
    response.raise_for_status()
    return response.content


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
            choices = cartesia_choices(
                [response.json()], language, model, allow_private=True
            )
        elif provider == "elevenlabs":
            response = await client.get(
                f"https://api.elevenlabs.io/v1/voices/{voice_id}"
            )
            response.raise_for_status()
            models = await client.get("https://api.elevenlabs.io/v1/models")
            models.raise_for_status()
            choices = elevenlabs_choices(
                [response.json()], models.json(), language, model, allow_private=True
            )
        else:
            choices = []
    if not choices:
        raise ValueError(
            "This voice's support for the language and model could not be verified. Choose a suggested voice or update Advanced."
        )
