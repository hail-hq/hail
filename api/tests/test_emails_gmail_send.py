"""POST /emails through a connected Gmail account."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from hailhq.api.main import app
from hailhq.api.routes.email_accounts import get_gmail_client_builder
from hailhq.core.models import Email, UsageEvent
from hailhq.core.providers.email.gmail import GmailAuthError

from tests.test_email_accounts_api import _insert_account


@pytest.fixture(autouse=True)
def _feature_settings(monkeypatch):
    """Mirrors test_email_accounts_api.py's fixture — module-scoped autouse
    fixtures don't travel with an imported helper, and ``_insert_account``
    needs a valid Fernet key to encrypt its fixture refresh token."""
    from hailhq.core import config
    from hailhq.core.secret_cipher import generate_key

    monkeypatch.setattr(config.settings, "google_oauth_client_id", "cid")
    monkeypatch.setattr(config.settings, "google_oauth_client_secret", "csecret")
    monkeypatch.setattr(config.settings, "hail_provider_secret_key", generate_key())


class FakeGmail:
    """Stands in for GmailClient — captures what the provider sends."""

    def __init__(self, *, fail_auth: bool = False) -> None:
        self.fail_auth = fail_auth
        self.sent: list[dict] = []

    async def find_thread_id(self, rfc822_message_id: str) -> str | None:
        return "t42" if rfc822_message_id == "<orig@mail.example>" else None

    async def send_message(self, *, raw: bytes, thread_id=None):
        if self.fail_auth:
            raise GmailAuthError(401, "revoked")
        self.sent.append({"raw": raw, "thread_id": thread_id})
        return "gm-1", thread_id or "t-new"


@pytest.fixture()
def fake_gmail():
    fake = FakeGmail()
    app.dependency_overrides[get_gmail_client_builder] = lambda: (lambda a: fake)
    yield fake
    app.dependency_overrides.pop(get_gmail_client_builder, None)


def _send_body(**over):
    body = {
        "from": "alice@gmail.com",
        "to": ["bob@example.com"],
        "subject": "hi",
        "body_text": "hello",
        "recipient_consent": True,
    }
    body.update(over)
    return body


async def test_send_via_connected_account(
    client, org_and_key, async_session, fake_gmail
):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    resp = await client.post(
        "/emails", json=_send_body(), headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "sent"
    assert data["email_account_id"] == str(acct.id)
    assert data["email_domain_id"] is None
    row = (
        await async_session.execute(select(Email).where(Email.id == data["id"]))
    ).scalar_one()
    assert row.provider == "gmail"
    assert row.provider_message_id == "gm-1"
    assert row.provider_thread_id == "t-new"
    assert len(fake_gmail.sent) == 1


async def test_gmail_send_writes_usage_event(
    client, org_and_key, async_session, fake_gmail
):
    org_id, _, plain = org_and_key
    await _insert_account(async_session, org_id)
    resp = await client.post(
        "/emails", json=_send_body(), headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 201
    events = (
        (
            await async_session.execute(
                select(UsageEvent).where(
                    UsageEvent.organization_id == org_id, UsageEvent.channel == "email"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1  # billed at the standard email rate


async def test_reply_threads_into_gmail(client, org_and_key, async_session, fake_gmail):
    org_id, _, plain = org_and_key
    await _insert_account(async_session, org_id)
    resp = await client.post(
        "/emails",
        json=_send_body(in_reply_to="<orig@mail.example>", subject="Re: hi"),
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201
    assert fake_gmail.sent[0]["thread_id"] == "t42"
    assert resp.json()["provider_thread_id"] == "t42"
    raw = fake_gmail.sent[0]["raw"].decode()
    assert "In-Reply-To: <orig@mail.example>" in raw


async def test_reauth_required_account_409(
    client, org_and_key, async_session, fake_gmail
):
    org_id, _, plain = org_and_key
    await _insert_account(async_session, org_id, status="reauth_required")
    resp = await client.post(
        "/emails", json=_send_body(), headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 409
    assert "reconnect" in resp.json()["detail"]


async def test_auth_failure_marks_account_and_email(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    fake = FakeGmail(fail_auth=True)
    app.dependency_overrides[get_gmail_client_builder] = lambda: (lambda a: fake)
    try:
        resp = await client.post(
            "/emails", json=_send_body(), headers={"Authorization": f"Bearer {plain}"}
        )
    finally:
        app.dependency_overrides.pop(get_gmail_client_builder, None)
    assert resp.status_code == 409
    await async_session.refresh(acct)
    assert acct.status == "reauth_required"
    row = (
        await async_session.execute(
            select(Email).where(Email.organization_id == org_id)
        )
    ).scalar_one()
    assert row.status == "failed"


async def test_ses_default_path_untouched(client, org_and_key, async_session):
    """No connected account matches → the existing domain/hail-mail flow runs."""
    _, _, plain = org_and_key
    body = _send_body()
    del body["from"]  # default path never considers email_accounts
    resp = await client.post(
        "/emails", json=body, headers={"Authorization": f"Bearer {plain}"}
    )
    # conftest's email provider mock + hail-mail settings handle the rest;
    # assert only that resolution did not error out on the new branch.
    assert resp.status_code in (201, 422, 503)
