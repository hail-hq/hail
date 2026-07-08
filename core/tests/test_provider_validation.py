"""validate_provider_key: cheap authenticated GET per provider, mocked transport."""

from __future__ import annotations

import httpx
import pytest

from hailhq.core.provider_validation import validate_provider_key

# (provider, layer, params, expected URL substring, expected auth header, header value prefix)
CASES = [
    (
        "openai-compatible",
        "llm",
        {"base_url": "https://api.openai.com/v1"},
        "https://api.openai.com/v1/models",
        "authorization",
        "Bearer ",
    ),
    ("anthropic", "llm", {}, "https://api.anthropic.com/v1/models", "x-api-key", ""),
    ("google", "llm", {}, "generativelanguage.googleapis.com", None, ""),
    ("cartesia", "tts", {}, "https://api.cartesia.ai/voices", "x-api-key", ""),
    ("elevenlabs", "tts", {}, "https://api.elevenlabs.io/v1/user", "xi-api-key", ""),
    (
        "deepgram",
        "stt",
        {},
        "https://api.deepgram.com/v1/projects",
        "authorization",
        "Token ",
    ),
]


@pytest.mark.parametrize("provider,layer,params,url_part,auth_header,prefix", CASES)
async def test_valid_key_returns_ok(
    provider, layer, params, url_part, auth_header, prefix
) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok, message = await validate_provider_key(
        layer, provider, "sk-test-KEY1", params, client=client
    )
    assert ok, message
    assert url_part in seen["url"]
    if auth_header:
        assert seen["headers"][auth_header].startswith(prefix)
        assert "KEY1" in seen["headers"][auth_header]
    else:  # google puts the key in the query string
        assert "KEY1" in seen["url"]


async def test_unauthorized_key_returns_not_ok() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={}))
    )
    ok, message = await validate_provider_key(
        "stt", "deepgram", "bad-key", {}, client=client
    )
    assert not ok
    assert "401" in message


async def test_network_error_returns_not_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok, message = await validate_provider_key("tts", "cartesia", "k", {}, client=client)
    assert not ok


async def test_unknown_provider_rejected() -> None:
    ok, message = await validate_provider_key("llm", "grok", "k", {})
    assert not ok
    assert "unknown" in message.lower()
