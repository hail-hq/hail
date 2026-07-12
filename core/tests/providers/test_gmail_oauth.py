"""Unit tests for gmail_oauth helpers — Google is mocked at the httpx layer."""

from __future__ import annotations

import uuid

import httpx
import pytest

from hailhq.core.providers.email import gmail_oauth
from hailhq.core.providers.email.gmail_oauth import (
    GmailReauthRequired,
    InvalidStateToken,
    build_authorization_url,
    exchange_code,
    mint_state,
    refresh_access_token,
    verify_state,
)


@pytest.fixture(autouse=True)
def _oauth_settings(monkeypatch):
    monkeypatch.setattr(
        gmail_oauth.settings, "google_oauth_client_id", "cid.apps.googleusercontent.com"
    )
    monkeypatch.setattr(gmail_oauth.settings, "google_oauth_client_secret", "csecret")


def test_authorization_url_carries_scopes_and_state() -> None:
    url = build_authorization_url(state="st4te", redirect_uri="https://api.example/cb")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=st4te" in url
    assert "gmail.send" in url and "gmail.readonly" in url


def test_state_roundtrip_and_tamper() -> None:
    org = uuid.uuid4()
    acct = uuid.uuid4()
    token = mint_state(org, acct)
    assert verify_state(token) == (org, acct)
    assert verify_state(mint_state(org, None)) == (org, None)
    with pytest.raises(InvalidStateToken):
        verify_state(token[:-2] + "xx")


async def test_exchange_code_parses_grant() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://oauth2.googleapis.com/token")
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3599,
                "scope": "openid email https://www.googleapis.com/auth/gmail.send",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        grant = await exchange_code(
            code="c0de", redirect_uri="https://api.example/cb", http=http
        )
    assert grant.access_token == "at"
    assert grant.refresh_token == "rt"


async def test_refresh_invalid_grant_raises_reauth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(GmailReauthRequired):
            await refresh_access_token(refresh_token="rt", http=http)
