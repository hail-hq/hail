"""Verify the real LiveKit Gemini plugin selects the expected Google endpoint."""

import base64
import json

import httpx
import pytest
from google.genai import types
from google.oauth2.credentials import Credentials
from hailhq.core.config import settings
from hailhq.voicebot.pipeline import ResolvedLayer, build_tts
from livekit.plugins.google.beta import gemini_tts


@pytest.mark.parametrize("mode", ["house-api", "workspace", "vertex"])
async def test_gemini_synthesizes_with_the_correct_credentials(monkeypatch, mode):
    monkeypatch.setattr(
        settings, "google_cloud_project", "" if mode == "house-api" else "hail-speech"
    )
    monkeypatch.setattr(settings, "google_cloud_location", "eu")
    monkeypatch.setattr(settings, "google_api_key", "house-key")
    monkeypatch.setattr(settings, "cartesia_api_key", "")
    monkeypatch.setattr(settings, "eleven_api_key", "")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    requests = []
    audio = base64.b64encode(b"\x01\x00" * 480).decode()
    event = {
        "candidates": [
            {
                "content": {
                    "parts": [{"inlineData": {"mimeType": "audio/pcm", "data": audio}}]
                }
            }
        ]
    }

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=f"data: {json.dumps(event)}\n\n")

    sdk_client = gemini_tts.Client
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:

        def make_client(**kwargs):
            if kwargs["vertexai"]:
                kwargs["credentials"] = Credentials(token="fixture-cloud-token")
            return sdk_client(
                **kwargs, http_options=types.HttpOptions(httpx_async_client=http)
            )

        monkeypatch.setattr(gemini_tts, "Client", make_client)
        org = (
            ResolvedLayer("gemini", "workspace-key", {}, False)
            if mode == "workspace"
            else None
        )
        renderer = build_tts(org, language="en")
        try:
            assert type(renderer) is gemini_tts.TTS
            async with renderer.synthesize("Good morning.") as stream:
                assert (
                    sum([chunk.frame.samples_per_channel async for chunk in stream])
                    == 480
                )
        finally:
            await renderer.aclose()

    (request,) = requests
    assert request.url.path.endswith(
        "/models/gemini-3.8-flash-tts:streamGenerateContent"
    )
    if mode == "vertex":
        assert request.url.host == "aiplatform.eu.rep.googleapis.com"
        assert "/projects/hail-speech/locations/eu/" in request.url.path
        assert request.headers["authorization"] == "Bearer fixture-cloud-token"
        assert "x-goog-api-key" not in request.headers
    else:
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == (
            "workspace-key" if org else "house-key"
        )


def test_old_workspace_vertex_settings_never_use_hail_credentials(monkeypatch):
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    org = ResolvedLayer("gemini", "workspace-key", {"project": "old-project"}, False)
    with pytest.raises(ValueError, match="Use a Gemini API key"):
        build_tts(org, language="en")
