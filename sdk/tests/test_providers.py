"""End-to-end client tests for the `/providers` surface — request shapes."""

from __future__ import annotations

import json

import httpx
import respx
from hail import Client


def make_provider_entry(
    *,
    layer: str = "llm",
    provider: str = "openai-compatible",
    key_last4: str | None = "ABCD",
    params: dict | None = None,
    fallback_enabled: bool = False,
    is_active: bool = True,
) -> dict:
    """Server-shaped JSON for a ProviderConfigEntry."""
    return {
        "layer": layer,
        "provider": provider,
        "key_last4": key_last4,
        "key_set_at": "2026-08-06T12:00:00+00:00",
        "params": params if params is not None else {"model": "my-model"},
        "fallback_enabled": fallback_enabled,
        "is_active": is_active,
    }


# --------------------------------------------------------------------------- #
# providers.list
# --------------------------------------------------------------------------- #


@respx.mock
async def test_providers_list(base_url: str, api_key: str) -> None:
    payload = {"providers": [make_provider_entry(), make_provider_entry(layer="tts")]}
    route = respx.get(f"{base_url}/providers").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        got = await c.providers.list()

    assert [p.layer for p in got.providers] == ["llm", "tts"]
    assert got.providers[0].key_last4 == "ABCD"
    req = route.calls.last.request
    assert req.method == "GET"
    assert req.headers["Authorization"] == f"Bearer {api_key}"


# --------------------------------------------------------------------------- #
# providers.set
# --------------------------------------------------------------------------- #


@respx.mock
async def test_providers_set_sends_full_body(base_url: str, api_key: str) -> None:
    route = respx.put(f"{base_url}/providers/llm").mock(
        return_value=httpx.Response(200, json=make_provider_entry())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        entry = await c.providers.set(
            "llm",
            provider="openai-compatible",
            api_key="sk-endpoint-ABCD",
            params={"base_url": "https://llm.example.com/v1", "model": "my-model"},
            fallback_enabled=True,
        )

    assert entry.provider == "openai-compatible"
    assert entry.is_active is True
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "provider": "openai-compatible",
        "api_key": "sk-endpoint-ABCD",
        "params": {"base_url": "https://llm.example.com/v1", "model": "my-model"},
        "fallback_enabled": True,
    }


@respx.mock
async def test_providers_set_omits_api_key_when_not_given(
    base_url: str, api_key: str
) -> None:
    """No ``api_key`` in the body means "keep the stored key" server-side —
    sending ``null`` would be a different request. Assert it's absent."""
    route = respx.put(f"{base_url}/providers/tts").mock(
        return_value=httpx.Response(200, json=make_provider_entry(layer="tts"))
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.providers.set("tts", provider="cartesia", params={"voice_id": "v-1"})

    body = json.loads(route.calls.last.request.content)
    assert body == {
        "provider": "cartesia",
        "params": {"voice_id": "v-1"},
        "fallback_enabled": False,
    }
    assert "api_key" not in body


# --------------------------------------------------------------------------- #
# providers.delete
# --------------------------------------------------------------------------- #


@respx.mock
async def test_providers_delete(base_url: str, api_key: str) -> None:
    route = respx.delete(f"{base_url}/providers/stt/deepgram").mock(
        return_value=httpx.Response(204)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        assert await c.providers.delete("stt", "deepgram") is None

    assert route.calls.last.request.method == "DELETE"


# --------------------------------------------------------------------------- #
# providers.activate
# --------------------------------------------------------------------------- #


@respx.mock
async def test_providers_activate(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/providers/tts/activate").mock(
        return_value=httpx.Response(
            200, json=make_provider_entry(layer="tts", provider="cartesia")
        )
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        entry = await c.providers.activate("tts", provider="cartesia")

    assert entry.provider == "cartesia"
    assert json.loads(route.calls.last.request.content) == {"provider": "cartesia"}


# --------------------------------------------------------------------------- #
# providers.test
# --------------------------------------------------------------------------- #


@respx.mock
async def test_providers_test_stored_key_sends_empty_body(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/providers/llm/validate").mock(
        return_value=httpx.Response(200, json={"status": "valid", "message": "checked"})
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        result = await c.providers.test("llm")

    assert result.status == "valid"
    assert result.message == "checked"
    assert json.loads(route.calls.last.request.content) == {}


@respx.mock
async def test_providers_test_with_unsaved_key(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/providers/llm/validate").mock(
        return_value=httpx.Response(200, json={"status": "invalid", "message": "401"})
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        result = await c.providers.test(
            "llm",
            provider="openai-compatible",
            api_key="sk-untested",
            params={"base_url": "https://llm.example.com/v1", "model": "m"},
        )

    assert result.status == "invalid"
    assert json.loads(route.calls.last.request.content) == {
        "provider": "openai-compatible",
        "api_key": "sk-untested",
        "params": {"base_url": "https://llm.example.com/v1", "model": "m"},
    }
