"""Authenticated catalogue, fixed-text samples, and first-line playback.

Previews speak fixed text only: one greeting per language, for the current
three recommendations. They are shared in memory across orgs for one hour
(at most three per supported language).

The line route speaks only a workspace's own first line, at most 400
characters, in a catalogue voice. The website calls it after it checks the
session, org membership, and a per-user rate limit. Lines are cached in
memory for one hour, at most 256 at a time. No provider key reaches the
browser.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import time
import unicodedata
import wave
from collections import OrderedDict
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.core.config import settings
from hailhq.core.languages import SUPPORTED_LANGUAGES, Language, elevenlabs_language
from hailhq.core.voice_catalog import HEADERS_VERSION, voice_catalogue
from pydantic import BaseModel, Field, StringConstraints, field_validator

router = APIRouter(
    prefix="/internal/voices",
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)
# Key: provider, model, language, voice id, sha256 of the spoken text.
_Key = tuple[str, str, str, str, str]
_Cache = OrderedDict[_Key, tuple[float, bytes]]
_samples: _Cache = OrderedDict()
_lines: _Cache = OrderedDict()
_LINE_CAP = 256
# One lock per sample: concurrent first listens synthesize once, and a slow
# provider never blocks other samples or cache hits. A lock lives only while
# a request holds or waits on it, so the map holds in-flight samples only.
_sample_locks: dict[_Key, tuple[asyncio.Lock, int]] = {}
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
async def list_voices(language: Language = "en") -> dict:
    try:
        return await voice_catalogue(language)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            503, "Voice choices could not load. Try again or keep your current voice."
        ) from exc


class PreviewIn(BaseModel):
    language: Language
    provider: Literal["cartesia", "elevenlabs"]
    voice_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class LineIn(PreviewIn):
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)
    ]

    @field_validator("text")
    @classmethod
    def _no_control_characters(cls, text: str) -> str:
        if any(unicodedata.category(char) == "Cc" for char in text):
            raise ValueError("Use one line of text without control characters.")
        return text


async def _catalogue_model(body: PreviewIn) -> str:
    catalogue = await voice_catalogue(body.language)
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
        cap = 3 * len(SUPPORTED_LANGUAGES)
        return _wav(await _audio(body, model, text, _samples, cap))
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "This sample could not play. Try again.") from exc


@router.post("/line")
async def speak_line(body: LineIn) -> Response:
    try:
        model = await _catalogue_model(body)
        return _wav(await _audio(body, model, body.text, _lines, _LINE_CAP))
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "This line could not play. Try again.") from exc


async def _audio(
    body: PreviewIn, model: str, text: str, cache: _Cache, cap: int
) -> bytes:
    digest = hashlib.sha256(text.encode()).hexdigest()
    cache_key = (body.provider, model, body.language, body.voice_id, digest)
    lock, users = _sample_locks.get(cache_key, (asyncio.Lock(), 0))
    _sample_locks[cache_key] = (lock, users + 1)
    try:
        async with lock:
            cached = cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 3600:
                cache.move_to_end(cache_key)
                return cached[1]
            audio = await _synthesize(body, model, text)
            cache[cache_key] = (time.monotonic(), audio)
            cache.move_to_end(cache_key)
            while len(cache) > cap:
                cache.popitem(last=False)
            return audio
    finally:
        lock, users = _sample_locks[cache_key]
        if users == 1:
            del _sample_locks[cache_key]
        else:
            _sample_locks[cache_key] = (lock, users - 1)


async def _synthesize(body: PreviewIn, model: str, text: str) -> bytes:
    async with httpx.AsyncClient(timeout=20) as client:
        if body.provider == "cartesia":
            response = await client.post(
                "https://api.cartesia.ai/tts/bytes",
                headers={
                    "Authorization": f"Bearer {settings.cartesia_api_key}",
                    "Cartesia-Version": HEADERS_VERSION,
                },
                json={
                    "model_id": model,
                    "transcript": text,
                    "language": body.language,
                    "voice": body.voice_id,
                    "output_format": {
                        "container": "wav",
                        "encoding": "pcm_s16le",
                        "sample_rate": 24000,
                    },
                },
            )
            response.raise_for_status()
            return response.content
        response = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{body.voice_id}",
            headers={"xi-api-key": settings.eleven_api_key},
            params={"output_format": "pcm_24000"},
            json={
                "model_id": model,
                "text": text,
                "language_code": elevenlabs_language(body.language),
            },
        )
        response.raise_for_status()
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(response.content)
        return output.getvalue()
