"""Unit tests for the MCP tool wrappers.

Tests target the four tool callables in :mod:`hailhq.mcp.tools`,
exercising local validation, HTTP request shape, and error mapping.
The MCP/FastMCP transport layer is not covered here — that's framework
territory; we trust the registered tools dispatch to the same callables
we test directly.
"""

from __future__ import annotations

import re
from uuid import uuid4

import httpx
import pytest
import respx

from hailhq.mcp import tools
from hailhq.mcp.hail_client import HailClient

_BASE_URL = "http://hail-test"
_API_KEY = "test-key"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@pytest.fixture()
async def client() -> HailClient:
    c = HailClient(base_url=_BASE_URL, api_key=_API_KEY)
    try:
        yield c
    finally:
        await c.aclose()


def _call_response(call_id: str | None = None, status: str = "dialing") -> dict:
    """Return a minimal CallResponse-shaped dict for mocked 201s."""
    cid = call_id or str(uuid4())
    return {
        "id": cid,
        "organization_id": str(uuid4()),
        "conversation_id": None,
        "from_e164": "+14155551234",
        "to_e164": "+14155559999",
        "direction": "outbound",
        "status": status,
        "end_reason": None,
        "provider_call_sid": "PA_test",
        "livekit_room": "hail-test",
        "initial_prompt": None,
        "recording_s3_key": None,
        "requested_at": "2026-04-22T00:00:00+00:00",
        "started_at": None,
        "answered_at": None,
        "ended_at": None,
    }


# --------------------------------------------------------------------------- #
# place_call
# --------------------------------------------------------------------------- #


