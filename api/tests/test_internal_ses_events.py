"""Tests for POST /internal/ses-events.

The route is HMAC-signed by ses-ingest-lambda. The DB-side fixture lets us
construct an EmailDomain and observe an Email row landing after a signed POST.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.main import app
from hailhq.api.routes.internal.ses_events import (
    get_inbound_provider,
    get_s3_mail_client,
)
from hailhq.core.config import settings
from hailhq.core.models import Email, EmailDomain
from hailhq.core.providers.email.inbound.ses import SesInboundProvider

FIX = Path(__file__).parent.parent.parent / "core" / "tests" / "fixtures" / "inbound"

HMAC_SECRET = "test-shared-secret"


def _signed(body: bytes) -> dict[str, str]:
    sig = hmac.new(HMAC_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hail-Signature": f"sha256={sig}"}


def _payload(
    *,
    message_id: str,
    recipient: str,
    spam: str = "PASS",
    virus: str = "PASS",
    s3_key: str = "raw/abc",
) -> dict:
    return {
        "message_id": message_id,
        "envelope_from": "alice@example.com",
        "recipients": [recipient],
        "verdicts": {
            "spam": spam,
            "virus": virus,
            "spf": "PASS",
            "dkim": "PASS",
            "dmarc": "PASS",
        },
        "s3_bucket": "hail-inbound-test-raw",
        "s3_key": s3_key,
        "timestamp": "2026-06-06T10:11:12Z",
    }


async def _insert_inbound_domain(
    async_session: AsyncSession, *, user: str, org: str
) -> uuid.UUID:
    """Insert a verified hail_mail EmailDomain; return its organization id."""
    org_id = uuid.uuid4()
    domain = EmailDomain(
        organization_id=org_id,
        kind="hail_mail",
        domain=f"{user}+{org}@mail.hail.so",
        local_prefix_user=user,
        local_prefix_org=org,
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    async_session.add(domain)
    await async_session.commit()
    return org_id


@pytest.fixture()
def inbound_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "hail_inbound_enabled", True)
    monkeypatch.setattr(settings, "hail_inbound_hmac_secret", HMAC_SECRET)
    # Bucket is derived as `${prefix}-mail`, so this yields "hail-inbound-test-mail".
    monkeypatch.setattr(settings, "hail_mail_name_prefix", "hail-inbound-test")
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")


@pytest.fixture()
def fake_s3():
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    return s3


@pytest.fixture()
def override_internal_deps(fake_s3):
    """Override the provider + S3 deps so tests don't need real boto3 / env."""
    app.dependency_overrides[get_inbound_provider] = lambda: SesInboundProvider(
        hmac_secret=HMAC_SECRET
    )
    app.dependency_overrides[get_s3_mail_client] = lambda: fake_s3
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_inbound_provider, None)
        app.dependency_overrides.pop(get_s3_mail_client, None)


@pytest.mark.asyncio
async def test_rejects_when_disabled(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "hail_inbound_enabled", False)
    body = json.dumps({}).encode()
    resp = await client.post(
        "/internal/ses-events", content=body, headers=_signed(body)
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_rejects_missing_signature(
    client: httpx.AsyncClient, inbound_enabled, override_internal_deps
):
    resp = await client.post("/internal/ses-events", json={"any": "thing"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_rejects_bad_signature(
    client: httpx.AsyncClient, inbound_enabled, override_internal_deps
):
    body = json.dumps(
        _payload(message_id="x", recipient="alice+acme@mail.hail.so")
    ).encode()
    resp = await client.post(
        "/internal/ses-events",
        content=body,
        headers={"X-Hail-Signature": "sha256=deadbeef"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_happy_path_inserts_row(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    inbound_enabled,
    override_internal_deps,
    fake_s3,
):
    org_id = uuid.uuid4()
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

    body = json.dumps(_payload(message_id="happy1", recipient=domain.domain)).encode()
    resp = await client.post(
        "/internal/ses-events", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["email_ids"]) == 1
    assert data["suppressed_reasons"] == []
    assert data["skipped_recipients"] == []

    rows = (await async_session.execute(select(Email))).scalars().all()
    assert len(rows) == 1
    assert rows[0].direction == "inbound"
    assert rows[0].status == "received"


@pytest.mark.asyncio
async def test_replay_short_circuits(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    inbound_enabled,
    override_internal_deps,
):
    org_id = uuid.uuid4()
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

    body = json.dumps(_payload(message_id="dup", recipient=domain.domain)).encode()
    r1 = await client.post("/internal/ses-events", content=body, headers=_signed(body))
    r2 = await client.post("/internal/ses-events", content=body, headers=_signed(body))
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["email_ids"] == r2.json()["email_ids"]

    rows = (await async_session.execute(select(Email))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_spam_verdict_suppresses(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    inbound_enabled,
    override_internal_deps,
):
    org_id = uuid.uuid4()
    domain = EmailDomain(
        organization_id=org_id,
        kind="hail_mail",
        domain="carol+initech@mail.hail.so",
        local_prefix_user="carol",
        local_prefix_org="initech",
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    async_session.add(domain)
    await async_session.commit()

    body = json.dumps(
        _payload(message_id="spam1", recipient=domain.domain, spam="FAIL")
    ).encode()
    resp = await client.post(
        "/internal/ses-events", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200
    assert resp.json()["suppressed_reasons"] == ["spam"]


@pytest.mark.asyncio
async def test_malformed_signed_body_returns_400(
    client, inbound_enabled, override_internal_deps
):
    body = json.dumps({"not": "a ses envelope"}).encode()
    resp = await client.post(
        "/internal/ses-events", content=body, headers=_signed(body)
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_enabled_but_missing_hmac_secret_returns_503(client, monkeypatch):
    monkeypatch.setattr(settings, "hail_inbound_enabled", True)
    monkeypatch.setattr(settings, "hail_inbound_hmac_secret", "")
    resp = await client.post("/internal/ses-events", content=b"{}")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_inbound_meters_one_usage_event_per_created_row(
    client, async_session, inbound_enabled, override_internal_deps
):
    from unittest.mock import patch

    from sqlalchemy import func

    from hailhq.core.models import UsageEvent

    # Insert an org + hail_mail domain routing "smoke+acme@mail.hail.so".
    org_id = await _insert_inbound_domain(async_session, user="smoke", org="acme")
    body = json.dumps(
        _payload(message_id="meter-1", recipient="smoke+acme@mail.hail.so")
    ).encode()

    with patch("hailhq.api.usage.notify_usage_event_recorded"):
        r1 = await client.post(
            "/internal/ses-events", content=body, headers=_signed(body)
        )
        r2 = await client.post(
            "/internal/ses-events", content=body, headers=_signed(body)
        )
    assert r1.status_code == 200 and r2.status_code == 200

    count = (
        await async_session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(
                UsageEvent.organization_id == org_id,
                UsageEvent.channel == "email",
            )
        )
    ).scalar_one()
    assert count == 1  # first delivery metered; replay not metered
