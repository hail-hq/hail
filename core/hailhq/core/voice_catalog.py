"""Small language-aware shortlists from Cartesia's public voice catalogue.

GETs only. Provider-owned samples are used when available. English keeps
the deployed voice first, followed by a feminine narrator and a deeper male.
Other languages prefer a conversational female, a male, and a narrator.
These are starting choices for listening, not a quality ranking.
Reference: https://docs.cartesia.ai/api-reference/voices/list (2026-10-19).
"""

from __future__ import annotations

import time

import httpx
from hailhq.core.config import settings

_cache: dict[str, tuple[float, list[dict]]] = {}
_ENGLISH = (
    "4bc3cb8c-adb9-4bb8-b5d5-cbbef950b991",  # George: existing British voice
    "a33f7a4c-100f-41cf-a1fd-5822e8fc253f",  # Lauren: feminine narrator
    "5ee9feff-1265-424a-9d7f-8e4d431a12c7",  # Ronald: deeper masculine
)
HEADERS_VERSION = "2026-03-01"


def voice_accent(row: dict, language: str) -> dict | None:
    """Only label an accent when the provider gives one native locale.

    Country of origin and language alone do not identify an accent. A voice
    can also have several non-native accents, which these samples do not use.
    """
    native = [
        accent
        for accent in (row.get("accents") or [])
        if isinstance(accent, dict)
        and accent.get("is_native") is True
        and str(accent.get("locale", "")).replace("_", "-").split("-")[0] == language
    ]
    if len(native) != 1:
        return None
    accent = native[0]
    locale = accent["locale"].replace("_", "-").split("-")
    region = locale[-1] if len(locale) > 1 else ""
    region = (
        region.upper()
        if len(region) == 2 and region.isascii() and region.isalpha()
        else None
    )
    label = str(accent.get("accent", "")).replace("-", " ").strip().title()
    return {"label": label, "country_code": region} if label else None


def shortlist(rows: list[dict], language: str, default_id: str = "") -> list[dict]:
    """Deterministic variety, native-language voices only, at most three."""
    rows = [
        r
        for r in rows
        if r.get("language") == language
        and r.get("id")
        and r.get("name")
        and r.get("is_owner") is not True
        and r.get("status", "active") == "active"
    ]
    selected: list[dict] = []
    preferred = (default_id, *_ENGLISH) if language == "en" else ()
    for voice_id in preferred:
        row = next((r for r in rows if r["id"] == voice_id), None)
        if row and row not in selected and len(selected) < 3:
            selected.append(row)
    for gender, words in (
        ("feminine", ("convers", "support", "friendly")),
        ("masculine", ("deep", "calm", "warm")),
        (None, ("narrat", "story", "expressive")),
    ):
        candidates = [
            r
            for r in rows
            if r not in selected and (not gender or r.get("gender") == gender)
        ]
        candidates.sort(
            key=lambda r: (
                -sum(
                    word in (r.get("description", "") + r["name"]).lower()
                    for word in words
                ),
                not bool(r.get("preview_file_url")),
                r["id"],
            )
        )
        if candidates and len(selected) < 3:
            selected.append(candidates[0])
    for row in rows:
        if len(selected) == 3:
            break
        if row not in selected:
            selected.append(row)
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "description": r.get("description", ""),
            "gender": r.get("gender", ""),
            "language": language,
            "accent": voice_accent(r, language),
            "provider": "cartesia",
            "preview_url": (
                r.get("preview_file_url")
                if str(r.get("preview_file_url", "")).startswith("https://")
                else None
            ),
        }
        for r in selected
    ]


async def recommended_voices(language: str) -> list[dict]:
    cached = _cache.get(language)
    if cached and time.monotonic() - cached[0] < 900:
        return cached[1]
    if not settings.cartesia_api_key:
        raise ValueError(
            "Voice choices are unavailable. You can use your current voice or add a provider."
        )
    headers = {
        "Authorization": f"Bearer {settings.cartesia_api_key}",
        "Cartesia-Version": HEADERS_VERSION,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            "https://api.cartesia.ai/voices",
            headers=headers,
            params={"language": language, "limit": 100, "expand[]": "preview_file_url"},
        )
        response.raise_for_status()
        body = response.json()
        rows = body.get("data", []) if isinstance(body, dict) else body
        # The configured English voice may fall outside the first catalogue page.
        if (
            language == "en"
            and settings.cartesia_voice_id
            and not any(r.get("id") == settings.cartesia_voice_id for r in rows)
        ):
            current = await client.get(
                f"https://api.cartesia.ai/voices/{settings.cartesia_voice_id}",
                headers=headers,
                params={"expand[]": "preview_file_url"},
            )
            if current.is_success:
                rows.append(current.json())
    voices = shortlist(rows, language, settings.cartesia_voice_id)
    _cache[language] = (time.monotonic(), voices)
    return voices
