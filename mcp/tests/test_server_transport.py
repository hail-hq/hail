"""Smoke tests for the server's boot wiring.

In oauth-rs mode: unauth requests get 401 with the protected-resource
hint, and FastMCP auto-mounts /.well-known/oauth-protected-resource.
In static-key mode: no auth, no protected-resource route. SSE is gone
in both modes.
"""

from __future__ import annotations

import importlib

import httpx
import pytest


def _boot(
    monkeypatch, *, oauth: bool, mcp_resource_url: str = "https://mcp.hail.so"
) -> object:
    """Reload server.py under a specific mode env."""
    if oauth:
        monkeypatch.setattr(
            "hailhq.core.config.settings.hail_auth_url", "https://hail.so/api/auth"
        )
        monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "")
        monkeypatch.setattr(
            "hailhq.core.config.settings.mcp_resource_url", mcp_resource_url
        )
    else:
        monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "")
        monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "hl_live_test")

    import hailhq.mcp.server as srv

    return importlib.reload(srv)


@pytest.mark.asyncio
async def test_oauth_rs_unauth_returns_401_with_resource_metadata(monkeypatch):
    srv = _boot(monkeypatch, oauth=True)
    transport = httpx.ASGITransport(app=srv.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 401
    www = resp.headers.get("www-authenticate", "")
    assert "Bearer" in www
    assert "resource_metadata=" in www
    assert "https://mcp.hail.so/.well-known/oauth-protected-resource" in www


@pytest.mark.asyncio
async def test_oauth_rs_publishes_protected_resource_metadata(monkeypatch):
    srv = _boot(monkeypatch, oauth=True)
    transport = httpx.ASGITransport(app=srv.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    # Pydantic AnyHttpUrl canonicalises bare-host URLs with a trailing
    # slash (https://mcp.hail.so → https://mcp.hail.so/). Strip the slash
    # for the comparison rather than fighting the framework.
    assert body["resource"].rstrip("/") == "https://mcp.hail.so"
    assert "https://hail.so/api/auth" in body["authorization_servers"]


@pytest.mark.asyncio
async def test_static_key_no_auth_no_protected_resource_route(monkeypatch):
    srv = _boot(monkeypatch, oauth=False)
    # The POST to "/" reaches FastMCP's streamable handler in static-key
    # mode (no auth middleware to short-circuit it), and that handler
    # requires the session manager — which only starts under the parent
    # app's lifespan. httpx.ASGITransport does not run lifespan, so drive
    # it explicitly via Starlette's lifespan_context.
    async with srv.app.router.lifespan_context(srv.app):
        transport = httpx.ASGITransport(app=srv.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # Missing bearer is fine in static-key mode (the route does not require auth).
            # We expect SOMETHING other than 401-with-WWW-Authenticate. A 4xx without
            # the discovery hint is the contract.
            resp = await c.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            if resp.status_code == 401:
                assert "resource_metadata=" not in resp.headers.get(
                    "www-authenticate", ""
                )
            # And the protected-resource route is not mounted.
            wk = await c.get("/.well-known/oauth-protected-resource")
    assert wk.status_code == 404


@pytest.mark.asyncio
async def test_no_sse_routes_in_either_mode(monkeypatch):
    """SSE is gone — neither /sse nor /messages/ is mounted."""
    for oauth in (True, False):
        srv = _boot(monkeypatch, oauth=oauth)
        paths = {getattr(r, "path", None) for r in srv.app.routes} | {
            getattr(r, "path_format", None) for r in srv.app.routes
        }
        assert "/sse" not in paths, f"oauth={oauth}: /sse should be removed"
        assert "/messages/" not in paths, f"oauth={oauth}: /messages/ should be removed"


@pytest.mark.asyncio
async def test_healthz_works_in_both_modes(monkeypatch):
    for oauth in (True, False):
        srv = _boot(monkeypatch, oauth=oauth)
        transport = httpx.ASGITransport(app=srv.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            resp = await c.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_oauth_rs_requires_mcp_resource_url(monkeypatch):
    """Empty MCP_RESOURCE_URL in oauth-rs mode is a boot-time error,
    not a deferred pydantic-validation surprise at FastMCP construction."""
    with pytest.raises(RuntimeError, match="MCP_RESOURCE_URL"):
        _boot(monkeypatch, oauth=True, mcp_resource_url="")
