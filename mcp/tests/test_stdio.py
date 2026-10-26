"""Exercise MCP over a real stdio subprocess."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client.stdio import stdio_client

from mcp import ClientSession, StdioServerParameters

ROOT = Path(__file__).resolve().parents[2]


def _env() -> dict[str, str]:
    return {
        "PYTHONPATH": os.pathsep.join([str(ROOT / "mcp"), str(ROOT / "core")]),
        "HAIL_API_KEY": "hk_stdio_test_not_a_real_key",
        "HAIL_AUTH_URL": "",
        # Discovery and ping must work without an API or provider running.
        "HAIL_API_URL": "http://127.0.0.1:9",
    }


@pytest.mark.asyncio
async def test_stdio_initializes_pings_and_lists_real_tools(tmp_path: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-u", "-m", "hailhq.mcp.stdio"],
        env=_env(),
        cwd=tmp_path,
    )
    async with (
        asyncio.timeout(15),
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        result = await session.initialize()
        assert result.serverInfo.name == "hail"
        await session.send_ping()
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        for name in ["place_call", "send_sms", "send_email", "quote_numbers"]:
            assert name in tools
        assert "recipient_consent" in tools["send_email"].inputSchema["properties"]
        assert tools["quote_numbers"].annotations.readOnlyHint is True


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"HAIL_API_KEY": ""}, "MCP auth not configured"),
        (
            {"HAIL_API_KEY": "", "HAIL_AUTH_URL": "https://auth.example.test"},
            "stdio requires HAIL_API_KEY; unset HAIL_AUTH_URL",
        ),
    ],
)
def test_stdio_rejects_missing_or_oauth_credentials(
    tmp_path: Path, overrides: dict[str, str], message: str
) -> None:
    result = subprocess.run(
        [sys.executable, "-u", "-m", "hailhq.mcp.stdio"],
        cwd=tmp_path,
        env={**_env(), **overrides},
        input="",
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert message in result.stderr
