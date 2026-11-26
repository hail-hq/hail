import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
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
        voices.voice_catalogue.assert_awaited_once_with("fr", include_direct=False)
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


async def test_preview_uses_xai_for_an_advertised_voice(voice_app, monkeypatch):
    monkeypatch.setattr(settings, "xai_api_key", "house-key")
    voices.voice_catalogue.return_value["voices"] = [
        {"id": "ara", "provider": "xai", "model": "tts"}
    ]
    body = b'{"voice_id":"ara","provider":"xai","language":"en"}'

    with respx.mock as mock:
        synthesis = mock.post("https://api.x.ai/v1/tts").respond(
            200, content=b"\x00\x01"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/internal/voices/preview", content=body, headers=_signed(body)
            )
        assert response.status_code == 200
        assert response.content.startswith(b"RIFF")
        assert synthesis.calls[0].request.headers["authorization"] == "Bearer house-key"
    voices.voice_catalogue.assert_awaited_once_with("en", include_direct=True)


async def test_legacy_line_still_accepts_unicode_paragraph_separator(
    voice_app, monkeypatch
):
    monkeypatch.setattr(settings, "cartesia_api_key", "house-key")
    body = b'{"voice_id":"known","provider":"cartesia","language":"en","text":"Hello.\\u2029Goodbye."}'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        with respx.mock as mock:
            synthesis = mock.post("https://api.cartesia.ai/tts/bytes").respond(
                200, content=b"RIFF-legacy"
            )
            response = await client.post(
                "/internal/voices/line", content=body, headers=_signed(body)
            )
            assert response.status_code == 200
            assert synthesis.call_count == 1
            payload = json.loads(synthesis.calls[0].request.content)
            assert payload["transcript"] == "Hello.\u2029Goodbye."


async def test_line_keeps_the_existing_provider_scope(voice_app):
    body = b'{"voice_id":"ara","provider":"xai","language":"en","text":"Hello."}'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=voice_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/voices/line", content=body, headers=_signed(body)
        )
    assert response.status_code == 422
    voices.voice_catalogue.assert_not_awaited()


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


async def test_elevenlabs_preview_uses_verified_model_and_wav_envelope(
    voice_app, monkeypatch
):
    monkeypatch.setattr(settings, "eleven_api_key", "house-eleven")
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


