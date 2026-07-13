"""Ephemeral mailbox reads — nothing is persisted; Gmail is faked."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from hailhq.api.main import app
from hailhq.api.routes.email_accounts import get_gmail_client_builder
from hailhq.core.models import Email
from hailhq.core.providers.email.gmail import GmailApiError, GmailAuthError

from tests.test_email_accounts_api import _insert_account  # reuse row helper


@pytest.fixture(autouse=True)
def _secret_key_settings(monkeypatch):
    # _insert_account Fernet-encrypts the refresh token; the autouse fixture
    # that sets this in test_email_accounts_api.py only applies within that
    # module, so this test module needs its own copy.
    from hailhq.core import config
    from hailhq.core.secret_cipher import generate_key

    monkeypatch.setattr(config.settings, "hail_provider_secret_key", generate_key())


class FakeGmail:
    def __init__(
        self, *, fail_auth: bool = False, api_error: GmailApiError | None = None
    ) -> None:
        self.fail_auth = fail_auth
        self.api_error = api_error

    async def list_messages(self, *, q=None, max_results=25, page_token=None):
        if self.fail_auth:
            raise GmailAuthError(401, "revoked")
        if self.api_error is not None:
            raise self.api_error
        summary = {
            "id": "m1",
            "thread_id": "t1",
            "from_address": "Bob <bob@example.com>",
            "to_addresses": ["alice@gmail.com"],
            "cc_addresses": [],
            "subject": "hi",
            "date": "Sat, 12 Jul 2026 10:00:00 +0000",
            "snippet": "hello",
            "message_id": "<xyz@mail.example>",
        }
        return [summary], None

    async def get_message(self, message_id):
        if self.fail_auth:
            raise GmailAuthError(401, "revoked")
        if self.api_error is not None:
            raise self.api_error
        return {
            "id": message_id,
            "thread_id": "t1",
            "from_address": "Bob <bob@example.com>",
            "to_addresses": ["alice@gmail.com"],
            "cc_addresses": [],
            "subject": "hi",
            "date": "Sat, 12 Jul 2026 10:00:00 +0000",
            "snippet": "hello",
            "message_id": "<xyz@mail.example>",
            "body_text": "hello",
            "body_html": None,
            "in_reply_to": None,
            "attachments": [],
        }


@pytest.fixture()
def fake_gmail():
    fake = FakeGmail()
    app.dependency_overrides[get_gmail_client_builder] = lambda: (lambda account: fake)
    yield fake
    app.dependency_overrides.pop(get_gmail_client_builder, None)


async def test_list_messages_proxies_and_stores_nothing(
    client, org_and_key, async_session, fake_gmail
):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    resp = await client.get(
        f"/email-accounts/{acct.id}/messages",
        params={"q": "in:inbox"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["subject"] == "hi"
    count = (await async_session.execute(select(func.count(Email.id)))).scalar_one()
    assert count == 0  # ephemeral: no rows written


async def test_get_message_detail(client, org_and_key, async_session, fake_gmail):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    resp = await client.get(
        f"/email-accounts/{acct.id}/messages/m1",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    assert resp.json()["body_text"] == "hello"
    assert resp.json()["message_id"] == "<xyz@mail.example>"


async def test_disabled_account_409(client, org_and_key, async_session, fake_gmail):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id, status="disabled")
    resp = await client.get(
        f"/email-accounts/{acct.id}/messages",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 409


async def test_auth_error_flags_reauth_required(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    fake = FakeGmail(fail_auth=True)
    app.dependency_overrides[get_gmail_client_builder] = lambda: (lambda a: fake)
    try:
        resp = await client.get(
            f"/email-accounts/{acct.id}/messages",
            headers={"Authorization": f"Bearer {plain}"},
        )
    finally:
        app.dependency_overrides.pop(get_gmail_client_builder, None)
    assert resp.status_code == 409
    await async_session.refresh(acct)
    assert acct.status == "reauth_required"


async def test_gmail_5xx_returns_502_not_500(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    fake = FakeGmail(api_error=GmailApiError(500, "backend error"))
    app.dependency_overrides[get_gmail_client_builder] = lambda: (lambda a: fake)
    try:
        resp = await client.get(
            f"/email-accounts/{acct.id}/messages",
            headers={"Authorization": f"Bearer {plain}"},
        )
    finally:
        app.dependency_overrides.pop(get_gmail_client_builder, None)
    assert resp.status_code == 502
    assert resp.status_code != 500


async def test_gmail_429_returns_429(client, org_and_key, async_session):
    # Rate limiting must surface as 429 (not folded into the generic 4xx ->
    # 400 mapping) so callers can distinguish "back off and retry" from a
    # caller mistake.
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    fake = FakeGmail(api_error=GmailApiError(429, "rate limited"))
    app.dependency_overrides[get_gmail_client_builder] = lambda: (lambda a: fake)
    try:
        resp = await client.get(
            f"/email-accounts/{acct.id}/messages",
            headers={"Authorization": f"Bearer {plain}"},
        )
    finally:
        app.dependency_overrides.pop(get_gmail_client_builder, None)
    assert resp.status_code == 429


async def test_gmail_400_returns_400(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    fake = FakeGmail(api_error=GmailApiError(400, "bad request"))
    app.dependency_overrides[get_gmail_client_builder] = lambda: (lambda a: fake)
    try:
        resp = await client.get(
            f"/email-accounts/{acct.id}/messages/m1",
            headers={"Authorization": f"Bearer {plain}"},
        )
    finally:
        app.dependency_overrides.pop(get_gmail_client_builder, None)
    assert resp.status_code == 400


async def test_gmail_404_unknown_message_returns_404(
    client, org_and_key, async_session
):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    fake = FakeGmail(api_error=GmailApiError(404, "Requested entity was not found"))
    app.dependency_overrides[get_gmail_client_builder] = lambda: (lambda a: fake)
    try:
        resp = await client.get(
            f"/email-accounts/{acct.id}/messages/nope",
            headers={"Authorization": f"Bearer {plain}"},
        )
    finally:
        app.dependency_overrides.pop(get_gmail_client_builder, None)
    # An unknown message id is a 404, not the generic 400 for other Gmail 4xx.
    assert resp.status_code == 404


async def test_corrupted_credentials_returns_502(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    # Corrupt the ciphertext post-insert so the builder's cipher.decrypt()
    # raises cryptography.fernet.InvalidToken.
    acct.encrypted_refresh_token = "not-valid-fernet-ciphertext"
    await async_session.commit()
    resp = await client.get(
        f"/email-accounts/{acct.id}/messages",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 502
    assert "reconnect" in resp.json()["detail"]


async def test_other_org_account_404(client, org_and_key, async_session, fake_gmail):
    _, _, plain = org_and_key
    other = await _insert_account(async_session, uuid.uuid4(), address="x@gmail.com")
    resp = await client.get(
        f"/email-accounts/{other.id}/messages",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 404
