"""PostHog MCP analytics must not change what agents see or leak content.

Boots the HTTP app with and without a token, runs the same requests, and
checks that every response (body and session header) is identical, and that
the captured ``$mcp_tool_call`` carries metadata only.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import httpx
import pytest
from hailhq.mcp import analytics

_ACCEPT = "application/json, text/event-stream"
_LEGACY = "2025-06-18"
_MODERN = "2026-07-28"
_SECRET = "+14155550123 secret body"
_AUTH = {"Authorization": "Bearer user.jwt.token"}
_ORG = "00000000-0000-0000-0000-0000000000aa"
_USER = "00000000-0000-0000-0000-0000000000bb"
_EMAIL = "actor@example.org"
_NAME = "Example Actor"
_MODEL = "claude-test"


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
    monkeypatch,
    token: str,
    *,
    oauth: bool,
    protocol: str,
    fail: bool,
    call: dict | None = None,
    actor: dict | None = None,
    auth: dict | None = None,
    lookup_fails: bool = False,
    followup_actors: list[dict | None] | None = None,
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
    # The reload below sets this flag; monkeypatch puts it back after the test.
    monkeypatch.setattr("hailhq.mcp.tools._analytics_actor", False)
    monkeypatch.setattr("hailhq.mcp.tools.telemetry_enabled", lambda: False)
    # A capture that escapes the patch below goes nowhere.
    monkeypatch.setattr(
        "hailhq.core.config.settings.next_public_posthog_host", "http://127.0.0.1:9"
    )
    monkeypatch.setattr(
        "hailhq.core.config.settings.logfire_service_version", "abc1234"
    )
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "posthog.Posthog.capture",
        lambda self, event, **kw: captured.append((event, kw)),
    )

    pause_call = False
    entered = asyncio.Event()
    release = asyncio.Event()

    async def regions(_self):
        if pause_call:
            entered.set()
            await release.wait()
        if fail:
            raise RuntimeError(_SECRET)
        return {"regions": [], "note": _SECRET}

    monkeypatch.setattr(
        "hailhq.mcp.hail_client.HailClient.list_number_regions", regions
    )
    auth = (_AUTH if oauth else {}) if auth is None else auth
    expected_lookups = (
        [next(iter(auth.values())).split(" ", 1)[1]] if token and oauth and auth else []
    )
    actor = (
        actor
        if actor is not None
        else {
            "auth_kind": "jwt",
            "organization_id": _ORG,
            "user_id": _USER,
            "email": _EMAIL,
            "name": _NAME,
        }
    )
    lookups = []

    async def whoami(client):
        lookups.append(client._api_key)
        if lookup_fails:
            raise httpx.ConnectError("identity API unavailable")
        return actor

    monkeypatch.setattr("hailhq.mcp.hail_client.HailClient.whoami", whoami)
    monkeypatch.setattr("hailhq.mcp.tools._actor_cache", {})
    # Analytics adds an optional llm_model input; the agent fills it.
    args = {"llm_model": _MODEL} if token else {}
    call = call or {"name": "list_number_regions", "arguments": args}

    # Imported here: building the app at import needs the auth settings above.
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
            pending = None
            if followup_actors:
                # Keep A's tool running while B and an anonymous caller list.
                pause_call = True
                pending = asyncio.create_task(post(*_modern("tools/call", 9, **call)))
                await asyncio.wait_for(entered.wait(), 5)
            try:
                for i, next_actor in enumerate(followup_actors or []):
                    auth = {}
                    if next_actor is not None:
                        actor = next_actor
                        auth = {"Authorization": f"Bearer followup-{i}"}
                        expected_lookups.append(f"followup-{i}")
                    await post(*_modern("tools/list", 10 + i))
            finally:
                release.set()
                if pending:
                    await pending
    # Initialize, list, and call must share each verified bearer lookup.
    assert lookups == expected_lookups
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
    # The only difference agents see: an optional llm_model input on each tool.
    listed = [
        r for r in seen if isinstance(r[2], dict) and "tools" in r[2].get("result", {})
    ]
    assert listed
    for _, _, body in listed:
        for tool in body["result"]["tools"]:
            props = tool["inputSchema"].get("properties", {})
            model = props.pop("llm_model")
            assert model == {
                "type": "string",
                "description": analytics._MODEL_DESCRIPTION,
            }
            assert "llm_model" not in tool["inputSchema"].get("required", [])
    assert seen == plain
    is_error = seen[-1][2]["result"].get("isError", False)

    calls = [e for name, e in events if name == "$mcp_tool_call"]
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "list_number_regions"
    assert props["$mcp_is_error"] is is_error
    assert props["$mcp_server_build"] == "abc1234"
    assert props["$mcp_llm_model"] == _MODEL
    assert props["$mcp_llm_model_source"] == "self_reported"
    assert set(props) <= analytics._KEPT_PROPERTIES
    if oauth:
        assert calls[0]["distinct_id"] == _USER
        assert props["$groups"] == {"organization": _ORG}
        # PostHog omits the flag for identified users; processing defaults on.
        assert props.get("$process_person_profile", True) is True
        assert props["$set"] == {"email": _EMAIL, "name": _NAME}
        for name, event in events:
            if name in {"$mcp_initialize", "$mcp_tools_list"}:
                assert event["distinct_id"] == _USER
                assert event["properties"]["$set"] == {"email": _EMAIL, "name": _NAME}
    else:
        assert "$groups" not in props
        assert props["$process_person_profile"] is False
    assert _SECRET not in json.dumps([e for _, e in events], default=str)


def test_keep_metadata_drops_content():
    event = {
        "properties": {
            analytics._REQUEST_IDENTITY: None,
            "$mcp_tool_name": "send_sms",
            "$mcp_parameters": {"body": _SECRET},
            "$mcp_response": _SECRET,
            "$mcp_error_message": _SECRET,
            "$mcp_intent": _SECRET,
        }
    }
    assert analytics._keep_metadata(event)["properties"] == {
        "$mcp_tool_name": "send_sms",
        "$process_person_profile": False,
    }


def test_keep_metadata_limits_person_properties():
    event = {
        "distinct_id": "ses_shared",
        "properties": {
            analytics._REQUEST_IDENTITY: {
                "distinct_id": _USER,
                "properties": {"email": _EMAIL, "name": _NAME, "body": _SECRET},
                "groups": None,
            },
            "$mcp_parameters": {"email": _SECRET},
        },
    }
    kept = analytics._keep_metadata(event)
    assert kept["distinct_id"] == _USER
    assert kept["properties"] == {"$set": {"email": _EMAIL, "name": _NAME}}


def test_missing_request_identity_drops_sticky_sdk_profile():
    # The SDK swallows event_properties callback errors. Without a snapshot,
    # its remaining identity could belong to a previous request.
    event = {
        "event": "$mcp_tool_call",
        "distinct_id": _USER,
        "properties": {
            "$session_id": "ses_shared",
            "$set": {"email": _EMAIL, "name": _NAME},
        },
    }
    assert analytics._keep_metadata(event) is None


@pytest.mark.parametrize("name", ["$exception", "custom_event"])
def test_any_event_without_request_identity_is_dropped(name):
    event = {
        "event": name,
        "distinct_id": _USER,
        "properties": {"$set": {"email": _EMAIL, "name": _NAME}},
    }
    assert analytics._keep_metadata(event) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [_LEGACY, _MODERN])
@pytest.mark.parametrize("lookup_fails", [False, True])
async def test_unresolved_user_stays_personless(monkeypatch, protocol, lookup_fails):
    _, events = await _session(
        monkeypatch,
        "phc_test",
        oauth=True,
        protocol=protocol,
        fail=False,
        actor={"organization_id": _ORG, "user_id": None, "email": None, "name": None},
        lookup_fails=lookup_fails,
    )
    for _, event in events:
        assert event["distinct_id"] not in {_USER, _ORG}
        assert event["properties"]["$process_person_profile"] is False
        assert "$set" not in event["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [_LEGACY, _MODERN])
async def test_anonymous_discovery_does_not_resolve_a_user(monkeypatch, protocol):
    _, events = await _session(
        monkeypatch, "phc_test", oauth=True, protocol=protocol, fail=False, auth={}
    )
    assert events
    for _, event in events:
        assert event["properties"]["$process_person_profile"] is False
        assert "$set" not in event["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile", [{"email": _EMAIL, "name": _NAME}, {"email": None, "name": None}]
)
async def test_api_key_owner_is_identified(monkeypatch, profile):
    _, events = await _session(
        monkeypatch,
        "phc_test",
        oauth=True,
        protocol=_MODERN,
        fail=False,
        auth={"Authorization": "Bearer hl_live_owner"},
        actor={
            "auth_kind": "apikey",
            "organization_id": _ORG,
            "user_id": _USER,
            **profile,
        },
    )
    calls = [event for name, event in events if name == "$mcp_tool_call"]
    assert calls[0]["distinct_id"] == _USER
    assert calls[0]["properties"].get("$process_person_profile", True) is True
    if profile["email"]:
        assert calls[0]["properties"]["$set"] == {"email": _EMAIL, "name": _NAME}
    else:
        assert "$set" not in calls[0]["properties"]


@pytest.mark.asyncio
async def test_stateless_requests_do_not_share_person_profiles(monkeypatch):
    other_user = "00000000-0000-0000-0000-0000000000cc"
    _, events = await _session(
        monkeypatch,
        "phc_test",
        oauth=True,
        protocol=_MODERN,
        fail=False,
        followup_actors=[{"user_id": other_user, "email": None, "name": None}, None],
    )
    lists = [event for name, event in events if name == "$mcp_tools_list"]
    assert len(lists) == 3
    assert lists[0]["distinct_id"] == _USER
    assert lists[0]["properties"]["$set"] == {"email": _EMAIL, "name": _NAME}
    assert lists[1]["distinct_id"] == other_user
    assert "$set" not in lists[1]["properties"]
    assert "$groups" not in lists[1]["properties"]
    assert lists[2]["distinct_id"] not in {_USER, other_user}
    assert lists[2]["properties"]["$process_person_profile"] is False
    assert "$set" not in lists[2]["properties"]
    calls = [event for name, event in events if name == "$mcp_tool_call"]
    assert len(calls) == 2
    for event in calls:
        assert event["distinct_id"] == _USER
        assert event["properties"]["$set"] == {"email": _EMAIL, "name": _NAME}


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "phc_test"])
async def test_llm_model_argument_never_reaches_the_tool(monkeypatch, token):
    email_id = "00000000-0000-0000-0000-0000000000cc"
    received: list = []

    async def get_email(_self, email_id):
        received.append(email_id)
        return {"id": email_id}

    monkeypatch.setattr("hailhq.mcp.hail_client.HailClient.get_email", get_email)
    call = {
        "name": "get_email",
        "arguments": {"email_id": email_id, "llm_model": _MODEL},
    }
    seen, _ = await _session(
        monkeypatch, token, oauth=False, protocol=_LEGACY, fail=False, call=call
    )
    assert received == [email_id]
    assert not seen[-1][2]["result"].get("isError"), seen[-1]


@pytest.mark.parametrize(
    ("model", "kept"),
    [
        ("claude-opus-4-8", True),
        ("openai/gpt-5.2", True),
        ("+14155550123", False),
        ("me@example.com", False),
        ("call my mom", False),
    ],
)
def test_keep_metadata_keeps_only_model_ids(model, kept):
    event = {
        "properties": {
            analytics._REQUEST_IDENTITY: None,
            "$mcp_llm_model": model,
            "$mcp_llm_model_source": "self_reported",
        }
    }
    props = analytics._keep_metadata(event)["properties"]
    assert ("$mcp_llm_model" in props) is kept
    assert ("$mcp_llm_model_source" in props) is kept
