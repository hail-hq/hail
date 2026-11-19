"""The ``update_agent`` tool exposes the handover fields ``create_agent`` has.

Tests go through ``srv.mcp_app.call_tool``: the wrapper, not the inner
function, is what drops an argument it does not know.
"""

from __future__ import annotations

import importlib
import json
from uuid import uuid4

import httpx
import respx

_BASE_URL = "http://hail-test"


async def _real_server(monkeypatch):
    monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "hl_live_test")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_url", _BASE_URL)
    import hailhq.mcp.server as srv

    return importlib.reload(srv)


def _agent(aid: str) -> dict:
    return {
        "id": aid,
        "organization_id": str(uuid4()),
        "name": "Front desk",
        "system_prompt": "Answer politely.",
        "first_message": None,
        "ai_disclosure": True,
        "ai_disclosure_line": None,
        "voice_config": {},
        "tools": None,
        "max_duration_seconds": None,
        "handover_max_duration_seconds": 600,
        "voice_enabled": True,
        "sms_enabled": True,
        "status": "live",
        "handover_contacts": [],
        "created_at": "2026-10-09T00:00:00+00:00",
        "updated_at": "2026-10-09T00:00:00+00:00",
    }


@respx.mock
async def test_update_agent_tool_passes_handover_fields(monkeypatch) -> None:
    srv = await _real_server(monkeypatch)
    aid = str(uuid4())
    cid = str(uuid4())
    route = respx.patch(f"{_BASE_URL}/agents/{aid}").mock(
        return_value=httpx.Response(200, json=_agent(aid))
    )
    await srv.mcp_app.call_tool(
        "update_agent",
        {
            "agent_id": aid,
            "handover_max_duration_seconds": 600,
            "handover_contacts": [{"contact_id": cid, "note": "billing"}],
        },
    )
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.read()) == {
        "handover_max_duration_seconds": 600,
        "handover_contacts": [{"contact_id": cid, "note": "billing"}],
    }


@respx.mock
async def test_update_agent_tool_clears_handover_limit(monkeypatch) -> None:
    srv = await _real_server(monkeypatch)
    aid = str(uuid4())
    route = respx.patch(f"{_BASE_URL}/agents/{aid}").mock(
        return_value=httpx.Response(200, json=_agent(aid))
    )
    await srv.mcp_app.call_tool(
        "update_agent",
        {"agent_id": aid, "clear_fields": ["handover_max_duration_seconds"]},
    )
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.read()) == {
        "handover_max_duration_seconds": None
    }
