"""Transport-level wiring tests for the combined MCP Starlette app.

These assert the deployable ``app`` exposes both MCP transports
(Streamable HTTP at ``/``, legacy SSE at ``/sse`` + ``/messages/``)
plus the ``/healthz`` probe, and that the Streamable HTTP
session-manager lifespan starts and stops cleanly. The MCP protocol
handshake itself is framework territory and is not covered here —
matching the posture of ``test_tools.py``.

Each test builds a fresh app via ``_build_app()`` rather than importing
the module-level singleton: ``StreamableHTTPSessionManager.run()`` may
only be entered once per ``FastMCP`` instance, so a fresh app per test
keeps the lifespan re-entrant across the suite.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from hailhq.mcp.server import _build_app


def _route_paths(app: Starlette) -> set[str | None]:
    return {getattr(route, "path", None) for route in app.routes}


def test_app_exposes_both_transports_and_healthz() -> None:
    _mcp_app, _client, app = _build_app()
    paths = _route_paths(app)
    assert "/healthz" in paths
    assert "/sse" in paths
    assert "/" in paths  # Streamable HTTP at root
    # SSE delivers client->server messages to a mounted message path.
    assert any(p is not None and p.startswith("/messages") for p in paths)


def test_healthz_ok_under_lifespan() -> None:
    # Entering TestClient as a context manager runs the app lifespan,
    # which drives StreamableHTTPSessionManager.run(). A clean startup +
    # 200 from /healthz proves the parent lifespan is wired correctly
    # (a misconfigured lifespan raises on context-manager enter).
    _mcp_app, _client, app = _build_app()
    with TestClient(app) as test_client:
        resp = test_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_streamable_http_accepts_proxied_host() -> None:
    # Regression: FastMCP auto-enables DNS-rebinding protection when its
    # configured host is localhost, which 421s a proxied public Host
    # (e.g. mcp.hail.so behind Caddy). Binding the app host to "0.0.0.0"
    # tells the SDK this is a public bind, skipping that localhost-only
    # guard. 421 here means the Host header was rejected.
    _mcp_app, _client, app = _build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/",
            headers={
                "host": "mcp.hail.so",
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        )
    assert (
        resp.status_code != 421
    ), f"Host header rejected: {resp.status_code} {resp.text}"
