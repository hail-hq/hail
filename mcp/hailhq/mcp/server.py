"""Hail MCP server — remote app exposing both MCP transports.

The deployable artifact is ``app``. FastMCP serves Streamable HTTP at
``/`` (root — the service runs on a dedicated MCP subdomain, so the
endpoint is the bare host URL, not ``/mcp``) and legacy SSE at ``/sse``
+ ``/messages/`` during the transition window. We add ``/healthz`` to the
same Starlette app so the compose healthcheck stays a one-line probe
instead of spawning an MCP handshake per check.

Streamable HTTP needs its session manager running for the lifetime of
the app. ``FastMCP.streamable_http_app()`` wires that into its own
Starlette lifespan, but here we own the combined parent app, so we drive
``session_manager.run()`` from the parent lifespan ourselves.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from hailhq.mcp.hail_client import HailClient
from hailhq.mcp.tools import register_tools


def _build_app() -> tuple[FastMCP, HailClient, Starlette]:
    # streamable_http_path defaults to "/mcp"; the service runs on a
    # dedicated MCP subdomain, so serve Streamable HTTP at the root to
    # avoid a redundant /mcp segment in the public URL.
    #
    # host="0.0.0.0" matches the uvicorn bind and marks this as a public
    # bind, so FastMCP skips the localhost-only DNS-rebinding guard it
    # would otherwise auto-enable — that guard 421s the proxied public
    # Host (e.g. mcp.hail.so). The guard protects browser-reachable
    # localhost dev servers, which does not apply here: in prod the
    # container binds loopback only (127.0.0.1:8081) and Caddy host-routes
    # mcp.${HAIL_DOMAIN}. The server does not yet validate an inbound
    # bearer — per-connection auth lands in Phase 1c; pinning allowed_hosts
    # would add no real protection given the loopback bind.
    mcp_app: FastMCP = FastMCP(name="hail", streamable_http_path="/", host="0.0.0.0")
    client = HailClient()
    register_tools(mcp_app, client)

    # Build both transports. streamable_http_app() lazily creates the
    # session manager that the lifespan below runs; call it before the
    # lifespan references mcp_app.session_manager.
    sse_app = mcp_app.sse_app()  # /sse + /messages/
    http_app = mcp_app.streamable_http_app()  # / (root)

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with mcp_app.session_manager.run():
            yield

    # NOTE: splatting sub_app.routes drops sub_app.user_middleware. Both
    # middleware lists are empty today (no auth configured). Phase 1c
    # (oauth-rs mode) configures a token verifier, which makes FastMCP add
    # AuthenticationMiddleware + AuthContextMiddleware inside sse_app() /
    # streamable_http_app() — that spec must either fold sub_app.user_middleware
    # in here or switch to a FastMCP-owned app with a different combining strategy.
    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            *sse_app.routes,
            *http_app.routes,
        ],
        lifespan=lifespan,
    )
    return mcp_app, client, app


mcp_app, hail_client, app = _build_app()


__all__ = ["app", "mcp_app", "hail_client"]