@pytest.mark.parametrize("fails", [False, True])
async def test_preview_locks_serialize_one_sample_and_never_outlive_requests(
    monkeypatch,
    fails,
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
        if fails:
            raise httpx.ReadTimeout("fixture provider failure")
        return b"audio"

    monkeypatch.setattr(voices, "_synthesize", slow_audio)
    args = (body, "sonic-3.5", "hello", voices._samples, 10)
    first = asyncio.create_task(voices._audio(*args))
    second = asyncio.create_task(voices._audio(*args))
    await asyncio.sleep(0)
    digest = hashlib.sha256(b"hello").hexdigest()
    key = ("cartesia", "sonic-3.5", "en", "known", digest)
    assert voices._sample_locks[id(voices._samples), key].requests == 2
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    if fails:
        assert isinstance(results[0], httpx.ReadTimeout)
        assert isinstance(results[1], voices.ProviderKeyError)
    else:
        assert results == [b"audio", b"audio"]
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
    voices._line_failures.clear()
    voices._sample_locks.clear()
    monkeypatch.setattr(voices, "_synthesize", AsyncMock(return_value=b"audio"))
    body = voices.PreviewIn(language="en", provider="cartesia", voice_id="known")
    for i in range(voices._LINE_CAP + 5):
        await voices._audio(
            body, "sonic-3.5", f"line {i}", voices._lines, voices._LINE_CAP
        )
    assert len(voices._lines) == voices._LINE_CAP
    saved_recordings = tuple(voices._lines)
    voices._synthesize.side_effect = httpx.ReadTimeout("provider unavailable")
    with pytest.raises(httpx.ReadTimeout):
        await voices._audio(
            body,
            "sonic-3.5",
            "new line",
            voices._lines,
            voices._LINE_CAP,
            has_backup=True,
        )
    assert tuple(voices._lines) == saved_recordings
    assert len(voices._line_failures) == 1


async def test_elevenlabs_line_sends_text(voice_app, monkeypatch):
    monkeypatch.setattr(settings, "eleven_api_key", "house-eleven")
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


@pytest.fixture
def workspace_voice_app(monkeypatch):
    app = FastAPI()
    app.include_router(voices.router)
    app.state.session_open = False

    @asynccontextmanager
    async def session():
        app.state.session_open = True
        try:
            yield None
        finally:
            app.state.session_open = False

    monkeypatch.setattr(voices, "session_scope", session)
    monkeypatch.setattr(voices, "load_org_provider_configs", AsyncMock(return_value={}))
    voices._lines.clear()
    voices._line_failures.clear()
    return app


async def test_workspace_line_uses_saved_voice_and_model_outside_shortlist(
    workspace_voice_app, monkeypatch
):
    monkeypatch.setattr(settings, "cartesia_api_key", "house-key")
    monkeypatch.setattr(
        voices,
        "load_org_provider_configs",
        AsyncMock(
            return_value={
                "tts": SimpleNamespace(
                    provider="cartesia",
                    encrypted_api_key=None,
                    params={
                        "language": "fr",
                        "voice_id": "saved",
                        "model": "saved-model",
                    },
                    fallback_enabled=False,
                )
            }
        ),
    )
    body = (
        b'{"organization_id":"00000000-0000-0000-0000-000000000001","text":"Bonjour."}'
    )

    def synthesize(_request):
        assert not workspace_voice_app.state.session_open
        return httpx.Response(200, content=b"RIFF-sample")

    with respx.mock as mock:
        synthesis = mock.post("https://api.cartesia.ai/tts/bytes").mock(
            side_effect=synthesize
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=workspace_voice_app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/internal/voices/workspace-line", content=body, headers=_signed(body)
            )

        assert response.status_code == 200, response.text
        payload = json.loads(synthesis.calls[0].request.content)
        assert payload["voice"] == "saved"
        assert payload["model_id"] == "saved-model"
        assert payload["language"] == "fr"


async def test_workspace_line_can_play_saved_voice_on_legacy_fallback(
    workspace_voice_app, monkeypatch
):
    monkeypatch.setattr(settings, "cartesia_api_key", "house-cartesia")
    monkeypatch.setattr(settings, "eleven_api_key", "house-eleven")
    current_time = 100.0
    monkeypatch.setattr(voices, "time", SimpleNamespace(monotonic=lambda: current_time))
    voices.load_org_provider_configs.return_value = {
        "tts": SimpleNamespace(
            provider="cartesia",
            encrypted_api_key=None,
            params={},
            fallback_enabled=True,
        )
    }
    body = b'{"organization_id":"00000000-0000-0000-0000-000000000001","text":"Hi.","voice_id":"saved-eleven"}'

    with respx.mock as mock:
        first = mock.post("https://api.cartesia.ai/tts/bytes").respond(404)
        backup = mock.post(
            "https://api.elevenlabs.io/v1/text-to-speech/saved-eleven"
        ).respond(200, content=b"\x00\x00")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=workspace_voice_app),
            base_url="http://test",
        ) as client:
            for _ in range(2):
                response = await client.post(
                    "/internal/voices/workspace-line",
                    content=body,
                    headers=_signed(body),
                )
                assert response.status_code == 200
                assert response.content.startswith(b"RIFF")
            assert first.call_count == 1
            assert backup.call_count == 1

            current_time += 30
            first.respond(200, content=b"RIFF-primary")
            response = await client.post(
                "/internal/voices/workspace-line", content=body, headers=_signed(body)
            )
            assert response.content == b"RIFF-primary"
            assert first.call_count == 2

            voices.load_org_provider_configs.return_value["tts"].fallback_enabled = (
                False
            )
            first.respond(503)
            response = await client.post(
                "/internal/voices/workspace-line", content=body, headers=_signed(body)
            )
            assert response.content == b"RIFF-primary"
            assert first.call_count == 2
            assert backup.call_count == 1


