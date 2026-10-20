"""Stdio entry point for hosts such as Glama that launch a local MCP process.

Uses the same tool registry and static-key API client as the HTTP service.
Only protocol messages go to stdout. The container's default stays HTTP.
"""

from __future__ import annotations

import asyncio

from hailhq.mcp.auth import AuthMode, select_auth_mode
from hailhq.mcp.hail_client import HailClient
from hailhq.mcp.tools import register_tools

from mcp.server.fastmcp import FastMCP


async def serve() -> None:
    mode = select_auth_mode()
    if mode is not AuthMode.STATIC_KEY:
        raise RuntimeError("stdio requires HAIL_API_KEY; unset HAIL_AUTH_URL")

    async with HailClient() as client:
        server = FastMCP(name="hail")
        register_tools(server, mode=mode, singleton=client)
        await server.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(serve())
