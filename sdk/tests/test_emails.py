"""End-to-end client tests for the `/emails` surface."""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
import respx

from hail import Client, EmailCreate
from tests.conftest import make_email_response

# --------------------------------------------------------------------------- #
# emails.create
# --------------------------------------------------------------------------- #


@respx.mock
async def test_emails_create_happy_path(base_url: str, api_key: str) -> None:
    payload = make_email_response()
    route = respx.post(f"{base_url}/emails").mock(
        return_value=httpx.Response(201, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        email = await c.emails.create(
            to=["recipient@example.com"],
            subject="test subject",
            body_text="test body",
            idempotency_key="idem-fixed",
        )
    assert str(email.id) == payload["id"]
    assert email.status == "sent"
    assert email.from_address == "alice+acme@mail.hail.so"

    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {api_key}"
    assert req.headers["Idempotency-Key"] == "idem-fixed"
    body = json.loads(req.content)
    assert body == {
        "to": ["recipient@example.com"],
        "subject": "test subject",
        "body_text": "test body",
    }


@respx.mock
async def test_emails_create_auto_generates_idempotency_key(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/emails").mock(
        return_value=httpx.Response(201, json=make_email_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.emails.create(to=["x@example.com"], subject="hi", body_text="body")
    raw = route.calls.last.request.headers["Idempotency-Key"]
    UUID(raw)  # raises if malformed


@respx.mock
async def test_emails_create_serializes_from_alias(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/emails").mock(
        return_value=httpx.Response(201, json=make_email_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.emails.create(
            to=["x@example.com"],
            subject="hi",
            body_text="body",
            from_="alerts@acme.com",
        )
    body = json.loads(route.calls.last.request.content)
    assert body["from"] == "alerts@acme.com"
    assert "from_" not in body


@respx.mock
async def test_emails_create_with_cc_bcc_reply_to(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/emails").mock(
        return_value=httpx.Response(201, json=make_email_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.emails.create(
            to=["a@example.com"],
            subject="hi",
            body_text="body",
            cc=["b@example.com"],
            bcc=["c@example.com"],
            reply_to="replyto@example.com",
        )
    body = json.loads(route.calls.last.request.content)
    assert body["cc"] == ["b@example.com"]
    assert body["bcc"] == ["c@example.com"]
    assert body["reply_to"] == "replyto@example.com"


# --------------------------------------------------------------------------- #
# emails.get / emails.list
# --------------------------------------------------------------------------- #


@respx.mock
async def test_emails_get(base_url: str, api_key: str) -> None:
    payload = make_email_response()
    respx.get(f"{base_url}/emails/{payload['id']}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        email = await c.emails.get(payload["id"])
    assert str(email.id) == payload["id"]


@respx.mock
async def test_emails_list_filters_by_status(base_url: str, api_key: str) -> None:
    items = [make_email_response(status="failed")]
    route = respx.get(f"{base_url}/emails").mock(
        return_value=httpx.Response(200, json={"items": items, "next_cursor": None})
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        resp = await c.emails.list(status="failed", limit=10)
    assert len(resp.items) == 1
    assert resp.items[0].status == "failed"
    # Status + limit go on the query string.
    req = route.calls.last.request
    assert req.url.params["status"] == "failed"
    assert req.url.params["limit"] == "10"


# --------------------------------------------------------------------------- #
# emails.events / emails.stats
# --------------------------------------------------------------------------- #


@respx.mock
async def test_emails_events(base_url: str, api_key: str) -> None:
    payload = {"items": [{"kind": "delivered", "occurred_at": "2026-06-01T00:00:00Z"}]}
    route = respx.get(f"{base_url}/emails/abc/events").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        out = await c.emails.events("abc")
    assert out == payload
    assert route.calls.last.request.url.path == "/emails/abc/events"


@respx.mock
async def test_emails_stats_default_bucket(base_url: str, api_key: str) -> None:
    payload = {"totals": {"sent": 10}}
    route = respx.get(f"{base_url}/emails/stats").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        out = await c.emails.stats()
    assert out == payload
    req = route.calls.last.request
    assert req.url.params["bucket"] == "day"
    assert "from" not in req.url.params
    assert "to" not in req.url.params


@respx.mock
async def test_emails_stats_with_from_to_params(base_url: str, api_key: str) -> None:
    payload = {"totals": {"sent": 0}}
    route = respx.get(f"{base_url}/emails/stats").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        out = await c.emails.stats(
            from_="2026-06-01T00:00:00Z", to="2026-06-30T00:00:00Z", bucket="hour"
        )
    assert out["totals"]["sent"] == 0
    req = route.calls.last.request
    assert req.url.params["bucket"] == "hour"
    assert req.url.params["from"] == "2026-06-01T00:00:00Z"
    assert req.url.params["to"] == "2026-06-30T00:00:00Z"


@respx.mock
async def test_emails_stats_accepts_datetime_args(base_url: str, api_key: str) -> None:
    from datetime import datetime, timezone

    payload = {"totals": {"sent": 0}}
    route = respx.get(f"{base_url}/emails/stats").mock(
        return_value=httpx.Response(200, json=payload)
    )
    dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.emails.stats(from_=dt)
    req = route.calls.last.request
    assert req.url.params["from"] == dt.isoformat()


# --------------------------------------------------------------------------- #
# EmailCreate model — local validation
# --------------------------------------------------------------------------- #


def test_email_create_rejects_invalid_recipient() -> None:
    with pytest.raises(ValueError, match="invalid email"):
        EmailCreate(to=["not-an-email"], subject="hi", body_text="body")


def test_email_create_requires_a_body() -> None:
    with pytest.raises(ValueError, match="body_text or body_html"):
        EmailCreate(to=["x@example.com"], subject="hi")


def test_email_create_accepts_html_only() -> None:
    e = EmailCreate(to=["x@example.com"], subject="hi", body_html="<p>x</p>")
    assert e.body_html == "<p>x</p>"


def test_email_create_serializes_from_alias() -> None:
    e = EmailCreate(
        to=["x@example.com"], subject="hi", body_text="b", from_="alice@example.com"
    )
    dumped = e.model_dump(by_alias=True, exclude_none=True)
    assert dumped["from"] == "alice@example.com"
    assert "from_" not in dumped


# --------------------------------------------------------------------------- #
# Inbound EmailResponse — direction, verdicts, attachments, status="received"
# --------------------------------------------------------------------------- #


def test_email_response_inbound_fields_parse() -> None:
    """EmailResponse must accept inbound-only fields without dropping them.

    Also validates that status='received' is accepted (inbound mails use
    this status and the SDK Literal must include it).
    """
    from hail.models import EmailResponse, EmailAttachmentResponse
    from uuid import uuid4
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    att_id = str(uuid4())
    email = EmailResponse.model_validate(
        {
            "id": str(uuid4()),
            "organization_id": str(uuid4()),
            "conversation_id": None,
            "email_domain_id": str(uuid4()),
            "from_address": "sender@example.com",
            "to_addresses": ["inbox@mail.hail.so"],
            "cc_addresses": None,
            "bcc_addresses": None,
            "reply_to": None,
            "subject": "hello",
            "status": "received",
            "end_reason": None,
            "provider_message_id": None,
            "requested_at": now.isoformat(),
            "sent_at": None,
            "failed_at": None,
            "metadata": {},
            "body_text": "hi there",
            "body_html": None,
            "direction": "inbound",
            "message_id": "<abc@example.com>",
            "in_reply_to": None,
            "references_ids": ["<prev@example.com>"],
            "spam_verdict": "pass",
            "virus_verdict": "pass",
            "dkim_verdict": "pass",
            "spf_verdict": "pass",
            "dmarc_verdict": "pass",
            "provider_received_at": now.isoformat(),
            "raw_url": "https://api.hail.so/emails/abc/raw",
            "attachments": [
                {
                    "id": att_id,
                    "filename": "report.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 4096,
                    "content_id": None,
                    "url": "https://api.hail.so/emails/abc/attachments/1",
                }
            ],
        }
    )
    assert email.status == "received"
    assert email.direction == "inbound"
    assert email.message_id == "<abc@example.com>"
    assert email.spam_verdict == "pass"
    assert email.raw_url == "https://api.hail.so/emails/abc/raw"
    assert len(email.attachments) == 1
    att = email.attachments[0]
    assert isinstance(att, EmailAttachmentResponse)
    assert att.filename == "report.pdf"
    assert att.size_bytes == 4096


def test_email_response_outbound_keeps_defaults() -> None:
    """Outbound EmailResponse (no inbound fields in payload) still parses cleanly."""
    from hail.models import EmailResponse
    from uuid import uuid4
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    email = EmailResponse.model_validate(
        {
            "id": str(uuid4()),
            "organization_id": str(uuid4()),
            "conversation_id": None,
            "email_domain_id": str(uuid4()),
            "from_address": "sender@example.com",
            "to_addresses": ["dest@example.com"],
            "cc_addresses": None,
            "bcc_addresses": None,
            "reply_to": None,
            "subject": "hello",
            "status": "sent",
            "end_reason": None,
            "provider_message_id": "ses-123",
            "requested_at": now.isoformat(),
            "sent_at": now.isoformat(),
            "failed_at": None,
            "metadata": {},
            "body_text": "body",
            "body_html": None,
        }
    )
    assert email.direction == "outbound"
    assert email.attachments == []
    assert email.spam_verdict is None
