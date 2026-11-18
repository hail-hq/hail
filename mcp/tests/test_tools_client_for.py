"""Tests for the per-tool-call HailClient helper.

In oauth-rs mode the helper builds a fresh HailClient from the bearer
on MCPServer's request context, hands it to the tool, and closes it on
exit. In static-key mode the helper yields the shared singleton without
closing.
"""

from __future__ import annotations

import asyncio
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
        if cache_key:
            assert get_identity()["organization_id"] == "org-id"
            assert get_identity()["user_id"] == "user-id"
    org_name.assert_not_awaited()
    if cache_key:
        assert (await tools.resolve_actor(SimpleNamespace(whoami=whoami), cache_key))[
            "user_id"
        ] == "user-id"
        assert whoami.await_count == 1
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
            assert get_identity() == {}
    assert whoami.await_count == 1


@pytest.mark.asyncio
async def test_no_actor_lookup_when_analytics_setup_failed(monkeypatch):
    monkeypatch.setattr(tools, "telemetry_enabled", lambda: False)
    monkeypatch.setattr(tools, "_analytics_actor", False)
    whoami = AsyncMock()
    async with tools._actor_for(SimpleNamespace(whoami=whoami), cache_key="u"):
        pass
    whoami.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_name_failure_keeps_actor_ids(monkeypatch):
    monkeypatch.setattr(tools, "telemetry_enabled", lambda: True)
    monkeypatch.setattr(tools, "_actor_cache", {})
    monkeypatch.setattr(
        tools, "fetch_organization_name", AsyncMock(side_effect=RuntimeError("down"))
    )
    whoami = AsyncMock(return_value={"organization_id": "org-id", "user_id": "user-id"})
    async with tools._actor_for(SimpleNamespace(whoami=whoami), cache_key="user.jwt"):
        assert get_identity()["user_id"] == "user-id"
    assert (await tools.resolve_actor(SimpleNamespace(whoami=whoami), "user.jwt"))[
        "user_id"
    ] == "user-id"
    assert whoami.await_count == 1


@pytest.mark.asyncio
async def test_slow_actor_lookup_is_bounded_and_failure_is_cached(monkeypatch):
    monkeypatch.setattr(tools, "telemetry_enabled", lambda: False)
    monkeypatch.setattr(tools, "_analytics_actor", True)
    monkeypatch.setattr(tools, "_actor_cache", {})
    monkeypatch.setattr(tools, "_ACTOR_LOOKUP_SECONDS", 0.01)

    async def slow_whoami():
        await asyncio.Event().wait()

    whoami = AsyncMock(side_effect=slow_whoami)
    client = SimpleNamespace(whoami=whoami)
    for _ in range(2):
        actor = await asyncio.wait_for(tools.resolve_actor(client, "slow.jwt"), 0.2)
        assert actor == {}
    assert whoami.await_count == 1


@pytest.mark.asyncio
async def test_actor_cache_separates_bearers_and_refreshes_expired_profiles(
    monkeypatch,
):
    monkeypatch.setattr(tools, "telemetry_enabled", lambda: False)
    monkeypatch.setattr(tools, "_analytics_actor", True)
    monkeypatch.setattr(tools, "_actor_cache", {})
    clock = [100.0]
    monkeypatch.setattr(tools.time, "monotonic", lambda: clock[0])
    whoami = AsyncMock(
        side_effect=[
            {"user_id": "user-a", "email": "a@example.org", "name": "User A"},
            {"user_id": "user-b", "email": "b@example.org", "name": "User B"},
            {"user_id": "user-a", "email": "new@example.org", "name": "New Name"},
        ]
    )
    client = SimpleNamespace(whoami=whoami)
    assert (await tools.resolve_actor(client, "token-a"))[
        "user_email"
    ] == "a@example.org"
    assert (await tools.resolve_actor(client, "token-b"))["user_name"] == "User B"
    assert (await tools.resolve_actor(client, "token-a"))["user_name"] == "User A"
    assert whoami.await_count == 2
    clock[0] += 301
    refreshed = await tools.resolve_actor(client, "token-a")
    assert refreshed["user_email"] == "new@example.org"
    assert refreshed["user_name"] == "New Name"
    assert whoami.await_count == 3


@pytest.mark.asyncio
async def test_slow_org_name_shares_the_lookup_bound_and_keeps_ids(monkeypatch):
    monkeypatch.setattr(tools, "telemetry_enabled", lambda: True)
    monkeypatch.setattr(tools, "_actor_cache", {})
    monkeypatch.setattr(tools, "_ACTOR_LOOKUP_SECONDS", 0.01)

    async def slow_org_name(_org_id):
        await asyncio.Event().wait()

    monkeypatch.setattr(tools, "fetch_organization_name", slow_org_name)
    whoami = AsyncMock(return_value={"organization_id": "org-id", "user_id": "user-id"})
    actor = await asyncio.wait_for(
        tools.resolve_actor(SimpleNamespace(whoami=whoami), "user.jwt"), 0.2
    )
    assert actor["user_id"] == "user-id"
    assert "organization_name" not in actor


@pytest.mark.asyncio
async def test_parallel_calls_share_one_actor_lookup(monkeypatch):
    monkeypatch.setattr(tools, "telemetry_enabled", lambda: False)
    monkeypatch.setattr(tools, "_analytics_actor", True)
    monkeypatch.setattr(tools, "_actor_cache", {})
    monkeypatch.setattr(tools, "_actor_lookups", {})
    release = asyncio.Event()

    async def slow_whoami():
        await release.wait()
        return {"organization_id": "org-id", "user_id": "user-id"}

    whoami = AsyncMock(side_effect=slow_whoami)
    client = SimpleNamespace(whoami=whoami)
    lookups = [asyncio.create_task(tools.resolve_actor(client, "u")) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()
    actors = await asyncio.gather(*lookups)
    assert [a["user_id"] for a in actors] == ["user-id"] * 3
    assert whoami.await_count == 1
    assert tools._actor_lookups == {}
