"""Stdio entry point using the shared tools and API-key client."""

from __future__ import annotations

import asyncio

import logfire
from hailhq.core.telemetry import configure_telemetry, flush_telemetry
from hailhq.mcp.auth import AuthMode, select_auth_mode
from hailhq.mcp.hail_client import HailClient
from hailhq.mcp.tools import register_tools

from mcp.server.fastmcp import FastMCP


async def serve() -> None:
    mode = select_auth_mode()
    if mode is not AuthMode.STATIC_KEY:
        raise RuntimeError("stdio requires HAIL_API_KEY; unset HAIL_AUTH_URL")
    if configure_telemetry("hail-mcp"):
        logfire.instrument_mcp()

    try:
        async with HailClient() as client:
            server = FastMCP(name="hail")
            register_tools(server, mode=mode, singleton=client)
            await server.run_stdio_async()
    finally:
        await asyncio.to_thread(flush_telemetry)


if __name__ == "__main__":
    asyncio.run(serve())
