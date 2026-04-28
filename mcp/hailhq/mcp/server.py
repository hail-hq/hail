"""Hail MCP server — remote SSE app.

Deployable artifact is ``app``; FastMCP exposes ``/sse`` and ``/messages``
(see :doc:`docs/setup/mcp.md` for why SSE-only). We add ``/healthz`` to
the same Starlette app so the compose healthcheck stays a one-line probe
instead of spawning an MCP handshake per check.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hailhq.mcp.hail_client import HailClient
from hailhq.mcp.tools import register_tools


def _build_app() -> tuple[FastMCP, HailClient, Starlette]:
    mcp_app: FastMCP = FastMCP(name="hail")
    client = HailClient()
    register_tools(mcp_app, client)
    sse_app = mcp_app.sse_app()

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    sse_app.router.add_route("/healthz", healthz, methods=["GET"])
    return mcp_app, client, sse_app


mcp_app, hail_client, app = _build_app()


__all__ = ["app", "mcp_app", "hail_client"]
