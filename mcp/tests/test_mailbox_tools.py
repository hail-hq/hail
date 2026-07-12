"""Unit tests for the mailbox MCP tool wrappers.

Tests target ``list_email_accounts``, ``search_mailbox``, and
``read_mailbox_message`` in :mod:`hailhq.mcp.tools`, plus the
``in_reply_to`` passthrough on ``send_email``. Same style as
``test_tools.py``: respx stubs the REST API, tests call the module-level
tool functions directly with a constructed ``HailClient``.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx

from hailhq.mcp import tools
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


def _email_account(account_id: str | None = None, status: str = "active") -> dict:
    """Return a minimal EmailAccountResponse-shaped dict."""
    aid = account_id or str(uuid4())
    return {
        "id": aid,
        "provider": "gmail",
        "email_address": "alice@example.com",
        "display_name": "Alice",
        "status": status,
        "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-01T00:00:00+00:00",
    }


def _mailbox_message_summary(
    message_id: str | None = None, subject: str = "hi"
) -> dict:
    """Return a minimal MailboxMessageSummary-shaped dict."""
    return {
        "id": "gmail-msg-1",
        "thread_id": "gmail-thread-1",
        "from_address": "bob@example.com",
        "to_addresses": ["alice@example.com"],
        "cc_addresses": [],
        "subject": subject,
        "date": "2026-07-10T00:00:00+00:00",
        "snippet": "hello there",
        "message_id": message_id or "<abc123@mail.example.com>",
    }


def _mailbox_message_detail(message_id: str | None = None) -> dict:
    """Return a minimal MailboxMessageDetail-shaped dict."""
    summary = _mailbox_message_summary(message_id=message_id)
    return {
        **summary,
        "body_text": "hello there, full body",
        "body_html": None,
        "in_reply_to": None,
        "attachments": [],
    }


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


# --------------------------------------------------------------------------- #
# list_email_accounts
# --------------------------------------------------------------------------- #


@respx.mock
async def test_list_email_accounts_happy_path(client: HailClient) -> None:
    aid = str(uuid4())
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200, json={"items": [_email_account(aid)], "next_cursor": None}
        )

    respx.get(f"{_BASE_URL}/email-accounts").mock(side_effect=_handler)

    result = await tools.list_email_accounts(client=client)
    assert "error" not in result, result
    assert result["items"][0]["id"] == aid
    assert result["items"][0]["email_address"] == "alice@example.com"
    assert result["next_cursor"] is None
    assert captured["headers"]["authorization"] == f"Bearer {_API_KEY}"


@respx.mock
async def test_list_email_accounts_maps_401_to_error(client: HailClient) -> None:
    respx.get(f"{_BASE_URL}/email-accounts").mock(
        return_value=httpx.Response(401, json={"detail": "bad key"})
    )
    result = await tools.list_email_accounts(client=client)
    assert result == {"error": "auth failed: token rejected by Hail API"}


# --------------------------------------------------------------------------- #
# search_mailbox
# --------------------------------------------------------------------------- #


@respx.mock
async def test_search_mailbox_hits_messages_endpoint_with_params(
    client: HailClient,
) -> None:
    aid = str(uuid4())
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "items": [_mailbox_message_summary()],
                "next_page_token": "next-tok",
            },
        )

    respx.get(f"{_BASE_URL}/email-accounts/{aid}/messages").mock(side_effect=_handler)

    result = await tools.search_mailbox(
        client=client,
        account_id=aid,
        q="in:inbox newer_than:2d",
        max_results=10,
        page_token="cur-tok",
    )
    assert "error" not in result, result
    assert result["items"][0]["subject"] == "hi"
    assert result["next_page_token"] == "next-tok"

    url = captured["url"]
    assert f"/email-accounts/{aid}/messages" in url
    assert "q=in%3Ainbox+newer_than%3A2d" in url or "q=in:inbox+newer_than:2d" in url
    assert "max_results=10" in url
    assert "page_token=cur-tok" in url


@respx.mock
async def test_search_mailbox_omits_optional_params(client: HailClient) -> None:
    aid = str(uuid4())
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"items": [], "next_page_token": None})

    respx.get(f"{_BASE_URL}/email-accounts/{aid}/messages").mock(side_effect=_handler)

    result = await tools.search_mailbox(client=client, account_id=aid)
    assert result["items"] == []
    # Default max_results=25 always sent; q/page_token omitted when unset.
    assert "max_results=25" in captured["url"]
    assert "q=" not in captured["url"]
    assert "page_token=" not in captured["url"]


@respx.mock
async def test_search_mailbox_maps_404_to_not_found(client: HailClient) -> None:
    aid = str(uuid4())
    respx.get(f"{_BASE_URL}/email-accounts/{aid}/messages").mock(
        return_value=httpx.Response(404, json={"detail": "account not found"})
    )
    result = await tools.search_mailbox(client=client, account_id=aid)
    assert result == {"error": "resource not found"}


# --------------------------------------------------------------------------- #
# read_mailbox_message
# --------------------------------------------------------------------------- #


@respx.mock
async def test_read_mailbox_message_hits_detail_endpoint(client: HailClient) -> None:
    aid = str(uuid4())
    mid = "gmail-msg-1"
    respx.get(f"{_BASE_URL}/email-accounts/{aid}/messages/{mid}").mock(
        return_value=httpx.Response(
            200, json=_mailbox_message_detail(message_id="<thread-42@example.com>")
        )
    )
    result = await tools.read_mailbox_message(
        client=client, account_id=aid, message_id=mid
    )
    assert "error" not in result, result
    assert result["id"] == "gmail-msg-1"
    assert result["body_text"] == "hello there, full body"
    # message_id is the RFC 2822 header used as in_reply_to on a reply.
    assert result["message_id"] == "<thread-42@example.com>"


@respx.mock
async def test_read_mailbox_message_maps_404_to_not_found(client: HailClient) -> None:
    aid = str(uuid4())
    mid = "unknown-msg"
    respx.get(f"{_BASE_URL}/email-accounts/{aid}/messages/{mid}").mock(
        return_value=httpx.Response(404, json={"detail": "message not found"})
    )
    result = await tools.read_mailbox_message(
        client=client, account_id=aid, message_id=mid
    )
    assert result == {"error": "resource not found"}


# --------------------------------------------------------------------------- #
# send_email — in_reply_to passthrough
# --------------------------------------------------------------------------- #


@respx.mock
async def test_send_email_includes_in_reply_to_when_provided(
    client: HailClient,
) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_email_response())

    respx.post(f"{_BASE_URL}/emails").mock(side_effect=_handler)

    result = await tools.send_email(
        client=client,
        recipient_consent=True,
        to=["x@example.com"],
        subject="re: hi",
        body_text="body",
        in_reply_to="<thread-42@example.com>",
    )
    assert "error" not in result, result
    body = httpx.Response(200, content=captured["body"]).json()
    assert body["in_reply_to"] == "<thread-42@example.com>"


@respx.mock
async def test_send_email_omits_in_reply_to_when_none(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
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
    body = httpx.Response(200, content=captured["body"]).json()
    assert "in_reply_to" not in body
