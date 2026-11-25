import asyncio
import base64
import json

import httpx
import pytest
from hailhq.core.config import settings
from hailhq.core.provider_config import TTSParams
from hailhq.core.provider_validation import validate_provider_key
from hailhq.core.providers.tts import direct

PCM = b"\x01\x02\x03\x04"


async def test_gemini_stream_uses_customer_cloud_project():
    def handle(request):
        assert request.url.host == "aiplatform.eu.rep.googleapis.com"
        assert request.url.path == (
            "/v1/projects/customer-speech/locations/eu/publishers/google/models/"
            "gemini-3.8-flash-tts:streamGenerateContent"
        )
        assert request.url.params["alt"] == "sse"
        assert request.headers["x-goog-api-key"] == "workspace-key"
        config = json.loads(request.content)["generationConfig"]
        assert config["speechConfig"]["voiceConfig"] == {"voice": "Charon"}
        chunk = base64.b64encode(PCM).decode()
        event = {
            "candidates": [{"content": {"parts": [{"inlineData": {"data": chunk}}]}}]
        }
        return httpx.Response(200, content="data: " + json.dumps(event) + "\n\n")

    speech = direct.DirectSpeech.resolve(
        "gemini", {"project": "customer-speech", "location": "eu"}, "workspace-key"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert [audio async for audio in speech.pcm(client, "Hello.")] == [PCM]


def test_gemini_separates_customer_credentials_from_house_project(monkeypatch):
    monkeypatch.setattr(settings, "hail_gemini_tts_enabled", True)
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    monkeypatch.setattr(settings, "google_tts_location", "us")
    with pytest.raises(ValueError, match="Google Cloud project"):
        direct.DirectSpeech.resolve("gemini", api_key="workspace-key")
    with pytest.raises(ValueError, match="Supply your Google Cloud key"):
        direct.DirectSpeech.resolve("gemini", {"project": "customer-speech"})
    speech = direct.DirectSpeech.resolve("gemini")
    assert (speech.project, speech.location, speech.api_key) == (
        "hail-speech",
        "us",
        None,
    )
    with pytest.raises(ValueError):
        TTSParams(provider="gemini", project="../../bad")
    with pytest.raises(ValueError):
        TTSParams(provider="xai", project="customer-speech")


@pytest.mark.parametrize(
    ("provider", "url", "voice_field", "voice"),
    [
        ("xai", direct.XAI_TTS_URL, "voice_id", "ara"),
        ("assemblyai", direct.ASSEMBLYAI_TTS_URL, "voice", "aai-audrey"),
    ],
)
async def test_direct_key_validation_synthesizes_on_the_vendor(
    provider, url, voice_field, voice
):
    def handle(request):
        assert str(request.url) == url
        assert request.headers["authorization"] == "Bearer test-key"
        assert json.loads(request.content)[voice_field] == voice
        return httpx.Response(200, content=PCM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert await validate_provider_key("tts", provider, "test-key", {}, client) == (
            "valid",
            "ok",
        )


async def test_house_gemini_bounds_the_wait_for_credentials(monkeypatch):
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    monkeypatch.setattr(direct, "_GOOGLE_AUTH_TIMEOUT", 0.01)

    async def stalled(_function, _url):
        await asyncio.Event().wait()

    monkeypatch.setattr(direct.asyncio, "to_thread", stalled)
    speech = direct.DirectSpeech.resolve("gemini")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected synthesis"))
    ) as client:
        with pytest.raises(httpx.ReadTimeout, match="credentials timed out"):
            await anext(speech.pcm(client, "Hello."))


async def test_blank_customer_key_never_falls_back_to_house_key(monkeypatch):
    monkeypatch.setattr(settings, "xai_tts_api_key", "test-house-key")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected synthesis"))
    ) as client:
        assert await validate_provider_key("tts", "xai", " ", {}, client) == (
            "invalid",
            "Enter an API key.",
        )
