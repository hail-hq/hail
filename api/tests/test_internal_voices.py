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
        voices, "recommended_voices", AsyncMock(return_value=[{"id": "known"}])
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
        voices.recommended_voices.assert_awaited_once_with("fr")


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
                body = b'{"voice_id":"known","language":"fr"}'
                response = await client.post(
                    "/internal/voices/preview", content=body, headers=_signed(body)
                )
                assert response.status_code == 200
                assert response.content == b"RIFF-sample"
            assert synthesis.call_count == 1
            payload = json.loads(synthesis.calls[0].request.content)
            assert payload["language"] == "fr"
            assert payload["transcript"] == voices._HELLO["fr"]
            body = b'{"voice_id":"arbitrary","language":"fr"}'
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
            body = b'{"voice_id":"known","language":"en"}'
            response = await client.post(
                "/internal/voices/preview", content=body, headers=_signed(body)
            )
            assert response.status_code == 503
