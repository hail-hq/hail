"""``create_agent`` and ``update_agent`` pass ``http_tools`` through, and the
one-time ``http_tool_secret`` reaches the caller."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import respx

from tests.test_update_agent_handover import _BASE_URL, _agent, _real_server

_TOOL = {
    "name": "lookup_order",
    "description": "Find an order.",
    "url": "https://api.example.com/orders",
    "headers": {"Authorization": "Bearer x"},
}


def _result(out) -> dict:
    return out.structured_content


@respx.mock
async def test_create_agent_sends_http_tools_and_returns_the_secret(
    monkeypatch,
) -> None:
    srv = await _real_server(monkeypatch)
    aid = str(uuid4())
    body = _agent(aid) | {
        "http_tools": [
            {
                **{k: v for k, v in _TOOL.items() if k != "headers"},
                "parameters": {"type": "object", "properties": {}},
                "header_names": ["Authorization"],
                "timeout_seconds": 5,
                "wait_message": None,
            }
        ],
        "http_tool_secret": "hts_once",
    }
    route = respx.post(f"{_BASE_URL}/agents").mock(
        return_value=httpx.Response(201, json=body)
    )
    out = await srv.mcp_app.call_tool(
        "create_agent",
        {"name": "Front desk", "system_prompt": "Answer.", "http_tools": [_TOOL]},
    )
    sent = json.loads(route.calls[0].request.read())
    assert sent["http_tools"][0]["headers"] == {"Authorization": "Bearer x"}
    result = _result(out)
    assert result["http_tool_secret"] == "hts_once"
    assert result["http_tools"][0]["header_names"] == ["Authorization"]


@respx.mock
async def test_update_agent_replaces_http_tools(monkeypatch) -> None:
    srv = await _real_server(monkeypatch)
    aid = str(uuid4())
    route = respx.patch(f"{_BASE_URL}/agents/{aid}").mock(
        return_value=httpx.Response(200, json=_agent(aid))
    )
    await srv.mcp_app.call_tool("update_agent", {"agent_id": aid, "http_tools": []})
    assert json.loads(route.calls[0].request.read()) == {"http_tools": []}


async def test_bad_http_tool_is_refused_before_any_request(monkeypatch) -> None:
    srv = await _real_server(monkeypatch)
    out = await srv.mcp_app.call_tool(
        "create_agent",
        {
            "name": "Front desk",
            "system_prompt": "Answer.",
            "http_tools": [{**_TOOL, "url": "http://api.example.com/orders"}],
        },
    )
    assert "https" in _result(out)["error"]
