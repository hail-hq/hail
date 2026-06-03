"""Targeted regression tests for the deployable MCP Starlette app.

Most boot wiring (mode dispatch, 401-with-WWW-Authenticate, protected-
resource route, SSE removal, healthz) is asserted in
``test_server_transport.py``. This module covers one additional regression
that is not load-bearing for that suite: FastMCP's localhost-DNS-rebinding
protection 421-ing a proxied public ``Host`` header. Binding the app host
to ``0.0.0.0`` in ``_build_app()`` tells the SDK this is a public bind,
which skips the localhost-only guard.

Each test reloads ``server.py`` under a chosen mode env via the same
``_boot()`` helper as ``test_server_transport.py``: a fresh app per test
keeps ``StreamableHTTPSessionManager.run()`` re-entrant across the suite.
"""

from __future__ import annotations

import importlib

from starlette.testclient import TestClient


def _boot(monkeypatch, *, oauth: bool) -> object:
    """Reload server.py under a specific mode env."""
    if oauth:
        monkeypatch.setattr(
            "hailhq.core.config.settings.hail_auth_url", "https://hail.so/api/auth"
        )
        monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "")
        monkeypatch.setattr(
            "hailhq.core.config.settings.mcp_resource_url", "https://mcp.hail.so"
        )
    else:
        monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "")
        monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "hl_live_test")

    import hailhq.mcp.server as srv

    return importlib.reload(srv)


def test_streamable_http_accepts_proxied_host(monkeypatch) -> None:
    # Regression: FastMCP auto-enables DNS-rebinding protection when its
    # configured host is localhost, which 421s a proxied public Host
    # (e.g. mcp.hail.so behind Caddy). Binding the app host to "0.0.0.0"
    # tells the SDK this is a public bind, skipping that localhost-only
    # guard. 421 here means the Host header was rejected.
    #
    # We boot in static-key mode because the assertion is about the
    # streamable HTTP handler reaching the proxied Host, not about auth;
    # static-key skips the 401 middleware that would otherwise short-
    # circuit before the Host check.
    srv = _boot(monkeypatch, oauth=False)
    with TestClient(srv.app) as client:
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
