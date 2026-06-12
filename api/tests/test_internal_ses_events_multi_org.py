"""C1 regression: one SES delivery addressed to two orgs → two rows, no 500.

Before migration 0009 the global UNIQUE on emails.provider_message_id caused
the second INSERT to 500, rolling back both rows.  This test confirms the fix:
both orgs' Email rows are persisted and the route returns 200 with two ids.
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
    get_s3_inbound_client,
)
from hailhq.core.models import Email, EmailDomain
from hailhq.core.providers.email.inbound.ses import SesInboundProvider

FIX = Path(__file__).parent.parent.parent / "core" / "tests" / "fixtures" / "inbound"

HMAC_SECRET = "test-shared-secret"


def _signed(body: bytes) -> dict[str, str]:
    sig = hmac.new(HMAC_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hail-Signature": f"sha256={sig}"}


def _payload_multi(
    *,
    message_id: str,
    recipients: list[str],
    s3_key: str = "raw/abc",
) -> dict:
    return {
        "message_id": message_id,
        "envelope_from": "alice@example.com",
        "recipients": recipients,
        "verdicts": {
            "spam": "PASS",
            "virus": "PASS",
            "spf": "PASS",
            "dkim": "PASS",
            "dmarc": "PASS",
        },
        "s3_bucket": "hail-inbound-test-raw",
        "s3_key": s3_key,
        "timestamp": "2026-06-06T10:11:12Z",
    }


@pytest.fixture()
def inbound_enabled(monkeypatch: pytest.MonkeyPatch):
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "hail_inbound_enabled", True)
    monkeypatch.setattr(settings, "hail_inbound_hmac_secret", HMAC_SECRET)
    # Bucket is derived as `${prefix}-raw`, so this yields "hail-inbound-test-raw".
    monkeypatch.setattr(settings, "hail_inbound_email_name_prefix", "hail-inbound-test")
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")


@pytest.fixture()
def fake_s3():
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    return s3


@pytest.fixture()
def override_internal_deps(fake_s3):
    """Override provider + S3 deps so the test needs no real boto3 / env."""
    app.dependency_overrides[get_inbound_provider] = lambda: SesInboundProvider(
        hmac_secret=HMAC_SECRET
    )
    app.dependency_overrides[get_s3_inbound_client] = lambda: fake_s3
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_inbound_provider, None)
        app.dependency_overrides.pop(get_s3_inbound_client, None)


@pytest.mark.asyncio
async def test_two_orgs_same_provider_message_id_persists_both(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    inbound_enabled,
    override_internal_deps,
):
    """C1 regression: same SES receipt id addressed to two different orgs.

    Before 0009 the global UNIQUE on provider_message_id caused a 500 on the
    second row, rolling both back.  After 0009 (outbound-only partial unique)
    both rows land and the route returns 200 with two email_ids.
    """
    # Seed org A — hail-mail address alice+orgalpha@mail.hail.so
    # The routing lookup matches on (local_prefix_user, local_prefix_org).
    org_a = uuid.uuid4()
    domain_a = EmailDomain(
        organization_id=org_a,
        kind="hail_mail",
        domain="alice+orgalpha@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org="orgalpha",
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    async_session.add(domain_a)

    # Seed org B — hail-mail address bob+orgbeta@mail.hail.so
    org_b = uuid.uuid4()
    domain_b = EmailDomain(
        organization_id=org_b,
        kind="hail_mail",
        domain="bob+orgbeta@mail.hail.so",
        local_prefix_user="bob",
        local_prefix_org="orgbeta",
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    async_session.add(domain_b)
    await async_session.commit()

    # One SES delivery: same provider_message_id / same RFC Message-ID, two recipients
    # in two distinct orgs.  Before migration 0009 the global UNIQUE on
    # provider_message_id caused the second INSERT to fail; after 0009 both land.
    body = json.dumps(
        _payload_multi(
            message_id="ses-receipt-shared-1",
            recipients=[
                "alice+orgalpha@mail.hail.so",
                "bob+orgbeta@mail.hail.so",
            ],
        )
    ).encode()

    resp = await client.post(
        "/internal/ses-events", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert len(data["email_ids"]) == 2, (
        f"Expected 2 email_ids, got {data['email_ids']!r}. "
        "If this is 0 or 1, migration 0009 may not have run or the multi-org "
        "routing is broken."
    )

    # Confirm two distinct Email rows landed in two distinct orgs
    rows = (await async_session.execute(select(Email))).scalars().all()
    org_ids = {r.organization_id for r in rows}
    assert org_ids == {
        org_a,
        org_b,
    }, f"Expected rows for both orgs, got org_ids={org_ids!r}"
    for row in rows:
        assert row.direction == "inbound"
        assert row.status == "received"
        # Both rows share the same provider_message_id — this was the collision
        # point before 0009.
        assert row.provider_message_id == "ses-receipt-shared-1"
