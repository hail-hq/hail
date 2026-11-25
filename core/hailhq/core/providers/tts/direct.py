"""Stream mono 24 kHz PCM from the providers used by the English voice picker."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import lru_cache
from threading import Lock

import google.auth
import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from hailhq.core.config import settings

SAMPLE_RATE = 24000
CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
CLOUD_ENDPOINTS = {
    "global": "https://aiplatform.googleapis.com",
    "us": "https://aiplatform.us.rep.googleapis.com",
    "eu": "https://aiplatform.eu.rep.googleapis.com",
}
XAI_TTS_URL = "https://api.x.ai/v1/tts"
ASSEMBLYAI_TTS_URL = "https://tts.us.assemblyai.com/v1/openai/audio/speech"

# Ordered English defaults and suggestions from the audition.
DIRECT_VOICES = {
    "gemini": ("gemini-3.8-flash-tts", "Charon", "Charon", "male"),
    "xai": ("tts", "ara", "Ara", "female"),
    "assemblyai": ("assemblyai-tts-1", "aai-audrey", "Audrey", "female"),
}
_auth_lock = Lock()


def gemini_url(project: str, location: str) -> str:
    return (
        f"{CLOUD_ENDPOINTS[location]}/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{DIRECT_VOICES['gemini'][0]}:streamGenerateContent"
    )


@lru_cache(maxsize=1)
def _credentials():
    scopes = [CLOUD_SCOPE]
    if settings.google_service_account_json_b64:
        info = json.loads(base64.b64decode(settings.google_service_account_json_b64))
        return google.auth.load_credentials_from_dict(info, scopes=scopes)[0]
    if settings.google_application_credentials:
        return google.auth.load_credentials_from_file(
            settings.google_application_credentials, scopes=scopes
        )[0]
    return google.auth.default(scopes=scopes)[0]


def _google_headers(url: str) -> dict[str, str]:
    # Refresh once across concurrent syntheses in the same worker.
    headers: dict[str, str] = {}
    try:
        with _auth_lock:
            _credentials().before_request(Request(), "POST", url, headers)
    except (GoogleAuthError, ValueError, OSError) as exc:
        raise ValueError(
            "Google Cloud speech credentials could not be loaded."
        ) from exc
    return headers


def house_voice_ready(provider: str) -> bool:
    if provider == "gemini":
        return bool(settings.google_cloud_project)
    if provider == "xai":
        return bool(settings.xai_tts_api_key)
    if provider == "assemblyai":
        return bool(settings.assemblyai_tts_api_key)
    return False


@dataclass(frozen=True)
class DirectSpeech:
    provider: str
    model: str
    voice: str
    api_key: str | None = field(repr=False)
    project: str | None = None
    location: str = "global"

    @classmethod
    def resolve(
        cls,
        provider: str,
        params: dict | None = None,
        api_key: str | None = None,
        voice: str | None = None,
    ) -> DirectSpeech:
        params = params or {}
        model, default_voice, _, _ = DIRECT_VOICES[provider]
        voice = voice or params.get("voice_id") or default_voice
        if params.get("model") not in (None, model):
            raise ValueError(f"{provider} speech uses {model}.")
        if not api_key and not house_voice_ready(provider):
            raise ValueError(
                f"No workspace or Hail key is available for {provider} speech."
            )
        if provider == "gemini":
            if api_key and not params.get("project"):
                raise ValueError(
                    "Set your Google Cloud project for a Gemini speech key."
                )
            if not api_key and (params.get("project") or params.get("location")):
                raise ValueError(
                    "Supply your Google Cloud key to use your own project or location."
                )
            return cls(
                provider,
                model,
                voice,
                api_key,
                params.get("project") or settings.google_cloud_project,
                params.get("location")
                or ("global" if api_key else settings.google_tts_location),
            )
        house_key = (
            settings.xai_tts_api_key
            if provider == "xai"
            else settings.assemblyai_tts_api_key
        )
        return cls(provider, model, voice, api_key or house_key)

    async def pcm(self, client: httpx.AsyncClient, text: str) -> AsyncIterator[bytes]:
        if self.provider == "gemini":
            if not self.project:
                raise ValueError("Set a Google Cloud project for speech.")
            url = gemini_url(self.project, self.location)
            headers = (
                {"x-goog-api-key": self.api_key}
                if self.api_key
                else await asyncio.to_thread(_google_headers, url)
            )
            body = {
                "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"voice": self.voice}},
                },
            }
            async with client.stream(
                "POST", url, params={"alt": "sse"}, headers=headers, json=body
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:])
                    if "error" in event:
                        raise ValueError("Google Cloud returned a speech stream error.")
                    for candidate in event.get("candidates", []):
                        for part in candidate.get("content", {}).get("parts", []):
                            data = part.get("inlineData", {}).get("data")
                            if data:
                                yield base64.b64decode(data, validate=True)
            return

        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.provider == "xai":
            url = XAI_TTS_URL
            body = {
                "text": text,
                "voice_id": self.voice,
                "language": "en",
                "output_format": {"codec": "pcm", "sample_rate": SAMPLE_RATE},
            }
        else:
            url = ASSEMBLYAI_TTS_URL
            body = {
                "input": text,
                "voice": self.voice,
                "model": self.model,
                "language": "en",
                "response_format": "pcm",
                "sample_rate": SAMPLE_RATE,
            }
        async with client.stream("POST", url, headers=headers, json=body) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk
