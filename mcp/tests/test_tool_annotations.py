"""Every registered MCP tool carries explicit annotation hints.

A new tool fails this test until it is classified below, so no tool ships
with a default (and misleading) hint set.
"""

from __future__ import annotations

import importlib

import pytest
from mcp.types import Tool

# Read-only tools: never change anything.
_READ_ONLY = {
    "get_call",
    "list_calls",
    "get_sms",
    "list_sms",
    "get_email",
    "list_emails",
    "get_email_raw",
    "get_email_attachment",
    "get_email_events",
    "get_email_stats",
    "get_events",
    "list_agents",
    "list_contacts",
    "lookup_contact",
    "list_email_domains",
    "whoami",
    "get_agent",
    "list_numbers",
    "get_number",
    "quote_numbers",
    "get_verification_requirements",
    "list_verifications",
    "get_verification",
}
# Read-only tools that talk to outside systems.
_READ_ONLY_OPEN_WORLD = {
    "quote_numbers",
    "get_verification_requirements",
    "list_verifications",
    "get_verification",
}

# Destructive tools. acquire_number spends money, so clients ask a human.
# submit_verification cannot be undone; cancel_verification discards a draft.
_DESTRUCTIVE = {
    "release_number",
    "delete_agent",
    "acquire_number",
    "submit_verification",
    "cancel_verification",
}

# Tools that reach people or carriers outside Hail.
_OPEN_WORLD_WRITES = {
    "place_call",
    "send_sms",
    "send_email",
    "release_number",
    "acquire_number",
    "submit_verification",
    "cancel_verification",
}

_EXPECTED_COUNT = 36


@pytest.fixture()
async def tools_by_name(monkeypatch) -> dict[str, Tool]:
    monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "hl_live_test")
    import hailhq.mcp.server as srv

    srv = importlib.reload(srv)
    listed = await srv.mcp_app.list_tools()
    return {t.name: t for t in listed}


async def test_tool_count(tools_by_name) -> None:
    assert len(tools_by_name) == _EXPECTED_COUNT


async def test_every_tool_has_explicit_hints_and_title(tools_by_name) -> None:
    for name, tool in tools_by_name.items():
        a = tool.annotations
        assert a is not None, name
        for hint in (
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        ):
            assert getattr(a, hint) is not None, f"{name}.{hint} not set"
        assert tool.title, f"{name} has no title"


async def test_read_only_set_is_exact(tools_by_name) -> None:
    got = {n for n, t in tools_by_name.items() if t.annotations.readOnlyHint is True}
    assert got == _READ_ONLY


async def test_read_only_tools_are_idempotent_not_destructive(tools_by_name) -> None:
    for name in _READ_ONLY:
        a = tools_by_name[name].annotations
        assert a.destructiveHint is False, name
        assert a.idempotentHint is True, name
        assert a.openWorldHint is (name in _READ_ONLY_OPEN_WORLD), name


async def test_destructive_set_is_exact(tools_by_name) -> None:
    got = {n for n, t in tools_by_name.items() if t.annotations.destructiveHint is True}
    assert got == _DESTRUCTIVE
    # Not idempotent: a repeat submits again or fails.
    not_idempotent = {"submit_verification", "cancel_verification"}
    for name in _DESTRUCTIVE:
        a = tools_by_name[name].annotations
        assert a.idempotentHint is (name not in not_idempotent), name
        assert a.readOnlyHint is False, name


async def test_open_world_writes(tools_by_name) -> None:
    got = {
        n
        for n, t in tools_by_name.items()
        if t.annotations.openWorldHint is True and t.annotations.readOnlyHint is False
    }
    assert got == _OPEN_WORLD_WRITES
