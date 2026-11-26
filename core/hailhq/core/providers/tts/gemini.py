"""Gemini speech through the Gemini API or Vertex AI."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from functools import lru_cache, partial
from threading import Lock
from typing import TYPE_CHECKING, Self

import google.auth
import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from hailhq.core.config import settings
from hailhq.core.providers.tts.base import PCMAdapter

if TYPE_CHECKING:
    from livekit.agents.tts import TTS

CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
GEMINI_API_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"
_GOOGLE_AUTH_TIMEOUT = 4.0
CLOUD_ENDPOINTS = {
    "global": "https://aiplatform.googleapis.com",
    "us": "https://aiplatform.us.rep.googleapis.com",
    "eu": "https://aiplatform.eu.rep.googleapis.com",
}
_auth_lock = Lock()


@lru_cache(maxsize=1)
def _credentials():
    scopes = [CLOUD_SCOPE]
    try:
        if settings.google_service_account_json_b64:
            info = json.loads(
                base64.b64decode(settings.google_service_account_json_b64)
            )
            return google.auth.load_credentials_from_dict(info, scopes=scopes)[0]
        if settings.google_application_credentials:
            return google.auth.load_credentials_from_file(
                settings.google_application_credentials, scopes=scopes
            )[0]
        return google.auth.default(scopes=scopes)[0]
    except AttributeError as exc:
        raise ValueError("Google Cloud credentials must be a JSON object.") from exc


def _google_headers(url: str) -> dict[str, str]:
    # Refresh once across concurrent syntheses in the same worker.
    headers: dict[str, str] = {}
    if not _auth_lock.acquire(timeout=_GOOGLE_AUTH_TIMEOUT):
        raise httpx.ReadTimeout("Google Cloud speech credentials are busy.")
    try:
        request = partial(Request(), timeout=_GOOGLE_AUTH_TIMEOUT)
        _credentials().before_request(request, "POST", url, headers)
    except (GoogleAuthError, ValueError, OSError) as exc:
        raise ValueError(
            "Google Cloud speech credentials could not be loaded."
        ) from exc
    finally:
        _auth_lock.release()
    return headers


@dataclass(frozen=True)
class GeminiSpeech(PCMAdapter):
    provider = "gemini"
    model = "gemini-3.8-flash-tts"
    default_voice = "Charon"

    project: str = ""
    location: str = "global"

    def livekit_tts(self) -> TTS:
        from hailhq.core.providers.tts.livekit_deadline import GeminiTTS

        if self.project:
            # LiveKit only supports process ADC in Vertex mode.
            credentials_file = settings.google_application_credentials
            if (
                self.api_key
                or settings.google_service_account_json_b64
                or credentials_file
                and credentials_file != os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            ):
                from hailhq.core.providers.tts.gemini_direct import GeminiDirectTTS

                return GeminiDirectTTS(self)
            auth = {
                "vertexai": True,
                "project": self.project,
                "location": self.location,
            }
        else:
            auth = {"vertexai": False, "api_key": self.api_key}
        try:
            return GeminiTTS(model=self.model, voice_name=self.voice, **auth)
        except GoogleAuthError as exc:
            raise ValueError(
                "Google Cloud speech credentials could not be loaded."
            ) from exc

    @classmethod
    def house_key(cls) -> str | None:
        return (
            None if settings.google_cloud_project else settings.google_api_key or None
        )

    @classmethod
    def house_available(cls) -> bool:
        return bool(settings.google_cloud_project or cls.house_key())

    @classmethod
    async def house_credentials_available(cls) -> bool:
        if not settings.google_cloud_project:
            return bool(cls.house_key())
        try:
            async with asyncio.timeout(_GOOGLE_AUTH_TIMEOUT):
                await asyncio.to_thread(_credentials)
        except (GoogleAuthError, OSError, ValueError, TimeoutError):
            return False
        return True

    @classmethod
    def validate_options(cls, params: dict, *, has_workspace_key: bool) -> None:
        super().validate_options(params, has_workspace_key=has_workspace_key)
        if has_workspace_key and params.get("location") and not params.get("project"):
            raise ValueError("Set a Google Cloud project to use a location.")
        if not has_workspace_key and (params.get("project") or params.get("location")):
            raise ValueError(
                "Supply your Google Cloud key to use your own project or location."
            )

    @classmethod
    def from_config(
        cls, params: dict, api_key: str | None = None, voice: str | None = None
    ) -> Self:
        speech = super().from_config(params, api_key, voice)
        if api_key is not None:
            return replace(
                speech,
                project=params.get("project") or "",
                location=params.get("location") or "global",
            )
        return replace(
            speech,
            project=settings.google_cloud_project,
            location=settings.google_tts_location,
        )

    async def pcm(self, client: httpx.AsyncClient, text: str) -> AsyncIterator[bytes]:
        root = (
            f"{CLOUD_ENDPOINTS[self.location]}/v1/projects/{self.project}"
            f"/locations/{self.location}/publishers/google"
            if self.project
            else GEMINI_API_ENDPOINT
        )
        url = f"{root}/models/{self.model}:streamGenerateContent"
        if self.api_key:
            headers = {"x-goog-api-key": self.api_key}
        else:
            try:
                async with asyncio.timeout(_GOOGLE_AUTH_TIMEOUT):
                    headers = await asyncio.to_thread(_google_headers, url)
            except TimeoutError:
                raise httpx.ReadTimeout(
                    "Google Cloud speech credentials timed out."
                ) from None
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
