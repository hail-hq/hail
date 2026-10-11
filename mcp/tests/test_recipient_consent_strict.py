"""recipient_consent is a legal attestation: only the JSON boolean true counts.

Tests go through ``srv.mcp_app.call_tool``, the path a real client uses.
FastMCP validates in pydantic lax mode, so a plain ``bool`` would turn
"true", "yes", 1 and "on" into True.
"""

from __future__ import annotations

import importlib
import json

import httpx
import pytest
import respx

_BASE_URL = "http://hail-test"

_ARGS = {
    "place_call": {"to": "+14155559999", "system_prompt": "SECRET-PROMPT"},
    "send_sms": {"to": "+14155559999", "body": "SECRET-BODY"},
    "send_email": {
        "to": ["secret@example.com"],
        "subject": "SECRET-SUBJECT",
        "body_text": "SECRET-BODY",
    },
}
_PATHS = {"place_call": "/calls", "send_sms": "/sms", "send_email": "/emails"}
_SECRETS = ("SECRET-PROMPT", "SECRET-BODY", "SECRET-SUBJECT", "secret@example.com")


async def _real_server(monkeypatch):
    monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "hl_live_test")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_url", _BASE_URL)
    import hailhq.mcp.server as srv

    return importlib.reload(srv)


@respx.mock
@pytest.mark.parametrize("tool", list(_ARGS))
@pytest.mark.parametrize("value", ["true", "yes", "1", "on", 1, 0, None])
async def test_non_boolean_consent_is_refused_without_http(
    monkeypatch, tool, value
) -> None:
    srv = await _real_server(monkeypatch)
    route = respx.route().mock(return_value=httpx.Response(500))
    try:
        result = await srv.mcp_app.call_tool(
            tool, {**_ARGS[tool], "recipient_consent": value}
        )
    except Exception as exc:  # a schema refusal is a refusal
        result = str(exc)
    assert route.call_count == 0
    for secret in _SECRETS:
        assert secret not in str(result)


@respx.mock
@pytest.mark.parametrize("tool", list(_ARGS))
async def test_true_passes_and_body_carries_true(monkeypatch, tool) -> None:
    srv = await _real_server(monkeypatch)
    route = respx.post(f"{_BASE_URL}{_PATHS[tool]}").mock(
        return_value=httpx.Response(500, json={"detail": "stop"})
    )
    await srv.mcp_app.call_tool(tool, {**_ARGS[tool], "recipient_consent": True})
    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    assert body["recipient_consent"] is True


@respx.mock
@pytest.mark.parametrize("tool", list(_ARGS))
async def test_false_still_reaches_the_api(monkeypatch, tool) -> None:
    """False is a valid boolean; the API owns the 422 for it."""
    srv = await _real_server(monkeypatch)
    route = respx.post(f"{_BASE_URL}{_PATHS[tool]}").mock(
        return_value=httpx.Response(422, json={"detail": "consent required"})
    )
    await srv.mcp_app.call_tool(tool, {**_ARGS[tool], "recipient_consent": False})
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content)["recipient_consent"] is False


@pytest.mark.parametrize("tool", list(_ARGS))
async def test_consent_schema_is_still_boolean(monkeypatch, tool) -> None:
    srv = await _real_server(monkeypatch)
    listed = {t.name: t for t in await srv.mcp_app.list_tools()}
    prop = listed[tool].inputSchema["properties"]["recipient_consent"]
    assert prop["type"] == "boolean"
    assert "recipient_consent" in listed[tool].inputSchema["required"]
