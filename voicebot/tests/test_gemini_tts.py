"""Gemini auth and synthesis through the worker's supported clients."""

import asyncio
import base64
import json

import httpx
import pytest
from google.genai import types
from google.oauth2.credentials import Credentials
from hailhq.core.config import settings
from hailhq.core.providers.tts.gemini import GeminiSpeech
from hailhq.voicebot.gemini_tts import GeminiDirectTTS, build_gemini_tts
from livekit.agents import APIConnectionError, APIConnectOptions, APITimeoutError
from livekit.plugins.google.beta import gemini_tts


@pytest.mark.parametrize(
    "response_mode",
    ["complete", "no_audio", "partial_audio", "transport_error", "cancelled"],
)
async def test_native_gemini_synthesizes_with_the_configured_cloud_project(
    monkeypatch, response_mode
):
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    monkeypatch.setattr(settings, "google_tts_location", "eu")
    monkeypatch.setattr(settings, "google_service_account_json_b64", "")
    monkeypatch.setattr(settings, "google_application_credentials", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "fixture-developer-key")
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

    class AudioChunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(3 if response_mode == "complete" else 1):
                await asyncio.sleep(0.08)
                yield f"data: {json.dumps(event)}\n\n".encode()
            if response_mode == "partial_audio":
                await asyncio.Event().wait()
            if response_mode == "transport_error":
                await asyncio.sleep(0.03)
                raise httpx.ReadTimeout("Fixture transport stalled.")
            if response_mode == "cancelled":
                await asyncio.sleep(0.03)
                error = {"error": {"code": 499, "status": "CANCELLED"}}
                yield f"data: {json.dumps(error)}\n\n".encode()

    async def provider_response(request):
        requests.append(request)
        if response_mode == "no_audio":
            await asyncio.Event().wait()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=AudioChunks(),
        )

    sdk_client = gemini_tts.Client
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider_response)
    ) as http:

        def client_with_test_transport(**kwargs):
            assert kwargs["api_key"] is None
            return sdk_client(
                **kwargs,
                credentials=Credentials(token="fixture-cloud-token"),
                http_options=types.HttpOptions(httpx_async_client=http),
            )

        monkeypatch.setattr(gemini_tts, "Client", client_with_test_transport)
        renderer = build_gemini_tts(GeminiSpeech.from_config({}))
        try:
            assert isinstance(renderer, gemini_tts.TTS)
            options = APIConnectOptions(
                timeout=0.2,
                max_retry=int(response_mode != "no_audio"),
            )
            async with renderer.synthesize("Hello.", conn_options=options) as stream:
                async with asyncio.timeout(2):
                    if response_mode in (
                        "no_audio",
                        "partial_audio",
                        "transport_error",
                    ):
                        error = (
                            APIConnectionError
                            if response_mode == "transport_error"
                            else APITimeoutError
                        )
                        with pytest.raises(error) as exc:
                            [chunk.frame async for chunk in stream]
                        assert exc.value.retryable == (response_mode == "no_audio")
                    else:
                        frames = [chunk.frame async for chunk in stream]
                        expected_samples = 1440 if response_mode == "complete" else 480
                        assert (
                            sum(frame.samples_per_channel for frame in frames)
                            == expected_samples
                        )
        finally:
            await renderer.aclose()
    (request,) = requests
    assert request.url.host == "aiplatform.eu.rep.googleapis.com"
    assert request.headers["authorization"] == "Bearer fixture-cloud-token"
    assert "x-goog-api-key" not in request.headers
    assert request.url.path.endswith(
        "/projects/hail-speech/locations/eu/publishers/google/models/"
        "gemini-3.8-flash-tts:streamGenerateContent"
    )


async def test_inline_cloud_credentials_keep_the_existing_direct_path(monkeypatch):
    monkeypatch.setattr(settings, "google_cloud_project", "hail-speech")
    monkeypatch.setattr(
        settings, "google_service_account_json_b64", "fixture-encoded-credentials"
    )
    renderer = build_gemini_tts(GeminiSpeech.from_config({}))
    try:
        assert isinstance(renderer, GeminiDirectTTS)
        assert renderer.speech.api_key is None
        assert renderer.speech.project == "hail-speech"
    finally:
        await renderer.aclose()


@pytest.mark.parametrize("partial", [b"", b"\x00"])
async def test_pcm_stream_respects_first_audio_timeout(monkeypatch, partial):
    async def stalled_pcm(self, client, text):
        yield partial
        await asyncio.Event().wait()

    monkeypatch.setattr(GeminiSpeech, "pcm", stalled_pcm)
    renderer = GeminiDirectTTS(
        GeminiSpeech.from_config({"project": "fixture-project"}, "fixture-key")
    )
    try:
        options = APIConnectOptions(timeout=0.02, max_retry=0)
        async with renderer.synthesize("Hello.", conn_options=options) as stream:
            with pytest.raises(APITimeoutError):
                await asyncio.wait_for(anext(stream), timeout=1)
    finally:
        await renderer.aclose()
