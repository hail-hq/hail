"""Hail MCP server — Streamable HTTP only, mode-dispatched at boot.

The deployable artifact is ``app``. FastMCP serves Streamable HTTP at
``/`` (root — the service runs on a dedicated MCP subdomain so the
endpoint is the bare host URL). ``/healthz`` is mounted on the same
Starlette parent app so the compose healthcheck stays a one-line probe.
``/.well-known/glama.json`` is mounted the same way, to verify ownership
of the Glama MCP connector listing for this domain.

Two boot modes (see ``hailhq.mcp.auth.select_auth_mode``):

* **oauth-rs** — FastMCP receives ``AuthSettings`` + a pass-through
  ``TokenVerifier``. FastMCP auto-mounts
  ``/.well-known/oauth-protected-resource`` and rejects bearer-less
  requests with ``401 WWW-Authenticate: Bearer resource_metadata=...``.
* **static-key** — no FastMCP auth, no protected-resource route. Tools
  use the shared ``HAIL_API_KEY`` singleton (unchanged from pre-1c).

Streamable HTTP needs its session manager running for the lifetime of
the app. ``FastMCP.streamable_http_app()`` wires that into its own
Starlette lifespan, but here we own the combined parent app, so we drive
``session_manager.run()`` from the parent lifespan ourselves.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from hailhq.core.config import settings
from hailhq.mcp.auth import AuthMode, PassThroughVerifier, select_auth_mode
from hailhq.mcp.hail_client import HailClient
from hailhq.mcp.tools import register_tools


def _build_app() -> tuple[FastMCP, HailClient | None, Starlette]:
    mode = select_auth_mode()

    if mode is AuthMode.OAUTH_RS and not settings.mcp_resource_url:
        raise RuntimeError("oauth-rs mode requires MCP_RESOURCE_URL to be set")

    if mode is AuthMode.OAUTH_RS:
        verifier = PassThroughVerifier(resource_server_url=settings.mcp_resource_url)
        auth_settings = AuthSettings(
            issuer_url=settings.hail_auth_url,
            resource_server_url=settings.mcp_resource_url,
            required_scopes=None,  # scope enforcement deferred to Phase 2
        )
        mcp_app: FastMCP = FastMCP(
            name="hail",
            streamable_http_path="/",
            host="0.0.0.0",
            token_verifier=verifier,
            auth=auth_settings,
        )
        # Tools build per-call HailClient from ctx.request_context bearer;
        # no module-level singleton in oauth-rs mode.
        singleton: HailClient | None = None
    else:
        # static-key: pre-1c shape.
        mcp_app = FastMCP(name="hail", streamable_http_path="/", host="0.0.0.0")
        singleton = HailClient()

    register_tools(mcp_app, mode=mode, singleton=singleton)

    http_app = mcp_app.streamable_http_app()  # / (root)

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def glama_connector_claim(_request: Request) -> Response:
        return JSONResponse(
            {
                "$schema": "https://glama.ai/mcp/schemas/connector.json",
                "maintainers": [{"email": "redouane.a.achouri@gmail.com"}],
            }
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with mcp_app.session_manager.run():
            yield

    # Splatting sub_app.routes drops sub_app.user_middleware. FastMCP
    # adds AuthenticationMiddleware + AuthContextMiddleware *inside*
    # streamable_http_app() when auth is configured — those land on
    # http_app.user_middleware, NOT on its routes. Fold them into the
    # parent app's middleware stack so 401-with-WWW-Authenticate fires
    # before any route resolution.
    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/.well-known/glama.json", glama_connector_claim, methods=["GET"]),
            *http_app.routes,
        ],
        middleware=list(http_app.user_middleware),
        lifespan=lifespan,
    )
    return mcp_app, singleton, app


mcp_app, hail_client, app = _build_app()

__all__ = ["app", "mcp_app", "hail_client"]
