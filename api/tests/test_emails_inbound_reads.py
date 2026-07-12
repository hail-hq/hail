"""Tests for the inbound-read API surface.

GET /emails?direction=inbound — filter by direction
GET /emails/{id}/raw           — 302 → presigned S3 URL (404 for outbound)
GET /emails/{id}/attachments/{aid} — 302 → presigned S3 URL
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.main import app
from hailhq.api.routes.emails import _get_s3_mail
from hailhq.core.models import Email, EmailAttachment, EmailDomain


@pytest.fixture()
async def auth_headers(async_session: AsyncSession) -> tuple[uuid.UUID, dict[str, str]]:
    from .conftest import insert_org_and_key

    org_id, _, plain = await insert_org_and_key(async_session)
    return org_id, {"Authorization": f"Bearer {plain}"}


@pytest.fixture()
def s3_mock():
    s3 = AsyncMock()
    s3.presign_get.return_value = "https://signed.example.com/object"
    app.dependency_overrides[_get_s3_mail] = lambda: s3
    try:
        yield s3
    finally:
        app.dependency_overrides.pop(_get_s3_mail, None)


async def _make_inbound_pair(
    async_session: AsyncSession, org_id: uuid.UUID
) -> tuple[Email, EmailAttachment]:
    domain = EmailDomain(
        organization_id=org_id,
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
    email = Email(
        organization_id=org_id,
        email_domain_id=domain.id,
        direction="inbound",
        from_address="x@example.com",
        to_addresses=["alice+acme@mail.hail.so"],
        subject="hello",
        body_text="hi",
        status="received",
        provider="ses",
        message_id="<m1>",
        raw_s3_key="raw/m1",
    )
    async_session.add(email)
    await async_session.commit()
    att = EmailAttachment(
        email_id=email.id,
        filename="report.pdf",
        content_type="application/pdf",
        size_bytes=42,
        s3_key=f"attachments/{email.id}/x",
    )
    async_session.add(att)
    await async_session.commit()
    await async_session.refresh(email)
    await async_session.refresh(att)
    return email, att


async def _make_outbound(async_session: AsyncSession, org_id: uuid.UUID) -> Email:
    domain = EmailDomain(
        organization_id=org_id,
        kind="hail_mail",
        domain="bob+globex@mail.hail.so",
        local_prefix_user="bob",
        local_prefix_org="globex",
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    async_session.add(domain)
    await async_session.commit()
    email = Email(
        organization_id=org_id,
        email_domain_id=domain.id,
        direction="outbound",
        from_address="bob+globex@mail.hail.so",
        to_addresses=["x@example.com"],
        subject="out",
        body_text="hi",
        status="sent",
        provider="ses",
    )
    async_session.add(email)
    await async_session.commit()
    await async_session.refresh(email)
    return email


@pytest.mark.asyncio
async def test_list_filter_by_direction(
    client: httpx.AsyncClient,
    auth_headers,
    async_session: AsyncSession,
):
    org_id, headers = auth_headers
    inbound, _ = await _make_inbound_pair(async_session, org_id)
    outbound = await _make_outbound(async_session, org_id)

    r = await client.get("/emails?direction=inbound", headers=headers)
    assert r.status_code == 200
    ids = [e["id"] for e in r.json()["items"]]
    assert str(inbound.id) in ids
    assert str(outbound.id) not in ids

    r = await client.get("/emails?direction=outbound", headers=headers)
    ids = [e["id"] for e in r.json()["items"]]
    assert str(outbound.id) in ids
    assert str(inbound.id) not in ids


@pytest.mark.asyncio
async def test_raw_redirects_for_inbound(
    client: httpx.AsyncClient,
    auth_headers,
    async_session: AsyncSession,
    s3_mock,
):
    org_id, headers = auth_headers
    inbound, _ = await _make_inbound_pair(async_session, org_id)
    r = await client.get(
        f"/emails/{inbound.id}/raw", headers=headers, follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://")
    s3_mock.presign_get.assert_called_once_with("raw/m1", ttl_seconds=300)


@pytest.mark.asyncio
async def test_raw_404_for_outbound(
    client: httpx.AsyncClient,
    auth_headers,
    async_session: AsyncSession,
    s3_mock,
):
    org_id, headers = auth_headers
    outbound = await _make_outbound(async_session, org_id)
    r = await client.get(
        f"/emails/{outbound.id}/raw", headers=headers, follow_redirects=False
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_attachment_redirects(
    client: httpx.AsyncClient,
    auth_headers,
    async_session: AsyncSession,
    s3_mock,
):
    org_id, headers = auth_headers
    inbound, att = await _make_inbound_pair(async_session, org_id)
    r = await client.get(
        f"/emails/{inbound.id}/attachments/{att.id}",
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://")
    s3_mock.presign_get.assert_called_once()


@pytest.mark.asyncio
async def test_attachment_404_for_unknown_id(
    client: httpx.AsyncClient,
    auth_headers,
    async_session: AsyncSession,
    s3_mock,
):
    org_id, headers = auth_headers
    inbound, _ = await _make_inbound_pair(async_session, org_id)
    bogus = uuid.uuid4()
    r = await client.get(
        f"/emails/{inbound.id}/attachments/{bogus}",
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_attachment_cross_org_isolation(
    client: httpx.AsyncClient,
    auth_headers,
    async_session: AsyncSession,
    s3_mock,
):
    org_id, headers = auth_headers
    inbound, att = await _make_inbound_pair(async_session, org_id)
    from .conftest import insert_org_and_key

    _, _, plain_b = await insert_org_and_key(async_session)
    other_headers = {"Authorization": f"Bearer {plain_b}"}
    r = await client.get(
        f"/emails/{inbound.id}/attachments/{att.id}",
        headers=other_headers,
        follow_redirects=False,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_email_populates_raw_url_and_attachments(
    client: httpx.AsyncClient,
    auth_headers,
    async_session: AsyncSession,
):
    org_id, headers = auth_headers
    inbound, att = await _make_inbound_pair(async_session, org_id)

    r = await client.get(f"/emails/{inbound.id}", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["raw_url"] is not None
    assert body["raw_url"].endswith(f"/emails/{inbound.id}/raw")
    assert len(body["attachments"]) == 1
    assert body["attachments"][0]["url"].endswith(
        f"/emails/{inbound.id}/attachments/{att.id}"
    )


@pytest.mark.asyncio
async def test_get_outbound_email_no_raw_url_no_attachments(
    client: httpx.AsyncClient,
    auth_headers,
    async_session: AsyncSession,
):
    """Outbound rows must not gain raw_url or attachments — no behavior change."""
    org_id, headers = auth_headers
    outbound = await _make_outbound(async_session, org_id)

    r = await client.get(f"/emails/{outbound.id}", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["raw_url"] is None
    assert body["attachments"] == []
