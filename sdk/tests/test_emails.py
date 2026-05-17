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
