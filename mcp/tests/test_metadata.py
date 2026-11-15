"""Limits on the server metadata sent in ``initialize`` and ``tools/list``."""

from __future__ import annotations

import importlib
import re

import pytest
from hailhq.mcp import metadata
from mcp.types import Tool

# Claude Code cuts each tool description and the instructions at 2 KB.
CLIENT_LIMIT_BYTES = 2048


@pytest.fixture()
async def tools_by_name(monkeypatch) -> dict[str, Tool]:
    monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "hl_live_test")
    import hailhq.mcp.server as srv

    srv = importlib.reload(srv)
    return {t.name: t for t in await srv.mcp_app.list_tools()}


def test_instructions_fit_client_limit() -> None:
    assert len(metadata.INSTRUCTIONS.encode()) <= CLIENT_LIMIT_BYTES


async def test_instructions_name_only_real_tools(tools_by_name) -> None:
    named = set(re.findall(r"\b([a-z]+_[a-z_]+)\b", metadata.INSTRUCTIONS))
    params = {"recipient_consent", "consent_source", "idempotency_key", "next_cursor"}
    assert named - params <= set(tools_by_name)


async def test_tool_descriptions_fit_client_limit(tools_by_name) -> None:
    for name, tool in tools_by_name.items():
        assert len((tool.description or "").encode()) <= CLIENT_LIMIT_BYTES, name


async def test_every_tool_input_has_a_description(tools_by_name) -> None:
    for name, tool in tools_by_name.items():
        for param, schema in tool.inputSchema.get("properties", {}).items():
            assert schema.get("description"), f"{name}.{param}"


def test_website_url_follows_site_setting(monkeypatch) -> None:
    monkeypatch.setattr("hailhq.core.config.settings.hail_base_url", "https://x.test/")
    assert metadata.website_url() == "https://x.test/mcp"
    monkeypatch.setattr("hailhq.core.config.settings.hail_base_url", "")
    assert metadata.website_url() is None
