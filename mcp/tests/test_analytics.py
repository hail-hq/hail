"""PostHog MCP analytics must not change what agents see or leak content.

Boots the HTTP app with and without a token, runs the same session, and
checks that ``tools/list`` and a tool result are identical, and that the
captured ``$mcp_tool_call`` carries metadata only.
"""

from __future__ import annotations

import importlib
import json

import httpx
import pytest
from hailhq.mcp import analytics

_ACCEPT = "application/json, text/event-stream"
_SECRET = "+14155550123 secret body"


def _body(resp: httpx.Response) -> dict:
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise AssertionError(resp.text)


async def _session(monkeypatch, token: str) -> tuple[dict, dict, list]:
    monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "hl_live_test")
    monkeypatch.setattr("hailhq.core.config.settings.mcp_posthog_project_token", token)
    monkeypatch.setattr(
        "hailhq.core.config.settings.logfire_service_version", "abc1234"
    )
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "posthog.Posthog.capture",
        lambda self, event, **kw: captured.append((event, kw)),
    )

    async def regions(_self):
        return {"regions": [], "note": _SECRET}

    monkeypatch.setattr(
        "hailhq.mcp.hail_client.HailClient.list_number_regions", regions
    )

    import hailhq.mcp.server as srv

    srv = importlib.reload(srv)
    async with srv.app.router.lifespan_context(srv.app):
        transport = httpx.ASGITransport(app=srv.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            init = await c.post(
                "/",
                headers={"Accept": _ACCEPT},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "analytics-test", "version": "1.0"},
                    },
                },
            )
            headers = {
                "Accept": _ACCEPT,
                "Mcp-Session-Id": init.headers["mcp-session-id"],
                "Mcp-Protocol-Version": "2025-06-18",
            }
            await c.post(
                "/",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            listed = _body(
                await c.post(
                    "/",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                )
            )
            called = _body(
                await c.post(
                    "/",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "list_number_regions", "arguments": {}},
                    },
                )
            )
    return listed, called, captured


@pytest.mark.asyncio
async def test_analytics_keeps_tools_and_results_unchanged(monkeypatch):
    plain_list, plain_call, plain_events = await _session(monkeypatch, "")
    listed, called, events = await _session(monkeypatch, "phc_test")

    assert plain_events == []
    assert listed == plain_list
    assert called == plain_call

    calls = [kw for name, kw in events if name == "$mcp_tool_call"]
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "list_number_regions"
    assert props["$mcp_server_build"] == "abc1234"
    assert set(props) <= analytics._KEPT_PROPERTIES
    assert _SECRET not in json.dumps([kw for _, kw in events], default=str)


def test_keep_metadata_drops_content():
    event = {
        "properties": {
            "$mcp_tool_name": "send_sms",
            "$mcp_parameters": {"body": _SECRET},
            "$mcp_response": _SECRET,
            "$mcp_error_message": _SECRET,
            "$mcp_intent": _SECRET,
        }
    }
    assert analytics._keep_metadata(event)["properties"] == {
        "$mcp_tool_name": "send_sms"
    }