async def test_inherited_line_uses_fallback_voice_only_with_consent(
    workspace_voice_app, monkeypatch
):
    monkeypatch.setattr(settings, "cartesia_api_key", "house-cartesia")
    monkeypatch.setattr(settings, "cartesia_voice_id", "cartesia-default")
    monkeypatch.setattr(settings, "eleven_api_key", "house-eleven")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "eleven-default")
    org = SimpleNamespace(
        provider="cartesia",
        encrypted_api_key=None,
        params={"voice_id": "cartesia-default"},
        fallback_enabled=True,
    )
    voices.load_org_provider_configs.return_value = {"tts": org}
    body = b'{"organization_id":"00000000-0000-0000-0000-000000000001","text":"Hello."}'

    with respx.mock as mock:
        primary = mock.post("https://api.cartesia.ai/tts/bytes").respond(503)
        backup = mock.post(
            "https://api.elevenlabs.io/v1/text-to-speech/eleven-default"
        ).respond(200, content=b"\x00\x00")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=workspace_voice_app),
            base_url="http://test",
        ) as client:
            opted_in = await client.post(
                "/internal/voices/workspace-line", content=body, headers=_signed(body)
            )
            org.fallback_enabled = False
            strict = await client.post(
                "/internal/voices/workspace-line", content=body, headers=_signed(body)
            )

    assert opted_in.status_code == 200
    assert strict.status_code == 503
    assert primary.call_count == 2
    assert backup.call_count == 1
    assert {key[3] for key in voices._lines} == {"eleven-default"}


async def test_workspace_line_rejects_an_override_the_call_would_skip(
    workspace_voice_app, monkeypatch
):
    monkeypatch.setattr(settings, "xai_api_key", "house-xai")
    monkeypatch.setattr(settings, "cartesia_api_key", "house-cartesia")
    monkeypatch.setattr(settings, "cartesia_voice_id", "former-voice")
    voices.load_org_provider_configs.return_value = {
        "tts": SimpleNamespace(
            provider="xai", encrypted_api_key=None, params={}, fallback_enabled=True
        )
    }
    body = b'{"organization_id":"00000000-0000-0000-0000-000000000001","language":"en","voice_id":"former-voice","text":"Hi."}'
    synthesize = AsyncMock(return_value=b"RIFF-unused")
    monkeypatch.setattr(voices, "_synthesize_route", synthesize)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=workspace_voice_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/voices/workspace-line", content=body, headers=_signed(body)
        )
    assert response.status_code == 409
    synthesize.assert_not_awaited()


async def test_empty_elevenlabs_audio_does_not_block_playback_fallback(
    workspace_voice_app, monkeypatch
):
    monkeypatch.setattr(settings, "eleven_api_key", "house-eleven")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "eleven-default")
    monkeypatch.setattr(settings, "cartesia_api_key", "house-cartesia")
    voices.load_org_provider_configs.return_value = {
        "tts": SimpleNamespace(
            provider="elevenlabs",
            encrypted_api_key=None,
            params={"voice_id": "eleven-default"},
            fallback_enabled=True,
        )
    }
    body = b'{"organization_id":"00000000-0000-0000-0000-000000000001","text":"Hi."}'

    with respx.mock as mock:
        primary = mock.post(
            "https://api.elevenlabs.io/v1/text-to-speech/eleven-default"
        ).respond(200, content=b"")
        backup = mock.post("https://api.cartesia.ai/tts/bytes").respond(
            200, content=b"RIFF-backup"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=workspace_voice_app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/internal/voices/workspace-line", content=body, headers=_signed(body)
            )
        assert response.content == b"RIFF-backup"
        assert primary.call_count == backup.call_count == 1
    assert {key[0] for key in voices._lines} == {"cartesia"}


async def test_workspace_line_stops_a_stalled_provider_and_uses_the_backup(
    workspace_voice_app, monkeypatch
):
    import asyncio

    monkeypatch.setattr(settings, "cartesia_api_key", "house-cartesia")
    monkeypatch.setattr(settings, "eleven_api_key", "house-eleven")
    monkeypatch.setattr(voices, "_SYNTHESIS_TIMEOUT_SECONDS", 0.02, raising=False)
    attempted = []

    async def synthesize(route, text):
        attempted.append(route.provider)
        if route.provider == "cartesia":
            await asyncio.Future()
        return b"RIFF-backup"

    monkeypatch.setattr(voices, "_synthesize_route", synthesize)
    body = b'{"organization_id":"00000000-0000-0000-0000-000000000001","text":"Hi."}'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=workspace_voice_app), base_url="http://test"
    ) as client:
        async with asyncio.timeout(1):
            response = await client.post(
                "/internal/voices/workspace-line", content=body, headers=_signed(body)
            )
    assert response.status_code == 200
    assert response.content == b"RIFF-backup"
    assert attempted == ["cartesia", "elevenlabs"]


