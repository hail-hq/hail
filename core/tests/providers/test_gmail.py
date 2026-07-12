"""GmailClient / GmailEmailProvider against a mocked Gmail REST API."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from hailhq.core.providers.email.gmail import (
    GmailApiError,
    GmailAuthError,
    GmailClient,
    GmailEmailProvider,
)


def _token_ok(request: httpx.Request) -> httpx.Response | None:
    if request.url.host == "oauth2.googleapis.com":
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
    return None


def _client(handler) -> GmailClient:
    def routed(request: httpx.Request) -> httpx.Response:
        return _token_ok(request) or handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(routed))
    return GmailClient(refresh_token="rt", http=http)


async def test_send_message_posts_base64_raw_and_thread() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/messages/send")
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "m1", "threadId": "t1"})

    provider = GmailEmailProvider(_client(handler))
    result = await provider.send_email(
        from_address="alice@gmail.com",
        to_addresses=["bob@example.com"],
        subject="hi",
        body_text="hello",
        body_html=None,
        bcc=["quiet@example.com"],
    )
    assert result.provider_message_id == "m1"
    assert result.provider_thread_id == "t1"
    raw = base64.urlsafe_b64decode(seen["raw"] + "==").decode()
    assert "Bcc: quiet@example.com" in raw
    assert "threadId" not in seen  # no reply → no thread pinning


async def test_reply_resolves_thread_from_in_reply_to() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages") and request.method == "GET":
            assert "rfc822msgid" in str(request.url)
            return httpx.Response(
                200, json={"messages": [{"id": "orig", "threadId": "t9"}]}
            )
        body = json.loads(request.content)
        assert body["threadId"] == "t9"
        return httpx.Response(200, json={"id": "m2", "threadId": "t9"})

    provider = GmailEmailProvider(_client(handler))
    result = await provider.send_email(
        from_address="alice@gmail.com",
        to_addresses=["bob@example.com"],
        subject="Re: hi",
        body_text="pong",
        body_html=None,
        headers={
            "In-Reply-To": "<abc@mail.example>",
            "References": "<abc@mail.example>",
        },
    )
    assert result.provider_thread_id == "t9"


async def test_gmail_401_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})

    client = _client(handler)
    with pytest.raises(GmailAuthError):
        await client.get_profile()


async def test_transport_error_surfaces_as_gmail_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler)
    with pytest.raises(GmailApiError) as exc_info:
        await client.get_profile()
    assert exc_info.value.status == 502
    assert not isinstance(exc_info.value, GmailAuthError)


async def test_token_refresh_transport_error_surfaces_as_gmail_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            raise httpx.ConnectError("connection refused", request=request)
        raise AssertionError("should not reach the Gmail API without a token")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = GmailClient(refresh_token="rt", http=http)
    with pytest.raises(GmailApiError) as exc_info:
        await client.get_profile()
    assert exc_info.value.status == 502
    assert not isinstance(exc_info.value, GmailAuthError)


async def test_get_message_parses_multipart_body() -> None:
    def b64(s: str) -> str:
        return base64.urlsafe_b64encode(s.encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "m3",
                "threadId": "t3",
                "snippet": "hello there",
                "payload": {
                    "mimeType": "multipart/mixed",
                    "headers": [
                        {"name": "From", "value": "Bob <bob@example.com>"},
                        {
                            "name": "To",
                            "value": '"Doe, John" <john@example.com>, alice@example.com',
                        },
                        {"name": "Subject", "value": "hi"},
                        {"name": "Date", "value": "Sat, 12 Jul 2026 10:00:00 +0000"},
                        {"name": "Message-ID", "value": "<xyz@mail.example>"},
                    ],
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": b64("hello there")},
                        },
                        {
                            "mimeType": "application/pdf",
                            "filename": "doc.pdf",
                            "body": {"attachmentId": "att1", "size": 1234},
                        },
                    ],
                },
            },
        )

    msg = await _client(handler).get_message("m3")
    assert msg["body_text"] == "hello there"
    assert msg["message_id"] == "<xyz@mail.example>"
    # Bare addresses only — display names stripped, quoted commas respected.
    assert msg["from_address"] == "bob@example.com"
    assert msg["to_addresses"] == ["john@example.com", "alice@example.com"]
    assert msg["attachments"] == [
        {
            "filename": "doc.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1234,
            "attachment_id": "att1",
        }
    ]
