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
from contextlib import aclosing
from typing import Annotated
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
from hailhq.core.provider_config import ResolvedLayer, load_org_provider_configs
from hailhq.core.providers.tts.direct import DIRECT_VOICES, SAMPLE_RATE
from hailhq.core.providers.tts.routing import (
    ProviderKeyError,
    SavedVoiceError,
    SpeechRoute,
    VoiceLanguageError,
    resolve_fallback_voice_provider,
    resolve_provider_voice,
    resolve_tts_routes,
)
from hailhq.core.secret_cipher import SecretKeyMissing
from hailhq.core.voice_catalog import HEADERS_VERSION, voice_catalogue
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
_SYNTHESIS_RETRY_SECONDS = 15
_CHOOSE_VOICE = "Choose an available voice in Call settings to preview this line."
_UNAVAILABLE_AGENT_VOICE = "Playback is not available for this agent's saved voice."
# One lock per recording in each cache. Locks only last while requests
# hold or wait on them.
_sample_locks: dict[tuple[int, _Key], tuple[asyncio.Lock, int]] = {}
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
        if any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in text):
            raise ValueError("Use one line of text without control characters.")
        return text


class LineIn(PreviewIn, LineText):
    pass


class WorkspaceLineIn(LineText):
    organization_id: UUID
    language: Language | None = None
    voice_id: str | None = None


async def _catalogue_model(body: PreviewIn) -> str:
    catalogue = await voice_catalogue(
        body.language, include_direct=body.provider in DIRECT_VOICES
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
        cap = 3 * len(SUPPORTED_LANGUAGES) + len(DIRECT_VOICES)
        return _wav(await _audio(body, model, text, _samples, cap))
    except (httpx.HTTPError, ValueError, ProviderKeyError) as exc:
        raise HTTPException(503, "This sample could not play. Try again.") from exc


@router.post("/line")
async def speak_line(body: LineIn) -> Response:
    try:
        model = await _catalogue_model(body)
        return _wav(await _audio(body, model, body.text, _lines, _LINE_CAP))
    except (httpx.HTTPError, ValueError, ProviderKeyError) as exc:
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
    fallback = await resolve_fallback_voice_provider(org, body.voice_id, language)
    try:
        routes = resolve_tts_routes(
            org, body.voice_id, language, fallback_voice_provider=fallback
        )
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
    if primary.uses_workspace_key:
        raise HTTPException(
            409, "Playback is not available for a voice on your own provider key."
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

    # Old agent overrides have no provider ID. Try the permitted house
    # routes for that exact voice ID without substituting a different voice.
    playable = [primary]
    if body.voice_id == preview.voice_id:
        playable.extend(
            route
            for route in routes[1:]
            if route.voice_id == preview.voice_id and not route.uses_workspace_key
        )

    playable = list(dict.fromkeys(playable))
    for index, route in enumerate(playable):
        try:
            audio = await _audio(
                preview,
                route.model,
                body.text,
                _lines,
                _LINE_CAP,
                route=route,
                has_backup=index < len(playable) - 1,
            )
        except (httpx.HTTPError, ValueError, ProviderKeyError):
            continue
        return _wav(audio)
    raise HTTPException(503, "This line could not play. Try again.")


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
    cache_key = (provider, model, language, body.voice_id, digest)
    lock_key = (id(cache), cache_key)
    lock, users = _sample_locks.get(lock_key, (asyncio.Lock(), 0))
    _sample_locks[lock_key] = (lock, users + 1)
    try:
        async with lock:
            cached = cache.get(cache_key)
            if cached and time.monotonic() < cached[0]:
                cache.move_to_end(cache_key)
                return cached[1]
            if has_backup and _line_failures.get(cache_key, 0) > time.monotonic():
                _line_failures.move_to_end(cache_key)
                raise ProviderKeyError("Speech synthesis recently failed.")
            try:
                audio = (
                    await _synthesize_route(route, text)
                    if route
                    else await _synthesize(body, model, text)
                )
            except (httpx.HTTPError, ValueError, ProviderKeyError):
                if has_backup:
                    _line_failures[cache_key] = (
                        time.monotonic() + _SYNTHESIS_RETRY_SECONDS
                    )
                    _line_failures.move_to_end(cache_key)
                    if len(_line_failures) > _LINE_CAP:
                        _line_failures.popitem(last=False)
                raise
            if cache is _lines:
                _line_failures.pop(cache_key, None)
            cache[cache_key] = (time.monotonic() + 3600, audio)
            cache.move_to_end(cache_key)
            while len(cache) > cap:
                cache.popitem(last=False)
            return audio
    finally:
        lock, users = _sample_locks[lock_key]
        if users == 1:
            del _sample_locks[lock_key]
        else:
            _sample_locks[lock_key] = (lock, users - 1)


async def _synthesize(body: PreviewIn, model: str, text: str) -> bytes:
    route = resolve_provider_voice(
        body.provider, body.language, {"model": model}, voice_id=body.voice_id
    )
    return await _synthesize_route(route, text)


async def _synthesize_route(route: SpeechRoute, text: str) -> bytes:
    async with httpx.AsyncClient(timeout=20) as client:
        if route.direct:
            speech = route.direct
            output = io.BytesIO()
            async with aclosing(speech.pcm(client, text)) as chunks:
                with wave.open(output, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(SAMPLE_RATE)
                    async for chunk in chunks:
                        wav.writeframesraw(chunk)
            if output.getbuffer().nbytes <= 44:
                raise ValueError("No sample audio was returned.")
            return output.getvalue()
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
            return response.content
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
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(response.content)
        return output.getvalue()