async def test_workspace_line_explains_when_saved_provider_is_unavailable(
    workspace_voice_app, monkeypatch
):
    monkeypatch.setattr(settings, "xai_api_key", "")
    voices.load_org_provider_configs.return_value = {
        "tts": SimpleNamespace(
            provider="xai",
            encrypted_api_key=None,
            params={},
            fallback_enabled=False,
        )
    }
    body = b'{"organization_id":"00000000-0000-0000-0000-000000000001","text":"Hi."}'

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=workspace_voice_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/voices/workspace-line", content=body, headers=_signed(body)
        )
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Choose an available voice in Call settings to preview this line."
    )


async def test_concurrent_workspace_lines_share_terminal_fallback_failure(
    workspace_voice_app, monkeypatch
):
    import asyncio

    monkeypatch.setattr(settings, "cartesia_api_key", "house-cartesia")
    monkeypatch.setattr(settings, "eleven_api_key", "house-eleven")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def unavailable(route, text):
        calls.append(route.provider)
        if route.provider == "cartesia":
            started.set()
            await release.wait()
        raise httpx.ReadTimeout("fixture provider failure")

    monkeypatch.setattr(voices, "_synthesize_route", unavailable)
    body = b'{"organization_id":"00000000-0000-0000-0000-000000000001","text":"Hi.","voice_id":"saved-eleven"}'

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=workspace_voice_app), base_url="http://test"
    ) as client:

        async def play():
            return await client.post(
                "/internal/voices/workspace-line", content=body, headers=_signed(body)
            )

        first = asyncio.create_task(play())
        await asyncio.wait_for(started.wait(), timeout=1)
        second = asyncio.create_task(play())
        await asyncio.sleep(0)
        release.set()
        responses = await asyncio.gather(first, second)

    assert [response.status_code for response in responses] == [503, 503]
    assert calls == ["cartesia", "elevenlabs"]
    assert voices._playback_flights == {}


async def test_cancelled_workspace_line_releases_synthesis_for_a_retry(
    workspace_voice_app, monkeypatch
):
    import asyncio

    monkeypatch.setattr(settings, "cartesia_api_key", "house-cartesia")
    started = asyncio.Event()
    released = asyncio.Event()

    async def interrupted(route, text):
        if started.is_set():
            return b"RIFF-retry"
        started.set()
        try:
            await asyncio.Future()
        finally:
            released.set()

    monkeypatch.setattr(voices, "_synthesize_route", interrupted)
    body = (
        b'{"organization_id":"00000000-0000-0000-0000-000000000001","text":"Retry me."}'
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=workspace_voice_app), base_url="http://test"
    ) as client:

        async def play():
            return await client.post(
                "/internal/voices/workspace-line", content=body, headers=_signed(body)
            )

        first = asyncio.create_task(play())
        await asyncio.wait_for(started.wait(), timeout=1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(released.wait(), timeout=1)
        assert voices._playback_flights == {}

        response = await asyncio.wait_for(play(), timeout=1)

    assert response.status_code == 200
    assert response.content == b"RIFF-retry"


async def test_workspace_line_keeps_elevenlabs_language_automatic_when_unset(
    workspace_voice_app, monkeypatch
):
    monkeypatch.setattr(settings, "cartesia_api_key", "")
    monkeypatch.setattr(settings, "google_cloud_project", "")
    monkeypatch.setattr(settings, "google_api_key", "")
    monkeypatch.setattr(settings, "eleven_api_key", "house-eleven")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "house-eleven-voice")
    data = {
        "organization_id": "00000000-0000-0000-0000-000000000001",
        "text": "Buenos días.",
    }

    with respx.mock as mock:
        synthesis = mock.post(
            "https://api.elevenlabs.io/v1/text-to-speech/house-eleven-voice"
        ).respond(200, content=b"\x00\x00")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=workspace_voice_app),
            base_url="http://test",
        ) as client:
            for payload in (data, {**data, "language": "en"}):
                body = json.dumps(payload).encode()
                response = await client.post(
                    "/internal/voices/workspace-line",
                    content=body,
                    headers=_signed(body),
                )
                assert response.status_code == 200
        assert synthesis.call_count == 2
        assert "language_code" not in json.loads(synthesis.calls[0].request.content)
        assert json.loads(synthesis.calls[1].request.content)["language_code"] == "en"
