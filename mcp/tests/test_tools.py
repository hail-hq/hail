"""Unit tests for the MCP tool wrappers.

Tests target the nine tool callables in :mod:`hailhq.mcp.tools`,
exercising local validation, HTTP request shape, and error mapping.
The MCP/FastMCP transport layer is not covered here — that's framework
territory; we trust the registered tools dispatch to the same callables
we test directly.
"""

from __future__ import annotations

import base64
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
        recipient_consent=True,
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
    assert body == {
        "to": "+14155559999",
        "system_prompt": "be polite",
        "recipient_consent": True,
        "message_type": "informational",
    }


@respx.mock
async def test_place_call_language_lands_in_voice_config(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_call_response())

    respx.post(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    result = await tools.place_call(
        client=client,
        recipient_consent=True,
        to="+14155559999",
        system_prompt="be polite",
        language="fr",
    )
    assert "error" not in result, result

    body = httpx.Response(200, content=captured["body"]).json()
    assert body["voice_config"] == {"language": "fr"}


async def test_place_call_rejects_bad_language(client: HailClient) -> None:
    """A non-ISO-639-1 code fails CallCreate validation before any HTTP."""
    result = await tools.place_call(
        client=client,
        recipient_consent=True,
        to="+14155559999",
        system_prompt="be polite",
        language="French",
    )
    assert "error" in result
    assert "language" in result["error"]


@respx.mock
async def test_place_call_mode_b_byo_endpoint(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_call_response())

    respx.post(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    result = await tools.place_call(
        client=client,
        recipient_consent=True,
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
        recipient_consent=True,
        to="+14155559999",
        system_prompt="be polite",
        llm={"base_url": "https://api.example.com/v1", "api_key": "k", "model": "m"},
    )
    assert "error" in result
    assert "mutually exclusive" in result["error"]
    assert not route.called  # short-circuited before HTTP


@respx.mock
async def test_place_call_rejects_neither_mode(client: HailClient) -> None:
    route = respx.post(f"{_BASE_URL}/calls").mock(
        return_value=httpx.Response(201, json=_call_response())
    )
    result = await tools.place_call(
        client=client, recipient_consent=True, to="+14155559999"
    )
    assert "error" in result
    assert "either system_prompt or llm" in result["error"]
    assert not route.called


@respx.mock
async def test_place_call_auto_generates_idempotency_key(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("idempotency-key")
        return httpx.Response(201, json=_call_response())

    respx.post(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    await tools.place_call(
        client=client, recipient_consent=True, to="+14155559999", system_prompt="x"
    )
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

    result = await tools.place_call(
        client=client, recipient_consent=True, to="+14155559999", system_prompt="x"
    )
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
        recipient_consent=True,
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
        recipient_consent=True,
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
        recipient_consent=True,
        to="+14155559999",
        system_prompt="x",
        from_="+14155550000",
    )
    body = httpx.Response(200, content=captured["body"]).json()
    assert body["from"] == "+14155550000"
    assert "from_" not in body


@respx.mock
async def test_place_call_passes_tools_to_request_body(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_call_response())

    respx.post(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    await tools.place_call(
        client=client,
        recipient_consent=True,
        to="+14155559999",
        system_prompt="x",
        tools=["send_sms"],
    )
    body = httpx.Response(200, content=captured["body"]).json()
    assert body["tools"] == ["send_sms"]


@respx.mock
async def test_place_call_omits_tools_when_none(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_call_response())

    respx.post(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    await tools.place_call(
        client=client,
        recipient_consent=True,
        to="+14155559999",
        system_prompt="x",
    )
    body = httpx.Response(200, content=captured["body"]).json()
    assert "tools" not in body


# --------------------------------------------------------------------------- #
# send_email
# --------------------------------------------------------------------------- #


def _email_response(email_id: str | None = None, status: str = "sent") -> dict:
    eid = email_id or str(uuid4())
    return {
        "id": eid,
        "organization_id": str(uuid4()),
        "conversation_id": None,
        "email_domain_id": str(uuid4()),
        "from_address": "alice+acme@mail.hail.so",
        "to_addresses": ["x@example.com"],
        "cc_addresses": None,
        "bcc_addresses": None,
        "reply_to": None,
        "subject": "hi",
        "body_text": "body",
        "body_html": None,
        "status": status,
        "end_reason": None,
        "provider_message_id": "ses-msg-1",
        "requested_at": "2026-05-17T00:00:00+00:00",
        "sent_at": "2026-05-17T00:00:01+00:00",
        "failed_at": None,
    }


@respx.mock
async def test_send_email_happy_path(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_email_response())

    respx.post(f"{_BASE_URL}/emails").mock(side_effect=_handler)

    result = await tools.send_email(
        client=client,
        recipient_consent=True,
        to=["x@example.com"],
        subject="hi",
        body_text="body",
    )
    assert "error" not in result, result
    assert result["status"] == "sent"
    assert captured["headers"]["authorization"] == f"Bearer {_API_KEY}"
    assert _UUID_RE.match(captured["headers"]["idempotency-key"])
    body = httpx.Response(200, content=captured["body"]).json()
    assert body["to"] == ["x@example.com"]
    assert body["subject"] == "hi"
    assert body["body_text"] == "body"


@respx.mock
async def test_send_email_rejects_empty_recipients(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/emails").mock(return_value=httpx.Response(201, json={}))
    result = await tools.send_email(
        client=client, recipient_consent=True, to=[], subject="hi", body_text="body"
    )
    assert "error" in result
    assert "at least 1" in result["error"]
    # respx records every call; verify no HTTP went out.
    assert not respx.calls.called


@respx.mock
async def test_send_email_requires_a_body(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/emails").mock(return_value=httpx.Response(201, json={}))
    result = await tools.send_email(
        client=client, recipient_consent=True, to=["x@example.com"], subject="hi"
    )
    assert "body_text or body_html" in result["error"]
    assert not respx.calls.called


@respx.mock
async def test_send_email_serializes_from_alias(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_email_response())

    respx.post(f"{_BASE_URL}/emails").mock(side_effect=_handler)

    await tools.send_email(
        client=client,
        recipient_consent=True,
        to=["x@example.com"],
        subject="hi",
        body_text="body",
        from_="alice+acme@mail.hail.so",
    )
    body = httpx.Response(200, content=captured["body"]).json()
    assert body["from"] == "alice+acme@mail.hail.so"
    assert "from_" not in body


@respx.mock
async def test_send_email_returns_idempotency_key_in_response(
    client: HailClient,
) -> None:
    respx.post(f"{_BASE_URL}/emails").mock(
        return_value=httpx.Response(201, json=_email_response())
    )
    result = await tools.send_email(
        client=client,
        recipient_consent=True,
        to=["x@example.com"],
        subject="hi",
        body_text="body",
    )
    assert _UUID_RE.match(result["idempotency_key"])


@respx.mock
async def test_send_email_propagates_explicit_idempotency_key(
    client: HailClient,
) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(201, json=_email_response())

    respx.post(f"{_BASE_URL}/emails").mock(side_effect=_handler)

    result = await tools.send_email(
        client=client,
        recipient_consent=True,
        to=["x@example.com"],
        subject="hi",
        body_text="body",
        idempotency_key="my-key-1",
    )
    assert result["idempotency_key"] == "my-key-1"
    assert captured["headers"]["idempotency-key"] == "my-key-1"


@respx.mock
async def test_send_email_maps_503_to_error_detail(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/emails").mock(
        return_value=httpx.Response(
            503,
            json={"detail": "hail-mail prefixes are not configured: missing ..."},
        )
    )
    result = await tools.send_email(
        client=client,
        recipient_consent=True,
        to=["x@example.com"],
        subject="hi",
        body_text="body",
    )
    assert "hail-mail prefixes" in result["error"]


@respx.mock
async def test_send_email_passes_attachment_ids(client: HailClient) -> None:
    captured: dict = {}
    aid = str(uuid4())

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_email_response())

    respx.post(f"{_BASE_URL}/emails").mock(side_effect=_handler)

    await tools.send_email(
        client=client,
        recipient_consent=True,
        to=["x@example.com"],
        subject="hi",
        body_text="body",
        attachment_ids=[aid],
    )
    body = httpx.Response(200, content=captured["body"]).json()
    assert body["attachment_ids"] == [aid]


# --------------------------------------------------------------------------- #
# upload_email_attachment
# --------------------------------------------------------------------------- #


def _attachment_response(attachment_id: str | None = None) -> dict:
    return {
        "id": attachment_id or str(uuid4()),
        "filename": "invoice.pdf",
        "content_type": "application/pdf",
        "size_bytes": 11,
    }


@respx.mock
async def test_upload_email_attachment_happy_path(client: HailClient) -> None:
    aid = str(uuid4())
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        captured["content_type"] = request.headers["content-type"]
        return httpx.Response(201, json=_attachment_response(aid))

    respx.post(f"{_BASE_URL}/email-attachments").mock(side_effect=_handler)

    result = await tools.upload_email_attachment(
        client=client,
        content_base64=base64.b64encode(b"hello world").decode("ascii"),
        filename="invoice.pdf",
        content_type="application/pdf",
    )
    assert "error" not in result, result
    assert result["id"] == aid
    assert result["filename"] == "invoice.pdf"
    assert b"hello world" in captured["body"]
    assert captured["content_type"].startswith("multipart/form-data")


@respx.mock
async def test_upload_email_attachment_rejects_invalid_base64(
    client: HailClient,
) -> None:
    result = await tools.upload_email_attachment(
        client=client,
        content_base64="not-valid-base64!!!",
        filename="invoice.pdf",
        content_type="application/pdf",
    )
    assert result == {"error": "content_base64: invalid base64 encoding"}
    assert not respx.calls.called


@respx.mock
async def test_upload_email_attachment_maps_413_to_error_detail(
    client: HailClient,
) -> None:
    respx.post(f"{_BASE_URL}/email-attachments").mock(
        return_value=httpx.Response(
            413, json={"detail": "attachment exceeds 10MB limit"}
        )
    )
    result = await tools.upload_email_attachment(
        client=client,
        content_base64=base64.b64encode(b"hello world").decode("ascii"),
        filename="invoice.pdf",
        content_type="application/pdf",
    )
    assert "10MB" in result["error"]


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
# send_sms
# --------------------------------------------------------------------------- #


def _sms_response(sms_id: str | None = None, status: str = "sent") -> dict:
    """Return a minimal SmsResponse-shaped dict for mocked 201s."""
    sid = sms_id or str(uuid4())
    return {
        "id": sid,
        "organization_id": str(uuid4()),
        "from_e164": "+14155550000",
        "to_e164": "+14155551234",
        "direction": "outbound",
        "status": status,
        "body": "hi",
        "provider_message_sid": "SM_test",
        "segment_count": 1,
        "error_code": None,
        "requested_at": "2026-07-08T00:00:00+00:00",
        "sent_at": None,
    }


@respx.mock
async def test_send_sms_happy_path(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_sms_response())

    respx.post(f"{_BASE_URL}/sms").mock(side_effect=_handler)

    result = await tools.send_sms(
        client=client,
        to="+14155551234",
        body="hi",
        recipient_consent=True,
    )
    assert "error" not in result, result
    assert result["status"] == "sent"

    # Auth + Idempotency-Key auto-injected.
    assert captured["headers"]["authorization"] == f"Bearer {_API_KEY}"
    assert _UUID_RE.match(captured["headers"]["idempotency-key"])

    body = httpx.Response(200, content=captured["body"]).json()
    assert body == {
        "to": "+14155551234",
        "body": "hi",
        "recipient_consent": True,
        "message_type": "informational",
    }


@respx.mock
async def test_send_sms_returns_idempotency_key_in_response(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("idempotency-key")
        return httpx.Response(201, json=_sms_response())

    respx.post(f"{_BASE_URL}/sms").mock(side_effect=_handler)

    result = await tools.send_sms(
        client=client, to="+14155551234", body="hi", recipient_consent=True
    )
    assert "idempotency_key" in result
    assert result["idempotency_key"] == captured["key"]


@respx.mock
async def test_send_sms_propagates_explicit_idempotency_key(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("idempotency-key")
        return httpx.Response(201, json=_sms_response())

    respx.post(f"{_BASE_URL}/sms").mock(side_effect=_handler)

    explicit = "deadbeef-dead-beef-dead-beefdeadbeef"
    result = await tools.send_sms(
        client=client,
        to="+14155551234",
        body="hi",
        recipient_consent=True,
        idempotency_key=explicit,
    )
    assert captured["key"] == explicit
    assert result["idempotency_key"] == explicit


@respx.mock
async def test_send_sms_serializes_from_alias(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_sms_response())

    respx.post(f"{_BASE_URL}/sms").mock(side_effect=_handler)

    await tools.send_sms(
        client=client,
        to="+14155551234",
        body="hi",
        recipient_consent=True,
        from_="+14155550000",
    )
    body = httpx.Response(200, content=captured["body"]).json()
    assert body["from"] == "+14155550000"
    assert "from_" not in body


@respx.mock
async def test_send_sms_rejects_bad_e164(client: HailClient) -> None:
    route = respx.post(f"{_BASE_URL}/sms").mock(
        return_value=httpx.Response(201, json=_sms_response())
    )
    result = await tools.send_sms(
        client=client, to="not-a-number", body="hi", recipient_consent=True
    )
    assert "error" in result
    assert not route.called  # short-circuited before HTTP


@respx.mock
async def test_send_sms_api_error(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/sms").mock(
        return_value=httpx.Response(403, json={"detail": "blocked"})
    )
    result = await tools.send_sms(
        client=client, to="+14155551234", body="hi", recipient_consent=True
    )
    assert result == {"error": "hail api error 403: blocked"}


# --------------------------------------------------------------------------- #
# get_sms
# --------------------------------------------------------------------------- #


@respx.mock
async def test_get_sms_happy_path(client: HailClient) -> None:
    sid = str(uuid4())
    respx.get(f"{_BASE_URL}/sms/{sid}").mock(
        return_value=httpx.Response(200, json=_sms_response(sid, status="delivered"))
    )
    result = await tools.get_sms(client=client, sms_id=sid)
    assert result["id"] == sid
    assert result["status"] == "delivered"


@respx.mock
async def test_get_sms_maps_404_to_not_found(client: HailClient) -> None:
    sid = str(uuid4())
    respx.get(f"{_BASE_URL}/sms/{sid}").mock(
        return_value=httpx.Response(404, json={"detail": "sms not found"})
    )
    result = await tools.get_sms(client=client, sms_id=sid)
    assert result == {"error": "resource not found"}


# --------------------------------------------------------------------------- #
# list_sms
# --------------------------------------------------------------------------- #


@respx.mock
async def test_list_sms_pagination(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    respx.get(f"{_BASE_URL}/sms").mock(side_effect=_handler)

    result = await tools.list_sms(client=client, cursor="cur-abc", limit=25)
    assert result["items"] == []
    assert "cursor=cur-abc" in captured["url"]
    assert "limit=25" in captured["url"]


# --------------------------------------------------------------------------- #
# get_email
# --------------------------------------------------------------------------- #


@respx.mock
async def test_get_email_happy_path(client: HailClient) -> None:
    eid = str(uuid4())
    respx.get(f"{_BASE_URL}/emails/{eid}").mock(
        return_value=httpx.Response(200, json=_email_response(eid, status="received"))
    )
    result = await tools.get_email(client=client, email_id=eid)
    assert "error" not in result, result
    assert result["id"] == eid
    assert result["status"] == "received"
    # The full row carries the body so an agent can read a reply directly.
    assert result["body_text"] == "body"


@respx.mock
async def test_get_email_maps_404_to_not_found(client: HailClient) -> None:
    eid = str(uuid4())
    respx.get(f"{_BASE_URL}/emails/{eid}").mock(
        return_value=httpx.Response(404, json={"detail": "email not found"})
    )
    result = await tools.get_email(client=client, email_id=eid)
    assert result == {"error": "resource not found"}


# --------------------------------------------------------------------------- #
# list_emails
# --------------------------------------------------------------------------- #


def _email_summary(email_id: str | None = None, direction: str = "inbound") -> dict:
    """Return a minimal EmailSummary-shaped dict (no body) for list mocks."""
    eid = email_id or str(uuid4())
    return {
        "id": eid,
        "organization_id": str(uuid4()),
        "conversation_id": None,
        "email_domain_id": str(uuid4()),
        "direction": direction,
        "from_address": "sender@example.com",
        "to_addresses": ["alice+acme@mail.hail.so"],
        "cc_addresses": None,
        "bcc_addresses": None,
        "reply_to": None,
        "subject": "re: hi",
        "status": "received",
        "end_reason": None,
        "provider_message_id": "ses-in-1",
        "requested_at": "2026-06-13T00:00:00+00:00",
        "sent_at": None,
        "failed_at": None,
    }


@respx.mock
async def test_list_emails_filters_inbound(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200, json={"items": [_email_summary()], "next_cursor": None}
        )

    respx.get(f"{_BASE_URL}/emails").mock(side_effect=_handler)

    result = await tools.list_emails(
        client=client, direction="inbound", status="received"
    )
    assert "error" not in result, result
    assert result["items"][0]["direction"] == "inbound"
    # The filters that close the inbound-reply blind spot reach the wire.
    assert "direction=inbound" in captured["url"]
    assert "status=received" in captured["url"]


@respx.mock
async def test_list_emails_pagination(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    respx.get(f"{_BASE_URL}/emails").mock(side_effect=_handler)

    await tools.list_emails(client=client, cursor="cur-xyz", limit=10)
    assert "cursor=cur-xyz" in captured["url"]
    assert "limit=10" in captured["url"]


# --------------------------------------------------------------------------- #
# get_email_raw
# --------------------------------------------------------------------------- #

_PRESIGNED = (
    "https://s3.eu-west-1.amazonaws.com/hail-inbound/raw/abc"
    "?X-Amz-Signature=deadbeef&X-Amz-Expires=300"
)


@respx.mock
async def test_get_email_raw_returns_presigned_url(client: HailClient) -> None:
    eid = str(uuid4())
    respx.get(f"{_BASE_URL}/emails/{eid}/raw").mock(
        return_value=httpx.Response(302, headers={"location": _PRESIGNED})
    )
    result = await tools.get_email_raw(client=client, email_id=eid)
    assert "error" not in result, result
    # The tool returns the presigned URL, not the bytes — no redirect follow.
    assert result["url"] == _PRESIGNED


@respx.mock
async def test_get_email_raw_maps_404_to_not_found(client: HailClient) -> None:
    eid = str(uuid4())
    respx.get(f"{_BASE_URL}/emails/{eid}/raw").mock(
        return_value=httpx.Response(404, json={"detail": "raw MIME not available"})
    )
    result = await tools.get_email_raw(client=client, email_id=eid)
    assert result == {"error": "resource not found"}


@respx.mock
async def test_get_email_raw_redirect_without_location_errors(
    client: HailClient,
) -> None:
    # Defensive: a 3xx with no Location header must surface an error, never
    # a {"url": None}. Production always sets Location; this guards the path.
    eid = str(uuid4())
    respx.get(f"{_BASE_URL}/emails/{eid}/raw").mock(return_value=httpx.Response(302))
    result = await tools.get_email_raw(client=client, email_id=eid)
    assert "error" in result
    assert "redirect without Location" in result["error"]


# --------------------------------------------------------------------------- #
# get_email_attachment
# --------------------------------------------------------------------------- #


@respx.mock
async def test_get_email_attachment_returns_presigned_url(client: HailClient) -> None:
    eid = str(uuid4())
    aid = str(uuid4())
    respx.get(f"{_BASE_URL}/emails/{eid}/attachments/{aid}").mock(
        return_value=httpx.Response(302, headers={"location": _PRESIGNED})
    )
    result = await tools.get_email_attachment(
        client=client, email_id=eid, attachment_id=aid
    )
    assert "error" not in result, result
    assert result["url"] == _PRESIGNED


@respx.mock
async def test_get_email_attachment_maps_404_to_not_found(client: HailClient) -> None:
    eid = str(uuid4())
    aid = str(uuid4())
    respx.get(f"{_BASE_URL}/emails/{eid}/attachments/{aid}").mock(
        return_value=httpx.Response(404, json={"detail": "attachment not found"})
    )
    result = await tools.get_email_attachment(
        client=client, email_id=eid, attachment_id=aid
    )
    assert result == {"error": "resource not found"}


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
async def test_get_events_accepts_sms_resource_type(client: HailClient) -> None:
    route = respx.get(f"{_BASE_URL}/events").mock(
        return_value=httpx.Response(200, json={"items": [], "next_cursor": None})
    )
    result = await tools.get_events(client=client, id=f"sms:{uuid4()}")
    assert "error" not in result
    assert route.called


@respx.mock
async def test_get_events_rejects_unsupported_type(client: HailClient) -> None:
    route = respx.get(f"{_BASE_URL}/events").mock(
        return_value=httpx.Response(200, json={"items": [], "next_cursor": None})
    )
    result = await tools.get_events(client=client, id=f"fax:{uuid4()}")
    assert "error" in result
    assert "unsupported resource type" in result["error"]
    assert not route.called


# --------------------------------------------------------------------------- #
# get_email_events
# --------------------------------------------------------------------------- #


def _email_event(kind: str = "delivered", email_id: str | None = None) -> dict:
    return {
        "id": str(uuid4()),
        "email_id": email_id or str(uuid4()),
        "kind": kind,
        "payload": {},
        "occurred_at": "2026-06-28T10:00:00+00:00",
    }


@respx.mock
async def test_get_email_events_happy_path(client: HailClient) -> None:
    eid = str(uuid4())
    respx.get(f"{_BASE_URL}/emails/{eid}/events").mock(
        return_value=httpx.Response(
            200, json={"items": [_email_event("delivered", eid)]}
        )
    )
    result = await tools.get_email_events(client=client, email_id=eid)
    assert "error" not in result, result
    assert result["items"][0]["kind"] == "delivered"


@respx.mock
async def test_get_email_events_maps_404_to_not_found(client: HailClient) -> None:
    eid = str(uuid4())
    respx.get(f"{_BASE_URL}/emails/{eid}/events").mock(
        return_value=httpx.Response(404, json={"detail": "email not found"})
    )
    result = await tools.get_email_events(client=client, email_id=eid)
    assert result == {"error": "resource not found"}


@respx.mock
async def test_get_email_events_pagination_params(client: HailClient) -> None:
    eid = str(uuid4())
    route = respx.get(f"{_BASE_URL}/emails/{eid}/events").mock(
        return_value=httpx.Response(200, json={"items": [], "next_cursor": "cur-2"})
    )
    result = await tools.get_email_events(
        client=client, email_id=eid, cursor="cur-1", limit=50
    )
    assert result["next_cursor"] == "cur-2"
    req = route.calls.last.request
    assert req.url.params["cursor"] == "cur-1"
    assert req.url.params["limit"] == "50"

    # Omitted params must not be sent.
    await tools.get_email_events(client=client, email_id=eid)
    req2 = route.calls.last.request
    assert "cursor" not in req2.url.params
    assert "limit" not in req2.url.params


# --------------------------------------------------------------------------- #
# get_email_stats
# --------------------------------------------------------------------------- #


def _email_stats(sent: int = 2) -> dict:
    return {
        "from": "2026-06-28T00:00:00+00:00",
        "to": "2026-06-30T00:00:00+00:00",
        "bucket": "day",
        "totals": {"sent": sent, "delivered": 1},
        "rates": {"delivery": 0.5},
        "series": [],
    }


@respx.mock
async def test_get_email_stats_passthrough(client: HailClient) -> None:
    respx.get(f"{_BASE_URL}/emails/stats").mock(
        return_value=httpx.Response(200, json=_email_stats(sent=2))
    )
    result = await tools.get_email_stats(client=client)
    assert "error" not in result, result
    assert result["totals"]["sent"] == 2


@respx.mock
async def test_get_email_stats_wires_from_to_bucket_params(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_email_stats())

    respx.get(f"{_BASE_URL}/emails/stats").mock(side_effect=_handler)

    await tools.get_email_stats(
        client=client,
        from_="2026-06-28T00:00:00Z",
        to="2026-06-30T00:00:00Z",
        bucket="hour",
    )
    assert "from=2026-06-28T00%3A00%3A00Z" in captured["url"]
    assert "to=2026-06-30T00%3A00%3A00Z" in captured["url"]
    assert "bucket=hour" in captured["url"]
    assert "from_=" not in captured["url"]


@respx.mock
async def test_get_email_stats_defaults_bucket_day(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_email_stats())

    respx.get(f"{_BASE_URL}/emails/stats").mock(side_effect=_handler)

    await tools.get_email_stats(client=client)
    assert "bucket=day" in captured["url"]
    assert "from=" not in captured["url"]
    assert "to=" not in captured["url"]


@respx.mock
async def test_get_email_stats_maps_422_to_error_detail(client: HailClient) -> None:
    respx.get(f"{_BASE_URL}/emails/stats").mock(
        return_value=httpx.Response(422, json={"detail": "'from' must be before 'to'"})
    )
    result = await tools.get_email_stats(client=client, from_="bad")
    assert "must be before" in result["error"]


# --------------------------------------------------------------------------- #
# contacts
# --------------------------------------------------------------------------- #


def _contact_entry(
    contact_id: str | None = None,
    kind: str = "manual",
    name: str = "Maya Chen",
    phone_e164: str | None = "+14155551234",
    email: str | None = None,
) -> dict:
    return {
        "id": contact_id or str(uuid4()),
        "kind": kind,
        "name": name,
        "phone_e164": phone_e164,
        "email": email,
        "role": None,
    }


@respx.mock
async def test_list_contacts_calls_get_contacts(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"items": [_contact_entry()]})

    respx.get(f"{_BASE_URL}/contacts").mock(side_effect=_handler)

    result = await tools.list_contacts(client=client)
    assert "error" not in result, result
    assert result["items"][0]["name"] == "Maya Chen"
    assert captured["url"] == f"{_BASE_URL}/contacts"


@respx.mock
async def test_lookup_contact_passes_q_and_limit_10(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"items": [_contact_entry(name="maya")]})

    respx.get(f"{_BASE_URL}/contacts").mock(side_effect=_handler)

    result = await tools.lookup_contact(client=client, query="maya")
    assert "error" not in result, result
    assert "q=maya" in captured["url"]
    assert "limit=10" in captured["url"]


@respx.mock
async def test_lookup_contact_blank_query_rejected_before_client(
    client: HailClient,
) -> None:
    """A blank/whitespace query must not hit the API at all — no route
    mocked here, so a request escaping the guard would fail the test."""
    result = await tools.lookup_contact(client=client, query="   ")
    assert result == {
        "error": "query must be a non-empty name, email, or phone fragment"
    }


@respx.mock
async def test_create_contact_posts_body_and_surfaces_409(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(
            409, json={"detail": "a contact with that phone or email already exists"}
        )

    respx.post(f"{_BASE_URL}/contacts").mock(side_effect=_handler)

    result = await tools.create_contact(
        client=client, name="Maya Chen", phone_e164="+14155551234"
    )

    body = httpx.Response(200, content=captured["body"]).json()
    assert body == {"name": "Maya Chen", "phone_e164": "+14155551234"}

    assert result == {"error": "a contact with that phone or email already exists"}


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


@respx.mock
async def test_api_error_mapping_401(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/calls").mock(
        return_value=httpx.Response(401, json={"detail": "bad key"})
    )
    result = await tools.place_call(
        client=client, recipient_consent=True, to="+14155559999", system_prompt="x"
    )
    assert result == {"error": "auth failed: token rejected by Hail API"}


@respx.mock
async def test_api_error_mapping_404(client: HailClient) -> None:
    cid = str(uuid4())
    respx.get(f"{_BASE_URL}/calls/{cid}").mock(
        return_value=httpx.Response(404, json={"detail": "call not found"})
    )
    result = await tools.get_call(client=client, call_id=cid)
    # Generic "resource not found" — the same mapping serves /calls,
    # /emails, /email-domains. Specific resource type is in the request.
    assert result == {"error": "resource not found"}


@respx.mock
async def test_api_error_mapping_422(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/calls").mock(
        return_value=httpx.Response(422, json={"detail": "phone number not registered"})
    )
    result = await tools.place_call(
        client=client,
        recipient_consent=True,
        to="+14155559999",
        system_prompt="x",
        from_="+14155550000",
    )
    assert result == {"error": "phone number not registered"}


@respx.mock
async def test_api_error_mapping_5xx(client: HailClient) -> None:
    # 502/504 take the generic "hail upstream error: <code>" branch —
    # the body is provider noise the agent can't act on.
    respx.get(f"{_BASE_URL}/events").mock(
        return_value=httpx.Response(502, text="upstream down")
    )
    result = await tools.get_events(client=client)
    assert result == {"error": "hail upstream error: 502"}


@respx.mock
async def test_api_error_mapping_503_surfaces_detail(client: HailClient) -> None:
    # 503 is reserved for "operator-configurable preconditions failed"
    # (e.g. hail-mail prefixes unset, pool exhausted). The detail tells
    # the agent which knob to turn, so we surface it verbatim.
    respx.post(f"{_BASE_URL}/calls").mock(
        return_value=httpx.Response(
            503, json={"detail": "shared call line pool exhausted; try again shortly"}
        )
    )
    result = await tools.place_call(
        client=client, recipient_consent=True, to="+14155559999", system_prompt="x"
    )
    assert "pool exhausted" in result["error"]


@respx.mock
async def test_api_error_mapping_409_idempotency(client: HailClient) -> None:
    respx.post(f"{_BASE_URL}/calls").mock(
        return_value=httpx.Response(
            409, json={"detail": "Idempotency-Key reused with different payload"}
        )
    )
    result = await tools.place_call(
        client=client, recipient_consent=True, to="+14155559999", system_prompt="x"
    )
    assert "error" in result
    assert "Idempotency-Key" in result["error"]
