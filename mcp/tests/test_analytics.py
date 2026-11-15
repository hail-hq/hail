"""PostHog MCP analytics must not change what agents see or leak content.

Boots the HTTP app with and without a token, runs the same requests, and
checks that every response (body and session header) is identical, and that
the captured ``$mcp_tool_call`` carries metadata only.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import time

import httpx
import pytest
from hailhq.mcp import analytics

_ACCEPT = "application/json, text/event-stream"
_LEGACY = "2025-06-18"
_MODERN = "2026-07-28"
_SECRET = "+14155550123 secret body"
_AUTH = {"Authorization": "Bearer user.jwt.token"}
_ORG = "00000000-0000-0000-0000-0000000000aa"


def _body(resp: httpx.Response) -> dict:
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise AssertionError(resp.text)


def _modern(method: str, request_id: int, **params) -> tuple[dict, dict]:
    headers = {"Accept": _ACCEPT, "MCP-Protocol-Version": _MODERN, "Mcp-Method": method}
    if method == "tools/call":
        headers["Mcp-Name"] = params["name"]
    meta = {
        "io.modelcontextprotocol/protocolVersion": _MODERN,
        "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**params, "_meta": meta},
    }
    return headers, body


async def _session(
    monkeypatch, token: str, *, oauth: bool, protocol: str, fail: bool
) -> tuple[list, list]:
    monkeypatch.setattr("hailhq.core.config.settings.mcp_resource_url", "https://m.t")
    monkeypatch.setattr(
        "hailhq.core.config.settings.hail_auth_url",
        "https://hail.test/api/auth" if oauth else "",
    )
    monkeypatch.setattr(
        "hailhq.core.config.settings.hail_api_key", "" if oauth else "hl_live_test"
    )
    monkeypatch.setattr("hailhq.core.config.settings.next_public_posthog_key", token)
    # A capture that escapes the patch below goes nowhere.
    monkeypatch.setattr(
        "hailhq.core.config.settings.mcp_posthog_host", "http://127.0.0.1:9"
    )
    monkeypatch.setattr(
        "hailhq.core.config.settings.logfire_service_version", "abc1234"
    )
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "posthog.Posthog.capture",
        lambda self, event, **kw: captured.append((event, kw)),
    )

    async def regions(_self):
        if fail:
            raise RuntimeError(_SECRET)
        return {"regions": [], "note": _SECRET}

    monkeypatch.setattr(
        "hailhq.mcp.hail_client.HailClient.list_number_regions", regions
    )
    auth = _AUTH if oauth else {}
    # What _actor_for caches for this bearer when Logfire telemetry is on.
    key = hashlib.sha256(b"user.jwt.token").hexdigest()
    cache = {key: (time.monotonic() + 300, {"organization_id": _ORG})}
    monkeypatch.setattr("hailhq.mcp.tools._actor_cache", cache)
    call = {"name": "list_number_regions", "arguments": {}}

    import hailhq.mcp.server as srv

    srv = importlib.reload(srv)
    stack = [m.cls.__name__ for m in srv.app.user_middleware]
    assert "PostHogMcpStatelessSessionMiddleware" not in stack
    seen: list = []
    async with srv.app.router.lifespan_context(srv.app):
        transport = httpx.ASGITransport(app=srv.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:

            async def post(headers: dict, body: dict) -> httpx.Response:
                resp = await c.post("/", headers={**headers, **auth}, json=body)
                has_session = "mcp-session-id" in resp.headers
                seen.append((resp.status_code, has_session, resp.text and _body(resp)))
                return resp

            if protocol == _MODERN:
                await post(*_modern("tools/list", 2))
                await post(*_modern("tools/call", 3, **call))
            else:
                init = await post(
                    {"Accept": _ACCEPT},
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": _LEGACY,
                            "capabilities": {},
                            "clientInfo": {"name": "analytics-test", "version": "1.0"},
                        },
                    },
                )
                headers = {
                    "Accept": _ACCEPT,
                    "Mcp-Session-Id": init.headers["mcp-session-id"],
                    "Mcp-Protocol-Version": _LEGACY,
                }
                await c.post(
                    "/",
                    headers={**headers, **auth},
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                await post(headers, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                await post(
                    headers,
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call},
                )
    return seen, captured


@pytest.mark.asyncio
@pytest.mark.parametrize("oauth", [False, True])
@pytest.mark.parametrize("protocol", [_LEGACY, _MODERN])
@pytest.mark.parametrize("fail", [False, True])
async def test_analytics_keeps_responses_unchanged(monkeypatch, oauth, protocol, fail):
    kw = {"oauth": oauth, "protocol": protocol, "fail": fail}
    plain, plain_events = await _session(monkeypatch, "", **kw)
    seen, events = await _session(monkeypatch, "phc_test", **kw)

    assert plain_events == []
    assert seen == plain
    is_error = seen[-1][2]["result"].get("isError", False)

    calls = [e for name, e in events if name == "$mcp_tool_call"]
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "list_number_regions"
    assert props["$mcp_is_error"] is is_error
    assert props["$mcp_server_build"] == "abc1234"
    assert set(props) <= analytics._KEPT_PROPERTIES
    if oauth:
        assert calls[0]["distinct_id"] == _ORG
        assert props["$groups"] == {"organization": _ORG}
    else:
        assert "$groups" not in props
    assert _SECRET not in json.dumps([e for _, e in events], default=str)


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
