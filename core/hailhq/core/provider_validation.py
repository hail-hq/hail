"""Live provider-key validation: one cheap authenticated GET per provider.

Used by the internal ``/validate`` route (console TEST KEY button) and by
PUT-with-key. Proves the key (and, where the endpoint reflects it, the
account) is live — model names stay free text and are exercised end-to-end
by real calls, not here. Every check is a list/describe endpoint that costs
nothing and mutates nothing.
"""

from __future__ import annotations

import asyncio

import httpx

from hailhq.core.url_guard import UnsafeUrlError, assert_public_https_url
from hailhq.core.urls import join_url

_TIMEOUT = httpx.Timeout(10.0)

# Cartesia rejects requests without an API version pin.
_CARTESIA_VERSION = "2024-06-10"
_ANTHROPIC_VERSION = "2023-06-01"


def _request_for(
    provider: str, api_key: str, params: dict
) -> tuple[str, dict[str, str]] | None:
    """(url, headers) for the provider's cheapest authenticated GET."""
    if provider == "openai-compatible":
        base_url = params.get("base_url") or ""
        if not base_url:
            return None
        try:
            base_url = assert_public_https_url(base_url)
        except UnsafeUrlError:
            return None  # -> validate_provider_key returns (False, unknown/unsafe)
        return join_url(base_url, "models"), {"Authorization": f"Bearer {api_key}"}
    if provider == "anthropic":
        return "https://api.anthropic.com/v1/models", {
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
    if provider == "google":
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
            {},
        )
    if provider == "cartesia":
        return "https://api.cartesia.ai/voices", {
            "X-API-Key": api_key,
            "Cartesia-Version": _CARTESIA_VERSION,
        }
    if provider == "elevenlabs":
        return "https://api.elevenlabs.io/v1/user", {"xi-api-key": api_key}
    if provider == "deepgram":
        return "https://api.deepgram.com/v1/projects", {
            "Authorization": f"Token {api_key}"
        }
    return None


async def validate_provider_key(
    layer: str,
    provider: str,
    api_key: str,
    params: dict,
    client: httpx.AsyncClient | None = None,
) -> tuple[bool, str]:
    """Return (ok, message). Never raises on provider/network failure."""
    # _request_for resolves the customer host via blocking socket.getaddrinfo;
    # offload it so a slow attacker DNS can't stall the event loop.
    req = await asyncio.to_thread(_request_for, provider, api_key, params)
    if req is None:
        if provider == "openai-compatible" and params.get("base_url"):
            return False, "base_url is not a permitted public https endpoint"
        return False, f"unknown provider '{provider}' for layer '{layer}'"
    url, headers = req

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"could not reach {provider}: {exc.__class__.__name__}"
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code in (200, 206):
        return True, "ok"
    return False, f"{provider} rejected the key: HTTP {resp.status_code}"
