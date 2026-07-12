"""Route tests for /email-accounts. Google is mocked at the gmail_oauth layer."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from hailhq.core.models import Email, EmailAccount
from hailhq.core.providers.email.gmail_oauth import (
    GmailOAuthError,
    TokenGrant,
    Userinfo,
    mint_state,
)
from hailhq.core.secret_cipher import SecretCipher


@pytest.fixture(autouse=True)
def _feature_settings(monkeypatch):
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "google_oauth_client_id", "cid")
    monkeypatch.setattr(config.settings, "google_oauth_client_secret", "csecret")
    monkeypatch.setattr(
        config.settings,
        "hail_provider_secret_key",
        # any valid Fernet key works for tests
        __import__(
            "hailhq.core.secret_cipher", fromlist=["generate_key"]
        ).generate_key(),
    )


async def _insert_account(session, org_id, address="alice@gmail.com", status="active"):
    from hailhq.core import config

    # Real Fernet ciphertext under the test's HAIL_PROVIDER_SECRET_KEY (set
    # by the autouse _feature_settings fixture) — the delete route decrypts
    # this for real before calling revoke_token, so a placeholder plaintext
    # string would raise cryptography.fernet.InvalidToken.
    cipher = SecretCipher(config.settings.hail_provider_secret_key)
    acct = EmailAccount(
        organization_id=org_id,
        email_address=address,
        provider_user_id="sub-1",
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        encrypted_refresh_token=cipher.encrypt("rt-fixture"),
        status=status,
    )
    session.add(acct)
    await session.commit()
    await session.refresh(acct)
    return acct


async def test_connect_returns_google_url(client, org_and_key):
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-accounts/connect", headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 200
    url = resp.json()["authorization_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "state=" in url


async def test_connect_503_when_unconfigured(client, org_and_key, monkeypatch):
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "google_oauth_client_id", "")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-accounts/connect", headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 503


async def test_callback_creates_account(client, org_and_key, async_session):
    org_id, _, _ = org_and_key
    grant = TokenGrant(access_token="at", refresh_token="rt", expires_in=3599)
    info = Userinfo(sub="sub-1", email="alice@gmail.com", name="Alice")
    with (
        patch(
            "hailhq.api.routes.email_accounts.exchange_code",
            new=AsyncMock(return_value=grant),
        ),
        patch(
            "hailhq.api.routes.email_accounts.fetch_userinfo",
            new=AsyncMock(return_value=info),
        ),
    ):
        resp = await client.get(
            "/email-accounts/oauth/callback",
            params={"code": "c0de", "state": mint_state(org_id, None)},
        )
    assert resp.status_code == 200  # minimal HTML success page
    row = (
        await async_session.execute(
            select(EmailAccount).where(EmailAccount.organization_id == org_id)
        )
    ).scalar_one()
    assert row.email_address == "alice@gmail.com"
    assert row.status == "active"
    assert row.encrypted_refresh_token != "rt"  # stored encrypted, not plaintext


async def test_callback_exchange_code_expired_returns_400_not_500(client, org_and_key):
    org_id, _, _ = org_and_key
    with patch(
        "hailhq.api.routes.email_accounts.exchange_code",
        new=AsyncMock(side_effect=GmailOAuthError("boom")),
    ):
        resp = await client.get(
            "/email-accounts/oauth/callback",
            params={"code": "c0de", "state": mint_state(org_id, None)},
        )
    assert resp.status_code == 400
    assert resp.status_code != 500


async def test_callback_fetch_userinfo_failure_returns_400_not_500(client, org_and_key):
    org_id, _, _ = org_and_key
    grant = TokenGrant(access_token="at", refresh_token="rt", expires_in=3599)
    with (
        patch(
            "hailhq.api.routes.email_accounts.exchange_code",
            new=AsyncMock(return_value=grant),
        ),
        patch(
            "hailhq.api.routes.email_accounts.fetch_userinfo",
            new=AsyncMock(side_effect=GmailOAuthError("boom")),
        ),
    ):
        resp = await client.get(
            "/email-accounts/oauth/callback",
            params={"code": "c0de", "state": mint_state(org_id, None)},
        )
    assert resp.status_code == 400
    assert resp.status_code != 500


async def test_callback_rejects_bad_state(client):
    resp = await client.get(
        "/email-accounts/oauth/callback", params={"code": "x", "state": "garbage"}
    )
    assert resp.status_code == 400


async def test_callback_escapes_error_param(client):
    resp = await client.get(
        "/email-accounts/oauth/callback",
        params={"error": "<script>alert(1)</script>"},
    )
    assert resp.status_code == 400
    assert "<script>" not in resp.text  # escaped (&lt;script&gt;), never raw HTML


async def test_list_never_leaks_tokens(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    await _insert_account(async_session, org_id)
    resp = await client.get(
        "/email-accounts", headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["email_address"] == "alice@gmail.com"
    assert "refresh" not in str(resp.json()).lower()
    assert "encrypted" not in str(resp.json()).lower()


async def test_patch_disable_and_enable(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    resp = await client.patch(
        f"/email-accounts/{acct.id}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"


async def test_patch_reauth_to_active_returns_409(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id, status="reauth_required")
    resp = await client.patch(
        f"/email-accounts/{acct.id}",
        json={"status": "active"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 409
    assert "reconnect" in resp.json()["detail"]
    await async_session.refresh(acct)
    assert acct.status == "reauth_required"  # PATCH did not clear it


async def test_patch_reauth_to_disabled_stays_allowed(
    client, org_and_key, async_session
):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id, status="reauth_required")
    resp = await client.patch(
        f"/email-accounts/{acct.id}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"


async def test_get_other_org_account_404(client, org_and_key, async_session):
    _, _, plain = org_and_key
    other = await _insert_account(async_session, uuid.uuid4(), address="x@gmail.com")
    resp = await client.get(
        f"/email-accounts/{other.id}", headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 404


async def test_patch_other_org_account_404(client, org_and_key, async_session):
    _, _, plain = org_and_key
    other = await _insert_account(async_session, uuid.uuid4(), address="x@gmail.com")
    resp = await client.patch(
        f"/email-accounts/{other.id}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 404


async def test_delete_other_org_account_404(client, org_and_key, async_session):
    _, _, plain = org_and_key
    other = await _insert_account(async_session, uuid.uuid4(), address="x@gmail.com")
    resp = await client.delete(
        f"/email-accounts/{other.id}", headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 404


async def test_delete_revokes_and_deletes(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    with patch(
        "hailhq.api.routes.email_accounts.revoke_token", new=AsyncMock()
    ) as revoke:
        resp = await client.delete(
            f"/email-accounts/{acct.id}", headers={"Authorization": f"Bearer {plain}"}
        )
    assert resp.status_code == 204
    revoke.assert_awaited_once()


async def test_delete_409_when_emails_reference(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    async_session.add(
        Email(
            organization_id=org_id,
            email_account_id=acct.id,
            from_address="alice@gmail.com",
            to_addresses=["b@c.com"],
            subject="s",
            body_text="t",
            provider="gmail",
        )
    )
    await async_session.commit()
    with patch(
        "hailhq.api.routes.email_accounts.revoke_token", new=AsyncMock()
    ) as revoke:
        resp = await client.delete(
            f"/email-accounts/{acct.id}", headers={"Authorization": f"Bearer {plain}"}
        )
    assert resp.status_code == 409
    revoke.assert_not_called()  # must not revoke at Google before the delete can commit


async def test_reconnect_rejects_different_google_account(
    client, org_and_key, async_session
):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    resp = await client.post(
        f"/email-accounts/{acct.id}/reconnect",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    grant = TokenGrant(access_token="at", refresh_token="rt2", expires_in=3599)
    other = Userinfo(sub="DIFFERENT-sub", email="alice@gmail.com")
    with (
        patch(
            "hailhq.api.routes.email_accounts.exchange_code",
            new=AsyncMock(return_value=grant),
        ),
        patch(
            "hailhq.api.routes.email_accounts.fetch_userinfo",
            new=AsyncMock(return_value=other),
        ),
    ):
        resp = await client.get(
            "/email-accounts/oauth/callback",
            params={"code": "c", "state": mint_state(org_id, acct.id)},
        )
    assert resp.status_code == 409


async def test_callback_same_org_same_address_refreshes_in_place(
    client, org_and_key, async_session
):
    org_id, _, _ = org_and_key
    acct = await _insert_account(async_session, org_id, status="reauth_required")
    old_ciphertext = acct.encrypted_refresh_token
    grant = TokenGrant(access_token="at", refresh_token="rt-new", expires_in=3599)
    info = Userinfo(sub="sub-1", email="alice@gmail.com", name="Alice")
    with (
        patch(
            "hailhq.api.routes.email_accounts.exchange_code",
            new=AsyncMock(return_value=grant),
        ),
        patch(
            "hailhq.api.routes.email_accounts.fetch_userinfo",
            new=AsyncMock(return_value=info),
        ),
    ):
        resp = await client.get(
            "/email-accounts/oauth/callback",
            params={"code": "c", "state": mint_state(org_id, None)},
        )
    assert resp.status_code == 200
    rows = (
        (
            await async_session.execute(
                select(EmailAccount).where(EmailAccount.organization_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # refreshed in place, no duplicate row
    row = rows[0]
    await async_session.refresh(row)
    assert row.encrypted_refresh_token != old_ciphertext
    assert row.status == "active"


async def test_callback_409_when_address_connected_to_other_org(
    client, org_and_key, async_session
):
    org_a, _, _ = org_and_key
    acct = await _insert_account(async_session, org_a)
    old_ciphertext = acct.encrypted_refresh_token
    org_b = uuid.uuid4()
    grant = TokenGrant(access_token="at", refresh_token="rt-b", expires_in=3599)
    info = Userinfo(sub="sub-1", email="alice@gmail.com")
    with (
        patch(
            "hailhq.api.routes.email_accounts.exchange_code",
            new=AsyncMock(return_value=grant),
        ),
        patch(
            "hailhq.api.routes.email_accounts.fetch_userinfo",
            new=AsyncMock(return_value=info),
        ),
    ):
        resp = await client.get(
            "/email-accounts/oauth/callback",
            params={"code": "c", "state": mint_state(org_b, None)},
        )
    assert resp.status_code == 409
    await async_session.refresh(acct)
    assert acct.organization_id == org_a  # org A's row untouched
    assert acct.encrypted_refresh_token == old_ciphertext
    assert acct.status == "active"


async def test_callback_reconnect_success_updates_token_and_status(
    client, org_and_key, async_session
):
    org_id, _, _ = org_and_key
    acct = await _insert_account(async_session, org_id, status="reauth_required")
    old_ciphertext = acct.encrypted_refresh_token
    grant = TokenGrant(access_token="at", refresh_token="rt-re", expires_in=3599)
    info = Userinfo(sub="sub-1", email="alice@gmail.com")  # same Google account
    with (
        patch(
            "hailhq.api.routes.email_accounts.exchange_code",
            new=AsyncMock(return_value=grant),
        ),
        patch(
            "hailhq.api.routes.email_accounts.fetch_userinfo",
            new=AsyncMock(return_value=info),
        ),
    ):
        resp = await client.get(
            "/email-accounts/oauth/callback",
            params={"code": "c", "state": mint_state(org_id, acct.id)},
        )
    assert resp.status_code == 200
    await async_session.refresh(acct)
    assert acct.status == "active"
    assert acct.encrypted_refresh_token != old_ciphertext
