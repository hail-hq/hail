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
from hailhq.core.providers.tts.gemini_direct import GeminiDirectTTS
from hailhq.voicebot.pipeline import ResolvedLayer, build_tts
from livekit.agents import APIConnectionError, APIConnectOptions, APITimeoutError
from livekit.plugins.google.beta import gemini_tts


@pytest.mark.parametrize("workspace_key", [None, "workspace-key"])
async def test_gemini_developer_key_uses_the_livekit_plugin(monkeypatch, workspace_key):
    monkeypatch.setattr(
        settings, "google_cloud_project", "hail-speech" if workspace_key else ""
    )
    monkeypatch.setattr(settings, "google_api_key", "configured-house-key")
    monkeypatch.setattr(settings, "hail_gemini_tts_enabled", True)
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
        monkeypatch.setattr(
            gemini_tts,
            "Client",
            lambda **kwargs: sdk_client(
                **kwargs, http_options=types.HttpOptions(httpx_async_client=http)
            ),
        )
        org = (
            ResolvedLayer("gemini", workspace_key, {}, False) if workspace_key else None
        )
        renderer = build_tts(org, language="en")
        try:
            assert isinstance(renderer, gemini_tts.TTS)
            async with renderer.synthesize("Good morning.") as stream:
                assert (
                    sum([chunk.frame.samples_per_channel async for chunk in stream])
                    == 480
                )
        finally:
            await renderer.aclose()
    (request,) = requests
    assert request.url.host == "generativelanguage.googleapis.com"
    assert request.url.path.endswith(
        "/models/gemini-3.8-flash-tts:streamGenerateContent"
    )
    assert request.headers["x-goog-api-key"] == (
        workspace_key or "configured-house-key"
    )


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
        renderer = GeminiSpeech.from_config({}).livekit_tts()
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
    renderer = GeminiSpeech.from_config({}).livekit_tts()
    try:
        assert isinstance(renderer, GeminiDirectTTS)
        assert renderer.speech.api_key is None
        assert renderer.speech.project == "hail-speech"
    finally:
        await renderer.aclose()


@pytest.mark.parametrize(
    ("project", "override"),
    [("", "GOOGLE_GEMINI_BASE_URL"), ("hail-speech", "GOOGLE_VERTEX_BASE_URL")],
)
async def test_gemini_does_not_use_an_sdk_proxy_for_call_audio(
    monkeypatch, project, override
):
    monkeypatch.setattr(settings, "google_cloud_project", project)
    monkeypatch.setattr(settings, "google_api_key", "fixture-key")
    monkeypatch.setattr(settings, "google_application_credentials", "")
    monkeypatch.setattr(settings, "google_service_account_json_b64", "")
    monkeypatch.setenv(override, "https://other.example.test")
    renderer = GeminiSpeech.from_config({}).livekit_tts()
    try:
        assert isinstance(renderer, GeminiDirectTTS)
    finally:
        await renderer.aclose()


@pytest.mark.parametrize(
    "partial", [b"", b"\x00", b"\x01\x00" * 480], ids=["none", "one-byte", "one-frame"]
)
async def test_pcm_stream_times_out_when_audio_stalls(monkeypatch, partial):
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
            with pytest.raises(APITimeoutError) as error:
                async with asyncio.timeout(1):
                    [chunk.frame async for chunk in stream]
            assert error.value.retryable == (len(partial) < 960)
    finally:
        await renderer.aclose()


async def test_direct_gemini_preserves_provider_cancellation_after_audio(monkeypatch):
    speech = GeminiSpeech.from_config({"project": "hail-speech"}, "fixture-key")
    renderer = speech.livekit_tts()
    audio_sent = asyncio.Event()
    audio = base64.b64encode(b"\x01\x00" * 480).decode()
    event = {"candidates": [{"content": {"parts": [{"inlineData": {"data": audio}}]}}]}
    cancelled = {"error": {"code": 499, "status": "CANCELLED"}}
    requests = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield f"data: {json.dumps(event)}\n\n".encode()
            await audio_sent.wait()
            yield f"data: {json.dumps(cancelled)}\n\n".encode()

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=Body())

    original = GeminiSpeech.pcm
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(
            GeminiSpeech, "pcm", lambda self, _, text: original(self, http, text)
        )
        try:
            options = APIConnectOptions(max_retry=1, timeout=0.3)
            async with renderer.synthesize("Hello.", conn_options=options) as stream:
                async with asyncio.timeout(2):
                    async for _ in stream:
                        audio_sent.set()
        finally:
            await renderer.aclose()
    assert audio_sent.is_set()
    assert len(requests) == 1
