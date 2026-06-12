"""Tests for the inbound-action edit path on PATCH /email-domains/{id}.

The same route also handles prefix edits — those have their own tests in
test_email_domains_api.py and stay covered by the existing suite.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.config import settings
from hailhq.core.models import EmailDomain
from hailhq.core.secret_cipher import SecretCipher, generate_key


@pytest.fixture(autouse=True)
def webhook_secret_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set a real Fernet key so encrypt/decrypt round-trips in the route."""
    key = generate_key()
    monkeypatch.setattr(settings, "hail_webhook_secret_key", key)
    return key


@pytest.fixture()
async def org_id(async_session: AsyncSession) -> tuple[uuid.UUID, dict[str, str]]:
    from .conftest import insert_org_and_key

    org_id, _, plain = await insert_org_and_key(async_session)
    return org_id, {"Authorization": f"Bearer {plain}"}


@pytest.fixture()
async def hail_mail_domain(async_session: AsyncSession, org_id) -> EmailDomain:
    org, _ = org_id
    domain = EmailDomain(
        organization_id=org,
        kind="hail_mail",
        domain="alice+acme@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org="acme",
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    async_session.add(domain)
    await async_session.commit()
    await async_session.refresh(domain)
    return domain


@pytest.mark.asyncio
async def test_patch_sets_forward_targets(
    client: httpx.AsyncClient, org_id, hail_mail_domain
):
    _, headers = org_id
    r = await client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={"inbound_enabled": True, "forward_to": ["ops@acme.com"]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["forward_to"] == ["ops@acme.com"]
    assert body["inbound_enabled"] is True


@pytest.mark.asyncio
async def test_patch_sets_webhook_url_returns_secret_once(
    client: httpx.AsyncClient,
    org_id,
    hail_mail_domain,
    async_session: AsyncSession,
    webhook_secret_key: str,
):
    _, headers = org_id
    r = await client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={"inbound_enabled": True, "webhook_url": "https://hooks.example.com/x"},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["webhook_url"] == "https://hooks.example.com/x"
    assert body.get("webhook_secret", "").startswith("whd_")

    # Subsequent GET does NOT include the secret.
    g = await client.get(f"/email-domains/{hail_mail_domain.id}", headers=headers)
    assert g.json().get("webhook_secret") is None

    # The column stores ciphertext that decrypts back to the plaintext.
    domain_id = hail_mail_domain.id
    async_session.expire_all()
    dom = (
        await async_session.execute(
            select(EmailDomain).where(EmailDomain.id == domain_id)
        )
    ).scalar_one()
    assert dom.webhook_secret_encrypted != body["webhook_secret"]
    assert (
        SecretCipher(webhook_secret_key).decrypt(dom.webhook_secret_encrypted)
        == body["webhook_secret"]
    )


@pytest.mark.asyncio
async def test_patch_rejects_non_email_forward_targets(
    client: httpx.AsyncClient, org_id, hail_mail_domain
):
    _, headers = org_id
    r = await client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={"inbound_enabled": True, "forward_to": ["not-an-address"]},
        headers=headers,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_inbound_enabled_without_action_returns_422(
    client: httpx.AsyncClient, org_id, hail_mail_domain
):
    _, headers = org_id
    r = await client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={"inbound_enabled": True},
        headers=headers,
    )
    assert r.status_code == 422
    assert "forward" in r.text or "webhook" in r.text


@pytest.mark.asyncio
async def test_rotate_webhook_secret(
    client: httpx.AsyncClient,
    org_id,
    hail_mail_domain,
    async_session: AsyncSession,
    webhook_secret_key: str,
):
    _, headers = org_id
    # First, set a webhook so rotation is permitted.
    await client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={
            "inbound_enabled": True,
            "webhook_url": "https://hooks.example.com/x",
        },
        headers=headers,
    )
    rot = await client.post(
        f"/email-domains/{hail_mail_domain.id}/rotate-webhook-secret",
        headers=headers,
    )
    assert rot.status_code == 200
    secret = rot.json()["webhook_secret"]
    assert secret.startswith("whd_")

    # The stored ciphertext decrypts to the rotated plaintext.
    domain_id = hail_mail_domain.id
    async_session.expire_all()
    dom = (
        await async_session.execute(
            select(EmailDomain).where(EmailDomain.id == domain_id)
        )
    ).scalar_one()
    assert (
        SecretCipher(webhook_secret_key).decrypt(dom.webhook_secret_encrypted) == secret
    )


@pytest.mark.asyncio
async def test_rotate_secret_without_webhook_url_returns_422(
    client: httpx.AsyncClient, org_id, hail_mail_domain
):
    _, headers = org_id
    rot = await client.post(
        f"/email-domains/{hail_mail_domain.id}/rotate-webhook-secret",
        headers=headers,
    )
    assert rot.status_code == 422


@pytest.mark.asyncio
async def test_patch_webhook_url_rejects_non_https(
    client: httpx.AsyncClient, org_id, hail_mail_domain
):
    _, headers = org_id
    r = await client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={"webhook_url": "http://internal.example.com/hook"},
        headers=headers,
    )
    assert r.status_code == 422
    assert "https" in r.text.lower()


@pytest.mark.asyncio
async def test_patch_webhook_url_rejects_private_target(
    client: httpx.AsyncClient, org_id, hail_mail_domain
):
    _, headers = org_id
    r = await client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={"webhook_url": "https://10.0.0.5/hook"},
        headers=headers,
    )
    assert r.status_code == 422
    assert "private" in r.text.lower()


@pytest.mark.asyncio
async def test_patch_webhook_url_clear_to_empty_still_works(
    client: httpx.AsyncClient, org_id, hail_mail_domain
):
    """Clearing webhook_url to '' must still work (sets both columns to None)."""
    _, headers = org_id
    # First set a webhook.
    await client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={"inbound_enabled": True, "webhook_url": "https://hooks.example.com/x"},
        headers=headers,
    )
    # Now clear it by setting webhook_url to "".
    r = await client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={"inbound_enabled": False, "webhook_url": ""},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["webhook_url"] is None
