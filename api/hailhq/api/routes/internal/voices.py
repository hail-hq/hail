"""Authenticated catalogue, fixed-text samples, and first-line playback.

Catalogue previews speak one fixed greeting per language in each suggested
voice. Samples are shared in memory across workspaces for one hour.

Saved-line playback uses the same initial speech route as a call. The website
loads the line after checking the session, workspace, and per-user rate limit.
Lines of up to 400 characters are cached in memory for one hour, at most 256
at a time. Playback uses Hail credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import time
import unicodedata
import wave
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import UUID

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Response
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.core.db import session_scope
from hailhq.core.languages import (
    SUPPORTED_LANGUAGES,
    Language,
    TTSProvider,
    elevenlabs_language,
)
from hailhq.core.provider_config import (
    TTS_PRESET_MODELS,
    ResolvedLayer,
    load_org_provider_configs,
)
from hailhq.core.secret_cipher import SecretKeyMissing
from hailhq.core.voice_catalog import (
    HEADERS_VERSION,
    synthesize_preset,
    voice_catalogue,
)
from hailhq.core.voice_routing import (
    ProviderKeyError,
    SavedVoiceError,
    SpeechRoute,
    VoiceLanguageError,
    resolve_provider_voice,
    resolve_tts_routes,
)
from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

router = APIRouter(
    prefix="/internal/voices",
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)
# Key: provider, model, language, voice ID, SHA-256 of the spoken text.
_Key = tuple[str, str, str, str, str]
_Cache = OrderedDict[_Key, tuple[float, bytes]]
_samples: _Cache = OrderedDict()
_lines: _Cache = OrderedDict()
_line_failures: OrderedDict[_Key, float] = OrderedDict()
_LINE_CAP = 256
_SYNTHESIS_TIMEOUT_SECONDS = 20
_SYNTHESIS_RETRY_SECONDS = 15
_SYNTHESIS_ERRORS = (httpx.HTTPError, TimeoutError, ValueError, ProviderKeyError)
_CHOOSE_VOICE = "Choose an available voice in Call settings to preview this line."
_UNAVAILABLE_AGENT_VOICE = "Playback is not available for this agent's saved voice."


@dataclass
class _SynthesisFlight:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    requests: int = 0
    failed: bool = False


# Requests share one attempt per recording. Remove the entry when they finish.
_sample_locks: dict[tuple[int, _Key], _SynthesisFlight] = {}
_PlaybackKey = tuple[tuple[tuple[str, str, str | None, str | None], ...], str]


@dataclass
class _PlaybackFlight:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    requests: int = 0
    completed: bool = False
    audio: bytes | None = None


_playback_flights: dict[_PlaybackKey, _PlaybackFlight] = {}
_HELLO = {
    "ar": "مرحباً، كيف يمكنني مساعدتك اليوم؟",
    "bg": "Здравейте, как мога да ви помогна днес?",
    "bn": "হ্যালো, আজ আমি আপনাকে কীভাবে সাহায্য করতে পারি?",
    "cs": "Dobrý den, jak vám dnes mohu pomoci?",
    "da": "Hej, hvordan kan jeg hjælpe dig i dag?",
    "de": "Hallo, wie kann ich Ihnen heute helfen?",
    "el": "Γεια σας, πώς μπορώ να σας βοηθήσω σήμερα;",
    "en": "Hello, thanks for calling. How can I help you today?",
    "es": "Hola, gracias por llamar. ¿Cómo puedo ayudarte hoy?",
    "fi": "Hei, miten voin auttaa sinua tänään?",
    "fr": "Bonjour, merci de votre appel. Comment puis-je vous aider aujourd'hui ?",
    "gu": "નમસ્તે, આજે હું તમને કેવી રીતે મદદ કરી શકું?",
    "he": "שלום, איך אוכל לעזור לך היום?",
    "hi": "नमस्ते, आज मैं आपकी कैसे मदद कर सकता हूँ?",
    "hr": "Dobar dan, kako vam danas mogu pomoći?",
    "hu": "Üdvözlöm, hogyan segíthetek ma?",
    "id": "Halo, bagaimana saya bisa membantu Anda hari ini?",
    "it": "Buongiorno, come posso aiutarla oggi?",
    "ja": "こんにちは。今日はどのようなご用件でしょうか。",
    "kn": "ನಮಸ್ಕಾರ, ಇಂದು ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
    "ko": "안녕하세요. 오늘 무엇을 도와드릴까요?",
    "mr": "नमस्कार, आज मी तुम्हाला कशी मदत करू शकतो?",
    "ms": "Helo, bagaimana saya boleh membantu anda hari ini?",
    "nl": "Hallo, hoe kan ik u vandaag helpen?",
    "no": "Hei, hvordan kan jeg hjelpe deg i dag?",
    "pl": "Dzień dobry, jak mogę dziś pomóc?",
    "pt": "Olá, como posso ajudar hoje?",
    "ro": "Bună ziua, cu ce vă pot ajuta astăzi?",
    "ru": "Здравствуйте, чем я могу вам помочь сегодня?",
    "sk": "Dobrý deň, ako vám dnes môžem pomôcť?",
    "sv": "Hej, hur kan jag hjälpa dig idag?",
    "ta": "வணக்கம், இன்று நான் உங்களுக்கு எப்படி உதவ முடியும்?",
    "te": "నమస్కారం, ఈ రోజు నేను మీకు ఎలా సహాయపడగలను?",
    "th": "สวัสดีค่ะ วันนี้มีอะไรให้ช่วยไหมคะ",
    "tl": "Kumusta, paano kita matutulungan ngayon?",
    "tr": "Merhaba, bugün size nasıl yardımcı olabilirim?",
    "uk": "Вітаю, чим я можу вам допомогти сьогодні?",
    "vi": "Xin chào, hôm nay tôi có thể giúp gì cho bạn?",
    "zh": "您好，今天有什么可以帮您？",
}


@router.get("")
async def list_voices(language: Language = "en", include_direct: bool = False) -> dict:
    try:
        return await voice_catalogue(language, include_direct=include_direct)
    except (httpx.HTTPError, ValueError, ProviderKeyError) as exc:
        raise HTTPException(
            503, "Voice choices could not load. Try again or keep your current voice."
        ) from exc


class PreviewIn(BaseModel):
    language: Language
    provider: TTSProvider
    voice_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class LineText(BaseModel):
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)
    ]

    @field_validator("text")
    @classmethod
    def _no_control_characters(cls, text: str) -> str:
        if any(unicodedata.category(char) == "Cc" for char in text):
            raise ValueError("Use one line of text without control characters.")
        return text


class LineIn(PreviewIn, LineText):
    provider: Literal["cartesia", "elevenlabs"]


class WorkspaceLineIn(LineText):
    organization_id: UUID
    language: Language | None = None
    voice_id: str | None = None

    @field_validator("text")
    @classmethod
    def _no_line_separators(cls, text: str) -> str:
        if any(unicodedata.category(char) in {"Zl", "Zp"} for char in text):
            raise ValueError("Use one line of text without line separators.")
        return text


async def _catalogue_model(body: PreviewIn) -> str:
    catalogue = await voice_catalogue(
        body.language, include_direct=body.provider in TTS_PRESET_MODELS
    )
    choice = next(
        (
            v
            for v in catalogue["voices"]
            if v["id"] == body.voice_id and v["provider"] == body.provider
        ),
        None,
    )
    if choice is None:
        raise HTTPException(422, "Choose one of the suggested voices.")
    return choice["model"]


def _wav(audio: bytes) -> Response:
    return Response(
        audio,
        media_type="audio/wav",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/preview")
async def preview_voice(body: PreviewIn) -> Response:
    try:
        model = await _catalogue_model(body)
        text = _HELLO[body.language]
        cap = 3 * len(SUPPORTED_LANGUAGES) + len(TTS_PRESET_MODELS)
        return _wav(await _audio(body, model, text, _samples, cap))
    except _SYNTHESIS_ERRORS as exc:
        raise HTTPException(503, "This sample could not play. Try again.") from exc


@router.post("/line")
async def speak_line(body: LineIn) -> Response:
    try:
        model = await _catalogue_model(body)
        return _wav(await _audio(body, model, body.text, _lines, _LINE_CAP))
    except _SYNTHESIS_ERRORS as exc:
        raise HTTPException(503, "This line could not play. Try again.") from exc


@router.post("/workspace-line")
async def speak_workspace_line(body: WorkspaceLineIn) -> Response:
    try:
        async with session_scope() as db:
            rows = await load_org_provider_configs(db, body.organization_id)
            row = rows.get("tts")
            org = ResolvedLayer.from_row(row) if row is not None else None
    except (SecretKeyMissing, InvalidToken) as exc:
        raise HTTPException(503, "This line could not play. Try again.") from exc

    language = body.language or (org.params.get("language") if org else None)
    if language and language not in SUPPORTED_LANGUAGES:
        raise HTTPException(409, "No voice is available for this language.")
    try:
        routes = resolve_tts_routes(org, body.voice_id, language)
    except VoiceLanguageError as exc:
        raise HTTPException(
            409, "The saved provider cannot speak this language."
        ) from exc
    except SavedVoiceError as exc:
        raise HTTPException(409, _UNAVAILABLE_AGENT_VOICE) from exc
    except (ValueError, ProviderKeyError) as exc:
        detail = (
            _CHOOSE_VOICE
            if org and not org.fallback_enabled
            else "No voice is available for this language."
        )
        raise HTTPException(409, detail) from exc

    primary = routes[0]
    if body.voice_id and body.voice_id != primary.voice_id:
        raise HTTPException(409, _UNAVAILABLE_AGENT_VOICE)
    if primary.uses_workspace_key:
        raise HTTPException(
            409, "Playback is not available for a voice on your own provider key."
        )

    # Inherited voices follow the call's permitted fallbacks. Recovering a
    # saved agent voice requires the same ID because old overrides are unscoped.
    playable = list(
        dict.fromkeys(
            route
            for route in routes
            if not route.uses_workspace_key
            and (not body.voice_id or route.voice_id == body.voice_id)
        )
    )
    try:
        preview = PreviewIn(
            language=language or "en",
            provider=primary.provider,
            voice_id=primary.voice_id,
        )
    except ValidationError as exc:
        detail = _UNAVAILABLE_AGENT_VOICE if body.voice_id else _CHOOSE_VOICE
        raise HTTPException(409, detail) from exc

    audio = await _playback_audio(playable, preview, body.text)
    if audio is None:
        raise HTTPException(503, "This line could not play. Try again.")
    return _wav(audio)


async def _playback_audio(
    playable: list[SpeechRoute], preview: PreviewIn, text: str
) -> bytes | None:
    # Only Hail-managed routes reach this cache. Include their order so
    # requests with different fallback permissions never share an attempt.
    key = (
        tuple((r.provider, r.model, r.language, r.voice_id) for r in playable),
        hashlib.sha256(text.encode()).hexdigest(),
    )
    flight = _playback_flights.setdefault(key, _PlaybackFlight())
    flight.requests += 1
    try:
        async with flight.lock:
            if not flight.completed:
                flight.audio = await _synthesize_playback(playable, preview, text)
                flight.completed = True
            return flight.audio
    finally:
        flight.requests -= 1
        if flight.requests == 0:
            del _playback_flights[key]


async def _synthesize_playback(
    playable: list[SpeechRoute], preview: PreviewIn, text: str
) -> bytes | None:
    for index, route in enumerate(playable):
        try:
            audio = await _audio(
                preview,
                route.model,
                text,
                _lines,
                _LINE_CAP,
                route=route,
                has_backup=index < len(playable) - 1,
            )
        except _SYNTHESIS_ERRORS:
            continue
        return audio
    return None


async def _audio(
    body: PreviewIn,
    model: str,
    text: str,
    cache: _Cache,
    cap: int,
    *,
    route: SpeechRoute | None = None,
    has_backup: bool = False,
) -> bytes:
    digest = hashlib.sha256(text.encode()).hexdigest()
    provider = route.provider if route else body.provider
    language = (route.language or "") if route else body.language
    voice_id = (route.voice_id or "") if route else body.voice_id
    cache_key = (provider, model, language, voice_id, digest)
    lock_key = (id(cache), cache_key)
    flight = _sample_locks.setdefault(lock_key, _SynthesisFlight())
    flight.requests += 1
    try:
        async with flight.lock:
            cached = cache.get(cache_key)
            if cached and time.monotonic() < cached[0]:
                cache.move_to_end(cache_key)
                return cached[1]
            if flight.failed:
                raise ProviderKeyError("Speech synthesis failed for this recording.")
            if has_backup and _line_failures.get(cache_key, 0) > time.monotonic():
                _line_failures.move_to_end(cache_key)
                raise ProviderKeyError("Speech synthesis recently failed.")
            try:
                async with asyncio.timeout(_SYNTHESIS_TIMEOUT_SECONDS):
                    audio = (
                        await _synthesize_route(route, text)
                        if route
                        else await _synthesize(body, model, text)
                    )
            except _SYNTHESIS_ERRORS:
                flight.failed = True
                if has_backup:
                    failed_at = time.monotonic()
                    _line_failures[cache_key] = failed_at + _SYNTHESIS_RETRY_SECONDS
                    _line_failures.move_to_end(cache_key)
                    if len(_line_failures) > _LINE_CAP:
                        evicted = next(
                            (
                                key
                                for key, expires_at in _line_failures.items()
                                if expires_at <= failed_at
                            ),
                            next(iter(_line_failures)),
                        )
                        del _line_failures[evicted]
                raise
            if cache is _lines:
                _line_failures.pop(cache_key, None)
            cache[cache_key] = (time.monotonic() + 3600, audio)
            cache.move_to_end(cache_key)
            while len(cache) > cap:
                cache.popitem(last=False)
            return audio
    finally:
        flight.requests -= 1
        if flight.requests == 0:
            del _sample_locks[lock_key]


async def _synthesize(body: PreviewIn, model: str, text: str) -> bytes:
    route = resolve_provider_voice(
        body.provider, body.language, {"model": model}, voice_id=body.voice_id
    )
    return await _synthesize_route(route, text)


async def _synthesize_route(route: SpeechRoute, text: str) -> bytes:
    async with httpx.AsyncClient(timeout=_SYNTHESIS_TIMEOUT_SECONDS) as client:
        if route.provider == "cartesia":
            response = await client.post(
                "https://api.cartesia.ai/tts/bytes",
                headers={
                    "Authorization": f"Bearer {route.api_key}",
                    "Cartesia-Version": HEADERS_VERSION,
                },
                json={
                    "model_id": route.model,
                    "transcript": text,
                    "language": route.language or "en",
                    "voice": route.voice_id,
                    "output_format": {
                        "container": "wav",
                        "encoding": "pcm_s16le",
                        "sample_rate": 24000,
                    },
                },
            )
            response.raise_for_status()
            if not response.content:
                raise ValueError("No sample audio was returned.")
            return response.content
        if route.provider in TTS_PRESET_MODELS:
            pcm = await synthesize_preset(client, route.provider, route.voice_id, text)
        else:
            payload = {"model_id": route.model, "text": text}
            if route.language:
                payload["language_code"] = elevenlabs_language(route.language)
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{route.voice_id}",
                headers={"xi-api-key": route.api_key or ""},
                params={"output_format": "pcm_24000"},
                json=payload,
            )
            response.raise_for_status()
            pcm = response.content
        if len(pcm) < 2 or len(pcm) % 2:
            raise ValueError("No complete PCM audio was returned.")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(pcm)
        return output.getvalue()
