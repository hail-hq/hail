"""Unit tests for HailClient upload and attachment functionality."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
from hailhq.mcp.hail_client import HailClient
from pydantic import ValidationError

_AGENT_ID = "22222222-2222-4222-8222-222222222222"
_BASE_URL = "http://hail-test"
_API_KEY = "test-key"


@pytest.fixture()
async def client() -> HailClient:
    c = HailClient(base_url=_BASE_URL, api_key=_API_KEY)
    try:
        yield c
    finally:
        await c.aclose()


def _email_response(email_id: str | None = None, status: str = "sent") -> dict:
    """Return a minimal EmailResponse-shaped dict for mocked 201s."""
    eid = email_id or str(uuid4())
    return {
        "id": eid,
        "organization_id": str(uuid4()),
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


# --------------------------------------------------------------------------- #
# upload_email_attachment
# --------------------------------------------------------------------------- #


@respx.mock
async def test_upload_email_attachment_posts_multipart(client: HailClient) -> None:
    """Test that upload_email_attachment sends multipart POST request."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["url_path"] = request.url.path
        return httpx.Response(
            201,
            json={
                "id": "11111111-1111-1111-1111-111111111111",
                "filename": "a.pdf",
                "content_type": "application/pdf",
                "size_bytes": 3,
            },
        )

    respx.post(f"{_BASE_URL}/email-attachments").mock(side_effect=handler)

    result = await client.upload_email_attachment(
        filename="a.pdf", content=b"abc", content_type="application/pdf"
    )

    assert result["filename"] == "a.pdf"
    assert result["content_type"] == "application/pdf"
    assert result["size_bytes"] == 3
    assert captured["url_path"] == "/email-attachments"
    # Verify Authorization header is present
    assert captured["request"].headers.get("authorization") == f"Bearer {_API_KEY}"


# --------------------------------------------------------------------------- #
# send_email with attachment_ids
# --------------------------------------------------------------------------- #


@respx.mock
async def test_send_email_with_attachment_ids(client: HailClient) -> None:
    """Test that send_email includes attachment_ids in request body."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_email_response())

    respx.post(f"{_BASE_URL}/emails").mock(side_effect=handler)

    att_id_1 = str(uuid4())
    att_id_2 = str(uuid4())
    result = await client.send_email(
        to=["recipient@example.com"],
        subject="Test",
        body_text="Hello",
        recipient_consent=True,
        attachment_ids=[att_id_1, att_id_2],
    )

    assert "error" not in result
    assert result["status"] == "sent"

    # Verify attachment_ids in body
    body = httpx.Response(200, content=captured["body"]).json()
    assert body["attachment_ids"] == [att_id_1, att_id_2]


@respx.mock
async def test_send_email_without_attachment_ids(client: HailClient) -> None:
    """Test that send_email omits attachment_ids when not provided."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_email_response())

    respx.post(f"{_BASE_URL}/emails").mock(side_effect=handler)

    result = await client.send_email(
        to=["recipient@example.com"],
        subject="Test",
        body_text="Hello",
        recipient_consent=True,
    )

    assert "error" not in result
    assert result["status"] == "sent"

    # Verify attachment_ids not in body when not provided
    body = httpx.Response(200, content=captured["body"]).json()
    assert "attachment_ids" not in body


# --------------------------------------------------------------------------- #
# create_agent — handover_contacts
# --------------------------------------------------------------------------- #


def _agent_response(contact_id: str) -> dict:
    return {
        "id": str(uuid4()),
        "organization_id": str(uuid4()),
        "name": "Desk",
        "system_prompt": "Help.",
        "first_message": None,
        "ai_disclosure": True,
        "ai_disclosure_line": None,
        "voice_config": {},
        "tools": None,
        "max_duration_seconds": None,
        "handover_max_duration_seconds": None,
        "voice_enabled": True,
        "sms_enabled": True,
        "status": "live",
        "handover_contacts": [
            {
                "contact_id": contact_id,
                "name": "Sam",
                "phone_e164": "+14155550100",
                "note": "billing",
            }
        ],
        "created_at": "2026-10-08T00:00:00+00:00",
        "updated_at": "2026-10-08T00:00:00+00:00",
    }


@respx.mock
async def test_create_agent_sends_handover_contacts(client: HailClient) -> None:
    captured: dict = {}
    contact_id = str(uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json=_agent_response(contact_id))

    respx.post(f"{_BASE_URL}/agents").mock(side_effect=handler)
    out = await client.create_agent(
        name="Desk",
        system_prompt="Help.",
        handover_contacts=[{"contact_id": contact_id, "note": "billing"}],
    )
    assert captured["body"]["handover_contacts"] == [
        {"contact_id": contact_id, "note": "billing"}
    ]
    assert out["handover_contacts"][0]["name"] == "Sam"


@respx.mock
async def test_create_agent_rejects_empty_handover_note(client: HailClient) -> None:
    route = respx.post(f"{_BASE_URL}/agents")
    with pytest.raises(ValidationError):
        await client.create_agent(
            name="Desk",
            system_prompt="Help.",
            handover_contacts=[{"contact_id": str(uuid4()), "note": ""}],
        )
    assert not route.called


def _email_domain_handler(seen: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        now = "2026-10-31T00:00:00+00:00"
        return httpx.Response(
            200,
            json={
                "id": str(uuid4()),
                "organization_id": str(uuid4()),
                "kind": "hail_mail",
                "domain": "amber-heron+acme@mail.hail.so",
                "local_prefix_user": "amber-heron",
                "local_prefix_org": "acme",
                "verification_status": "verified",
                "dns_records": [],
                "mail_from_domain": None,
                "provider": "ses",
                "verified_at": now,
                "created_at": now,
                "updated_at": now,
            },
        )

    return handler


@pytest.mark.parametrize(
    ("kwargs", "body"),
    [
        ({"clear_agent": True}, {"email_agent_id": None}),
        ({"email_agent_id": _AGENT_ID, "clear_agent": True}, {"email_agent_id": None}),
        ({"email_agent_id": _AGENT_ID}, {"email_agent_id": _AGENT_ID}),
        ({"email_agent_scope": "anyone"}, {"email_agent_scope": "anyone"}),
    ],
    ids=["clear", "clear-wins", "set", "scope"],
)
@respx.mock
async def test_route_email_domain_body(
    client: HailClient, kwargs: dict[str, Any], body: dict[str, Any]
) -> None:
    domain_id = str(uuid4())
    seen: dict[str, Any] = {}
    respx.patch(f"{_BASE_URL}/email-domains/{domain_id}").mock(
        side_effect=_email_domain_handler(seen)
    )
    await client.route_email_domain(domain_id, **kwargs)
    assert seen == body
