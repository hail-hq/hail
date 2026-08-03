"""Tests for the per-tool-call HailClient helper.

In oauth-rs mode the helper builds a fresh HailClient from the bearer
on FastMCP's request context, hands it to the tool, and closes it on
exit. In static-key mode the helper yields the shared singleton without
closing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from hailhq.mcp.auth import AuthMode
from hailhq.mcp.hail_client import HailClient
from hailhq.mcp.tools import _client_for


def _ctx_with_bearer(bearer: str | None) -> SimpleNamespace:
    """Minimal FastMCP Context stand-in with the headers attribute the
    helper reaches into. Real FastMCP injects a richer Context object;
    we only depend on ``ctx.request_context.request.headers``."""
    headers = {}
    if bearer is not None:
        headers["authorization"] = f"Bearer {bearer}"
    return SimpleNamespace(
        request_context=SimpleNamespace(request=SimpleNamespace(headers=headers))
    )


@pytest.mark.asyncio
async def test_client_for_oauth_rs_builds_from_bearer():
    ctx = _ctx_with_bearer("eyJfake.jwt.value")
    async with _client_for(ctx, mode=AuthMode.OAUTH_RS, singleton=None) as client:
        assert isinstance(client, HailClient)
        # The bearer is wired through the constructor as api_key.
        assert client._api_key == "eyJfake.jwt.value"


@pytest.mark.asyncio
async def test_client_for_oauth_rs_closes_on_exit():
    """The per-call client's httpx pool must close on context exit."""
    ctx = _ctx_with_bearer("opaque")
    async with _client_for(ctx, mode=AuthMode.OAUTH_RS, singleton=None) as client:
        underlying = client._client
    assert underlying.is_closed


@pytest.mark.asyncio
async def test_client_for_oauth_rs_missing_bearer_raises():
    ctx = _ctx_with_bearer(None)
    with pytest.raises(RuntimeError, match="missing Authorization"):
        async with _client_for(ctx, mode=AuthMode.OAUTH_RS, singleton=None):
            pass


@pytest.mark.asyncio
async def test_client_for_static_key_yields_singleton():
    singleton = HailClient(base_url="http://t", api_key="hl_live_xxx")
    ctx = _ctx_with_bearer(None)  # No bearer needed in static-key mode.
    async with _client_for(
        ctx, mode=AuthMode.STATIC_KEY, singleton=singleton
    ) as client:
        assert client is singleton
    # Singleton stays open after the context exits.
    assert not singleton._client.is_closed
    await singleton.aclose()
