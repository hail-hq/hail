"""Tests for the per-tool-call HailClient helper.

In oauth-rs mode the helper builds a fresh HailClient from the bearer
on MCPServer's request context, hands it to the tool, and closes it on
exit. In static-key mode the helper yields the shared singleton without
closing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.core.telemetry_identity import get_identity, identity_scope
from hailhq.mcp import tools
from hailhq.mcp.auth import AuthMode
from hailhq.mcp.hail_client import HailClient
from hailhq.mcp.tools import _client_for


def _ctx_with_bearer(bearer: str | None) -> SimpleNamespace:
    """Minimal MCPServer Context stand-in with the headers attribute the
    helper reaches into. Real MCPServer injects a richer Context object;
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


@pytest.mark.asyncio
async def test_actor_enrichment_preserves_verified_identity_and_restores_scope(
    monkeypatch,
):
    monkeypatch.setattr(tools, "telemetry_enabled", lambda: True)
    monkeypatch.setattr(
        tools, "fetch_organization_name", AsyncMock(return_value="Example Workspace")
    )
    client = SimpleNamespace(
        whoami=AsyncMock(
            return_value={
                "organization_id": "org-id",
                "user_id": "user-id",
                "email": "actor@example.org",
                "auth_kind": "jwt",
            }
        )
    )
    with identity_scope({"organization_id": "outer"}):
        async with tools._actor_for(client):
            assert get_identity()["user_email"] == "actor@example.org"
            assert get_identity()["organization_name"] == "Example Workspace"
        assert get_identity() == {"organization_id": "outer"}


@pytest.mark.asyncio
async def test_actor_enrichment_failure_does_not_prevent_tool_execution(monkeypatch):
    monkeypatch.setattr(tools, "telemetry_enabled", lambda: True)
    client = SimpleNamespace(whoami=AsyncMock(side_effect=RuntimeError("unavailable")))
    async with tools._actor_for(client):
        assert get_identity() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_key", ["user.jwt", None])
async def test_analytics_fills_actor_cache_without_logfire(monkeypatch, cache_key):
    monkeypatch.setattr(tools, "telemetry_enabled", lambda: False)
    monkeypatch.setattr(tools, "_analytics_actor", True)
    monkeypatch.setattr(tools, "_actor_cache", {})
    org_name = AsyncMock(return_value="Example Workspace")
    monkeypatch.setattr(tools, "fetch_organization_name", org_name)
    whoami = AsyncMock(return_value={"organization_id": "org-id", "user_id": "user-id"})
    async with tools._actor_for(SimpleNamespace(whoami=whoami), cache_key=cache_key):
        pass
    org_name.assert_not_awaited()
    if cache_key:
        assert tools.cached_actor_ids(cache_key) == ("org-id", "user-id")
    else:
        # Static-key mode has no per-bearer cache for analytics to read.
        whoami.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_actor_lookup_is_not_retried_every_call(monkeypatch):
    monkeypatch.setattr(tools, "telemetry_enabled", lambda: False)
    monkeypatch.setattr(tools, "_analytics_actor", True)
    monkeypatch.setattr(tools, "_actor_cache", {})
    whoami = AsyncMock(side_effect=RuntimeError("unavailable"))
    client = SimpleNamespace(whoami=whoami)
    for _ in range(3):
        async with tools._actor_for(client, cache_key="user.jwt"):
            pass
    assert whoami.await_count == 1
    assert tools.cached_actor_ids("user.jwt") == (None, None)


@pytest.mark.asyncio
async def test_no_actor_lookup_when_analytics_setup_failed(monkeypatch):
    monkeypatch.setattr(tools, "telemetry_enabled", lambda: False)
    monkeypatch.setattr(tools, "_analytics_actor", False)
    whoami = AsyncMock()
    async with tools._actor_for(SimpleNamespace(whoami=whoami), cache_key="u"):
        pass
    whoami.assert_not_awaited()
