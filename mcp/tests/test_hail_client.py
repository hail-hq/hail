"""Unit tests for HailClient upload and attachment functionality."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx
from hailhq.mcp.hail_client import HailClient

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
