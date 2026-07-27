"""Capability probes + tri-state validation, mocked transport (no live network)."""

from __future__ import annotations

import httpx
import pytest
from hailhq.core.provider_validation import validate_provider_key

# (provider, layer, params, method, url-substr, header-key, header-val-substr, mode)
PROBES = [
    (
        "openai-compatible",
        "llm",
        {"base_url": "https://api.openai.com/v1"},
        "GET",
        "https://api.openai.com/v1/models",
        "authorization",
        "Bearer ",
        "auth",
    ),
    (
        "anthropic",
        "llm",
        {},
        "GET",
        "https://api.anthropic.com/v1/models",
        "x-api-key",
        "",
        "auth",
    ),
    ("google", "llm", {}, "GET", "generativelanguage.googleapis.com", None, "", "auth"),
    (
        "elevenlabs",
        "tts",
        {},
        "POST",
        "https://api.elevenlabs.io/v1/text-to-speech/",
        "xi-api-key",
        "",
        "capability",
    ),
    (
        "cartesia",
        "tts",
        {},
        "POST",
        "https://api.cartesia.ai/tts/bytes",
        "x-api-key",
        "",
        "capability",
    ),
    (
        "deepgram",
        "stt",
        {},
        "POST",
        "https://api.deepgram.com/v1/listen",
        "authorization",
        "Token ",
        "capability",
    ),
]


@pytest.mark.parametrize("provider,layer,params,method,url_part,hk,hv,mode", PROBES)
async def test_probe_shape(
    provider, layer, params, method, url_part, hk, hv, mode
) -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["url"] = str(req.url)
        seen["headers"] = req.headers
        # 200 for auth-mode success, a post-auth 4xx for capability-mode success
        return httpx.Response(200 if mode == "auth" else 404, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    status, _ = await validate_provider_key(
        layer, provider, "KEY123", params, client=client
    )
    assert status == "valid"
    assert seen["method"] == method
    assert url_part in seen["url"]
    if hk:
        assert hk in {k.lower() for k in seen["headers"]}
        assert "KEY123" in seen["headers"][hk]
    else:  # google: key in query string
        assert "KEY123" in seen["url"]


@pytest.mark.parametrize(
    "provider,layer,params,mode", [(p[0], p[1], p[2], p[7]) for p in PROBES]
)
async def test_401_is_invalid(provider, layer, params, mode) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={}))
    )
    status, _msg = await validate_provider_key(
        layer, provider, "bad", params, client=client
    )
    assert status == "invalid"


@pytest.mark.parametrize("code", [429, 500, 503])
async def test_5xx_and_429_are_indeterminate_capability(code) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(code, json={}))
    )
    status, _msg = await validate_provider_key(
        "tts", "elevenlabs", "k", {}, client=client
    )
    assert status == "indeterminate"


async def test_auth_mode_404_is_indeterminate_not_valid() -> None:
    # A 404 on GET /models likely means a wrong base_url, not a good key.
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    )
    status, _ = await validate_provider_key(
        "llm",
        "openai-compatible",
        "k",
        {"base_url": "https://api.openai.com/v1"},
        client=client,
    )
    assert status == "indeterminate"


async def test_capability_mode_400_is_valid() -> None:
    # deepgram empty-body → 400 PAYLOAD_ERROR after auth → key is usable.
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json={}))
    )
    status, _ = await validate_provider_key("stt", "deepgram", "k", {}, client=client)
    assert status == "valid"


async def test_capability_mode_3xx_is_indeterminate() -> None:
    # A redirect proves neither auth nor capability — not a valid key.
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(302, json={}))
    )
    status, _ = await validate_provider_key("tts", "elevenlabs", "k", {}, client=client)
    assert status == "indeterminate"


async def test_network_error_is_indeterminate() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=req)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    status, _ = await validate_provider_key("tts", "cartesia", "k", {}, client=client)
    assert status == "indeterminate"


async def test_unknown_provider_invalid() -> None:
    status, msg = await validate_provider_key("llm", "grok", "k", {})
    assert status == "invalid"
    assert "unknown" in msg.lower()


async def test_openai_bad_base_url_invalid() -> None:
    status, _msg = await validate_provider_key(
        "llm", "openai-compatible", "k", {"base_url": "http://169.254.169.254/v1"}
    )
    assert status == "invalid"


async def test_elevenlabs_body_never_uses_a_real_voice() -> None:
    # Guard against a future edit that plugs the stored voice_id into the probe
    # (a real voice → synthesis → billing). The probe voice must be a sentinel.
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(404, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await validate_provider_key(
        "tts", "elevenlabs", "k", {"voice_id": "real-voice-xyz"}, client=client
    )
    assert "real-voice-xyz" not in seen["url"]


async def test_cartesia_body_never_uses_a_real_voice() -> None:
    # Cartesia's voice id rides in the JSON body (voice.id), not the URL — the
    # exact spot a future edit might plug in a stored voice_id → synthesis →
    # billing. The probe body must carry the sentinel uuid, never the real id.
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = req.content.decode()
        return httpx.Response(404, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await validate_provider_key(
        "tts", "cartesia", "k", {"voice_id": "real-voice-xyz"}, client=client
    )
    assert "real-voice-xyz" not in seen["body"]
    assert "real-voice-xyz" not in seen["url"]
    assert "00000000-0000-0000-0000-000000000000" in seen["body"]