@respx.mock
async def test_place_call_mode_a_happy_path(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_call_response())

    respx.post(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    result = await tools.place_call(
        client=client,
        to="+14155559999",
        system_prompt="be polite",
    )
    assert "error" not in result, result
    assert result["status"] == "dialing"

    # Auth + Idempotency-Key auto-injected.
    assert captured["headers"]["authorization"] == f"Bearer {_API_KEY}"
    assert _UUID_RE.match(captured["headers"]["idempotency-key"])

    # Mode A: system_prompt on the wire, no llm.
    body = httpx.Response(200, content=captured["body"]).json()
    assert body == {"to": "+14155559999", "system_prompt": "be polite"}


@respx.mock
async def test_place_call_mode_b_byo_endpoint(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_call_response())

    respx.post(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    result = await tools.place_call(
        client=client,
        to="+14155559999",
        llm={
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "model": "gpt-4o-mini",
        },
    )
    assert "error" not in result, result

    body = httpx.Response(200, content=captured["body"]).json()
    assert "system_prompt" not in body
    assert body["llm"] == {
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test",
        "model": "gpt-4o-mini",
    }


@respx.mock
async def test_place_call_rejects_both_modes(client: HailClient) -> None:
    route = respx.post(f"{_BASE_URL}/calls").mock(
        return_value=httpx.Response(201, json=_call_response())
    )
    result = await tools.place_call(
        client=client,
        to="+14155559999",
        system_prompt="be polite",
        llm={"base_url": "u", "api_key": "k", "model": "m"},
    )
    assert "error" in result
    assert "mutually exclusive" in result["error"]
    assert not route.called  # short-circuited before HTTP


@respx.mock
async def test_place_call_rejects_neither_mode(client: HailClient) -> None:
    route = respx.post(f"{_BASE_URL}/calls").mock(
        return_value=httpx.Response(201, json=_call_response())
    )
    result = await tools.place_call(client=client, to="+14155559999")
    assert "error" in result
    assert "must provide either" in result["error"]
    assert not route.called


@respx.mock
async def test_place_call_auto_generates_idempotency_key(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("idempotency-key")
        return httpx.Response(201, json=_call_response())

    respx.post(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    await tools.place_call(client=client, to="+14155559999", system_prompt="x")
    assert captured["key"] is not None
    assert _UUID_RE.match(captured["key"]), captured["key"]


@respx.mock
async def test_place_call_returns_idempotency_key_in_response(
    client: HailClient,
) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("idempotency-key")
        return httpx.Response(201, json=_call_response())

    respx.post(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    result = await tools.place_call(client=client, to="+14155559999", system_prompt="x")
    # The auto-generated key is surfaced so an agent can retry exactly.
    assert "idempotency_key" in result
    assert _UUID_RE.match(result["idempotency_key"])
    assert result["idempotency_key"] == captured["key"]


@respx.mock
async def test_place_call_propagates_explicit_idempotency_key(
    client: HailClient,
) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("idempotency-key")
        return httpx.Response(201, json=_call_response())

    respx.post(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    explicit = "deadbeef-dead-beef-dead-beefdeadbeef"
    result = await tools.place_call(
        client=client,
        to="+14155559999",
        system_prompt="x",
        idempotency_key=explicit,
    )
    assert captured["key"] == explicit
    assert result["idempotency_key"] == explicit


@respx.mock
async def test_place_call_llm_validation_rejects_partial(client: HailClient) -> None:
    route = respx.post(f"{_BASE_URL}/calls").mock(
        return_value=httpx.Response(201, json=_call_response())
    )
    result = await tools.place_call(
        client=client,
        to="+14155559999",
        llm={"base_url": "https://x", "api_key": "k"},  # missing model
    )
    assert "error" in result
    assert "model" in result["error"]
    assert not route.called


@respx.mock
async def test_place_call_serializes_from_alias(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_call_response())

    respx.post(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    await tools.place_call(
        client=client,
        to="+14155559999",
        system_prompt="x",
        from_="+14155550000",
    )
    body = httpx.Response(200, content=captured["body"]).json()
    assert body["from"] == "+14155550000"
    assert "from_" not in body


# --------------------------------------------------------------------------- #
# get_call
# --------------------------------------------------------------------------- #


@respx.mock
async def test_get_call_happy_path(client: HailClient) -> None:
    cid = str(uuid4())
    respx.get(f"{_BASE_URL}/calls/{cid}").mock(
        return_value=httpx.Response(200, json=_call_response(cid, status="completed"))
    )
    result = await tools.get_call(client=client, call_id=cid)
    assert result["id"] == cid
    assert result["status"] == "completed"


# --------------------------------------------------------------------------- #
# list_calls
# --------------------------------------------------------------------------- #


@respx.mock
async def test_list_calls_pagination(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    respx.get(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    await tools.list_calls(client=client, cursor="cur-abc", limit=25)
    assert "cursor=cur-abc" in captured["url"]
    assert "limit=25" in captured["url"]


# --------------------------------------------------------------------------- #
# get_events
# --------------------------------------------------------------------------- #


@respx.mock
async def test_get_events_with_id_filter(client: HailClient) -> None:
    cid = str(uuid4())
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"items": [], "next_cursor": None, "call_status": "in_progress"},
        )

    respx.get(f"{_BASE_URL}/events").mock(side_effect=_handler)

    result = await tools.get_events(client=client, id=f"call:{cid}", limit=200)
    assert "error" not in result
    assert f"id=call%3A{cid}" in captured["url"] or f"id=call:{cid}" in captured["url"]
    assert result["call_status"] == "in_progress"


@respx.mock
async def test_get_events_rejects_malformed_id(client: HailClient) -> None:
    route = respx.get(f"{_BASE_URL}/events").mock(
        return_value=httpx.Response(200, json={"items": [], "next_cursor": None})
    )
    result = await tools.get_events(client=client, id="garbage")
    assert "error" in result
    assert "<type>:<uuid>" in result["error"]
    assert not route.called


@respx.mock
async def test_get_events_rejects_unsupported_type(client: HailClient) -> None:
    route = respx.get(f"{_BASE_URL}/events").mock(
        return_value=httpx.Response(200, json={"items": [], "next_cursor": None})
    )
    result = await tools.get_events(client=client, id=f"sms:{uuid4()}")
    assert "error" in result
    assert "unsupported resource type" in result["error"]
    assert not route.called


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


@respx.mock
async def test_api_error_mapping_401(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/calls").mock(
        return_value=httpx.Response(401, json={"detail": "bad key"})
    )
    result = await tools.place_call(client=client, to="+14155559999", system_prompt="x")
    assert result == {"error": "auth failed: check HAIL_API_KEY"}


@respx.mock
async def test_api_error_mapping_404(client: HailClient) -> None:
    cid = str(uuid4())
    respx.get(f"{_BASE_URL}/calls/{cid}").mock(
        return_value=httpx.Response(404, json={"detail": "call not found"})
    )
    result = await tools.get_call(client=client, call_id=cid)
    assert result == {"error": "call not found"}


@respx.mock
async def test_api_error_mapping_422(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/calls").mock(
        return_value=httpx.Response(422, json={"detail": "phone number not registered"})
    )
    result = await tools.place_call(
        client=client,
        to="+14155559999",
        system_prompt="x",
        from_="+14155550000",
    )
    assert result == {"error": "phone number not registered"}


@respx.mock
async def test_api_error_mapping_5xx(client: HailClient) -> None:
    respx.get(f"{_BASE_URL}/events").mock(
        return_value=httpx.Response(503, text="upstream down")
    )
    result = await tools.get_events(client=client)
    assert result == {"error": "hail upstream error: 503"}


@respx.mock
async def test_api_error_mapping_409_idempotency(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/calls").mock(
        return_value=httpx.Response(
            409, json={"detail": "Idempotency-Key reused with different payload"}
        )
    )
    result = await tools.place_call(client=client, to="+14155559999", system_prompt="x")
    assert "error" in result
    assert "Idempotency-Key" in result["error"]
