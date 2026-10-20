import hashlib
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastapi import FastAPI
from hailhq.api.routes.internal import voices
from hailhq.core.config import settings

from .test_internal_dsar import _signed, internal_secret_set  # noqa: F401

pytestmark = pytest.mark.usefixtures("internal_secret_set")


@pytest.fixture
def voice_app(monkeypatch):
    voices._samples.clear()
    monkeypatch.setattr(
        voices,
        "voice_catalogue",
        AsyncMock(
            return_value={
                "voices": [
                    {"id": "known", "provider": "cartesia", "model": "sonic-3.5"}
                ],
                "languages": [
                    {"code": "en", "name": "English"},
                    {"code": "fr", "name": "French"},
                ],
                "default_voice": None,
                "partial": False,
            }
        ),
    )
    app = FastAPI()
    app.include_router(voices.router)
    return app


async def test_catalogue_requires_signature_and_valid_language(voice_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        assert (await client.get("/internal/voices")).status_code == 401
        assert (
            await client.get("/internal/voices?language=invalid", headers=_signed(b""))
        ).status_code == 422
        response = await client.get(
            "/internal/voices?language=fr", headers=_signed(b"")
        )
        assert response.status_code == 200
        voices.voice_catalogue.assert_awaited_once_with("fr")
        assert response.json()["languages"][1]["code"] == "fr"


async def test_preview_is_fixed_text_cached_and_rejects_arbitrary_voices(
    voice_app, monkeypatch
):
    import json

    monkeypatch.setattr(settings, "cartesia_api_key", "test-key")
    monkeypatch.setattr(settings, "cartesia_model", "sonic-3.5")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        with respx.mock(assert_all_called=False) as mock:
            synthesis = mock.post("https://api.cartesia.ai/tts/bytes").mock(
                return_value=httpx.Response(200, content=b"RIFF-sample")
            )
            for _ in range(2):
                body = b'{"voice_id":"known","provider":"cartesia","language":"fr"}'
                response = await client.post(
                    "/internal/voices/preview", content=body, headers=_signed(body)
                )
                assert response.status_code == 200
                assert response.content == b"RIFF-sample"
            assert synthesis.call_count == 1
            payload = json.loads(synthesis.calls[0].request.content)
            assert payload["language"] == "fr"
            assert payload["transcript"] == voices._HELLO["fr"]
            body = b'{"voice_id":"arbitrary","provider":"cartesia","language":"fr"}'
            response = await client.post(
                "/internal/voices/preview", content=body, headers=_signed(body)
            )
            assert response.status_code == 422
            assert synthesis.call_count == 1


async def test_preview_provider_failure_returns_retryable_error(voice_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        with respx.mock:
            respx.post("https://api.cartesia.ai/tts/bytes").mock(
                return_value=httpx.Response(503)
            )
            body = b'{"voice_id":"known","provider":"cartesia","language":"en"}'
            response = await client.post(
                "/internal/voices/preview", content=body, headers=_signed(body)
            )
            assert response.status_code == 503


async def test_provider_identity_prevents_cross_provider_preview(voice_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        body = b'{"voice_id":"known","provider":"elevenlabs","language":"en"}'
        response = await client.post(
            "/internal/voices/preview", content=body, headers=_signed(body)
        )
        assert response.status_code == 422


async def test_elevenlabs_preview_uses_verified_model_and_wav_envelope(voice_app):
    import io
    import json
    import wave

    voices.voice_catalogue.return_value["voices"] = [
        {"id": "known", "provider": "elevenlabs", "model": "verified-model"}
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        with respx.mock:
            route = respx.post(
                "https://api.elevenlabs.io/v1/text-to-speech/known"
            ).mock(return_value=httpx.Response(200, content=b"\x01\x00" * 24))
            body = b'{"voice_id":"known","provider":"elevenlabs","language":"en"}'
            response = await client.post(
                "/internal/voices/preview", content=body, headers=_signed(body)
            )
            assert response.status_code == 200
            assert response.headers["content-type"] == "audio/wav"
            with wave.open(io.BytesIO(response.content)) as audio:
                assert audio.getframerate() == 24000
                assert audio.getnframes() == 24
            assert (
                json.loads(route.calls[0].request.content)["model_id"]
                == "verified-model"
            )
            voices.voice_catalogue.return_value["voices"][0]["model"] = "updated-model"
            await client.post(
                "/internal/voices/preview", content=body, headers=_signed(body)
            )
            assert route.call_count == 2


async def test_preview_rejects_path_like_ids(voice_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        body = b'{"voice_id":"../other","provider":"elevenlabs","language":"en"}'
        response = await client.post(
            "/internal/voices/preview", content=body, headers=_signed(body)
        )
        assert response.status_code == 422


async def test_preview_locks_serialize_one_sample_and_never_outlive_requests(
    monkeypatch,
):
    import asyncio

    voices._samples.clear()
    voices._sample_locks.clear()
    body = voices.PreviewIn(language="en", provider="cartesia", voice_id="known")
    release = asyncio.Event()
    running, peak, calls = 0, 0, 0

    async def slow_audio(body, model, text):
        nonlocal running, peak, calls
        calls += 1
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1
        return b"audio"

    monkeypatch.setattr(voices, "_synthesize", slow_audio)
    args = (body, "sonic-3.5", "hello", voices._samples, 10)
    first = asyncio.create_task(voices._audio(*args))
    second = asyncio.create_task(voices._audio(*args))
    await asyncio.sleep(0)
    digest = hashlib.sha256(b"hello").hexdigest()
    key = ("cartesia", "sonic-3.5", "en", "known", digest)
    assert voices._sample_locks[key][1] == 2
    release.set()
    assert await asyncio.gather(first, second) == [b"audio", b"audio"]
    assert peak == 1
    assert calls == 1
    assert voices._sample_locks == {}


async def test_failed_preview_synthesis_drops_its_lock(monkeypatch):
    voices._samples.clear()
    voices._sample_locks.clear()
    body = voices.PreviewIn(language="en", provider="cartesia", voice_id="known")
    monkeypatch.setattr(
        voices, "_synthesize", AsyncMock(side_effect=httpx.ConnectError("down"))
    )
    with pytest.raises(httpx.ConnectError):
        await voices._audio(body, "sonic-3.5", "hello", voices._samples, 10)
    assert voices._sample_locks == {}


def _line(text, voice_id="known", provider="cartesia", language="en"):
    return json.dumps(
        {
            "voice_id": voice_id,
            "provider": provider,
            "language": language,
            "text": text,
        }
    ).encode()


async def test_line_requires_signature(voice_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        response = await client.post("/internal/voices/line", content=_line("Hi"))
        assert response.status_code == 401


async def test_line_speaks_text_and_caches_per_text(voice_app, monkeypatch):
    voices._lines.clear()
    monkeypatch.setattr(settings, "cartesia_api_key", "test-key")
    first_line = "Hi, this is an AI assistant answering on behalf of Acme."
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        with respx.mock:
            synthesis = respx.post("https://api.cartesia.ai/tts/bytes").mock(
                return_value=httpx.Response(200, content=b"RIFF-line")
            )
            for _ in range(2):
                body = _line(f"  {first_line} ")
                response = await client.post(
                    "/internal/voices/line", content=body, headers=_signed(body)
                )
                assert response.status_code == 200
                assert response.headers["content-type"] == "audio/wav"
                assert response.content == b"RIFF-line"
            assert synthesis.call_count == 1
            payload = json.loads(synthesis.calls[0].request.content)
            assert payload["transcript"] == first_line
            assert payload["voice"] == "known"
            body = _line("Hi, this is Acme.")
            response = await client.post(
                "/internal/voices/line", content=body, headers=_signed(body)
            )
            assert response.status_code == 200
            assert synthesis.call_count == 2
            payload = json.loads(synthesis.calls[1].request.content)
            assert payload["transcript"] == "Hi, this is Acme."
    assert len(voices._lines) == 2
    assert voices._samples == {}


async def test_line_cache_stays_bounded(monkeypatch):
    voices._lines.clear()
    voices._sample_locks.clear()
    monkeypatch.setattr(voices, "_synthesize", AsyncMock(return_value=b"audio"))
    body = voices.PreviewIn(language="en", provider="cartesia", voice_id="known")
    for i in range(voices._LINE_CAP + 5):
        await voices._audio(
            body, "sonic-3.5", f"line {i}", voices._lines, voices._LINE_CAP
        )
    assert len(voices._lines) == voices._LINE_CAP


async def test_elevenlabs_line_sends_text(voice_app):
    voices._lines.clear()
    voices.voice_catalogue.return_value["voices"] = [
        {"id": "known", "provider": "elevenlabs", "model": "verified-model"}
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        with respx.mock:
            route = respx.post(
                "https://api.elevenlabs.io/v1/text-to-speech/known"
            ).mock(return_value=httpx.Response(200, content=b"\x01\x00" * 24))
            body = _line("Hi from Acme.", provider="elevenlabs")
            response = await client.post(
                "/internal/voices/line", content=body, headers=_signed(body)
            )
            assert response.status_code == 200
            assert response.content.startswith(b"RIFF")
            payload = json.loads(route.calls[0].request.content)
            assert payload["text"] == "Hi from Acme."
            assert payload["model_id"] == "verified-model"


async def test_line_rejects_non_catalogue_voice(voice_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        with respx.mock:
            synthesis = respx.post("https://api.cartesia.ai/tts/bytes")
            body = _line("Hi", voice_id="arbitrary")
            response = await client.post(
                "/internal/voices/line", content=body, headers=_signed(body)
            )
            assert response.status_code == 422
            assert response.json()["detail"] == "Choose one of the suggested voices."
            assert synthesis.call_count == 0


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("", 422),
        ("   ", 422),
        ("a" * 401, 422),
        ("Hi\nthere", 422),
        ("Hi\x00", 422),
        ("Hi\x1b[31m", 422),
        ("a" * 400, 200),
    ],
)
async def test_line_text_limits(voice_app, monkeypatch, text, status):
    voices._lines.clear()
    monkeypatch.setattr(voices, "_synthesize", AsyncMock(return_value=b"audio"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        body = _line(text)
        response = await client.post(
            "/internal/voices/line", content=body, headers=_signed(body)
        )
        assert response.status_code == status


async def test_line_provider_failure_returns_retryable_error(voice_app):
    voices._lines.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        with respx.mock:
            respx.post("https://api.cartesia.ai/tts/bytes").mock(
                return_value=httpx.Response(503)
            )
            body = _line("Hi from Acme.")
            response = await client.post(
                "/internal/voices/line", content=body, headers=_signed(body)
            )
            assert response.status_code == 503
            assert response.json()["detail"] == "This line could not play. Try again."
    assert voices._lines == {}
