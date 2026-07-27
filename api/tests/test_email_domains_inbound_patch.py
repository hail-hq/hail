"""Tests for the inbound-action edit path on PATCH /email-domains/{id}.

The same route also handles prefix edits — those have their own tests in
test_email_domains_api.py and stay covered by the existing suite.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from hailhq.core.models import EmailDomain
from sqlalchemy.ext.asyncio import AsyncSession


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
async def test_patch_inbound_enabled_without_action_succeeds(
    client: httpx.AsyncClient, org_id, hail_mail_domain
):
    # 0018 dropped the forward_to requirement: enabling inbound with no
    # forward action is valid (mail is persisted + webhooked, not forwarded).
    _, headers = org_id
    r = await client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={"inbound_enabled": True},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["inbound_enabled"] is True
    assert body["forward_to"] is None


@pytest.mark.asyncio
async def test_rotate_webhook_secret_endpoint_removed(
    client: httpx.AsyncClient, org_id, hail_mail_domain
):
    _, headers = org_id
    r = await client.post(
        f"/email-domains/{hail_mail_domain.id}/rotate-webhook-secret",
        headers=headers,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_rejects_webhook_url_field(
    client: httpx.AsyncClient, org_id, hail_mail_domain
):
    _, headers = org_id
    r = await client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={"webhook_url": "https://hooks.example.com/x"},
        headers=headers,
    )
    assert r.status_code == 422
