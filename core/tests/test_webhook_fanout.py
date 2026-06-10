import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from hailhq.core.models import EmailDomain, WebhookDelivery, WebhookSubscription
from hailhq.core.webhook_fanout import build_event_data, fanout_email_event


async def _seed_domain_with_webhook(session, org_id):
    domain = EmailDomain(
        organization_id=org_id,
        kind="hail_mail",
        domain="alice+acme@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org="acme",
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
        inbound_enabled=True,
        webhook_url="https://example.com/per-domain",
        webhook_secret_encrypted="hash",
    )
    session.add(domain)
    await session.commit()
    return domain


def _data():
    return build_event_data(
        email_id="00000000-0000-0000-0000-000000000001",
        direction="inbound",
        from_address="a@b",
        to_addresses=["c@d"],
        subject="s",
        message_id="<m>",
        in_reply_to=None,
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        raw_url="https://api.hail.so/emails/x/raw",
        attachments=[],
    )


@pytest.mark.asyncio
async def test_fanout_creates_per_domain_and_subscription_deliveries(async_session):
    org_id = uuid.uuid4()
    domain = await _seed_domain_with_webhook(async_session, org_id)
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://example.com/firehose",
        secret_encrypted="hash",
        event_types=["email.received"],
    )
    async_session.add(sub)
    await async_session.commit()

    n = await fanout_email_event(
        async_session,
        organization_id=org_id,
        email_domain_id=domain.id,
        event_type="email.received",
        event_id=domain.id,
        data=_data(),
    )
    assert n == 2

    rows = (await async_session.execute(select(WebhookDelivery))).scalars().all()
    targets = {(r.subscription_id, r.email_domain_id) for r in rows}
    assert (sub.id, None) in targets
    assert (None, domain.id) in targets


@pytest.mark.asyncio
async def test_fanout_skips_subscription_with_non_matching_event_type(async_session):
    org_id = uuid.uuid4()
    domain = await _seed_domain_with_webhook(async_session, org_id)
    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=["email.bounced"],
        )
    )
    await async_session.commit()

    n = await fanout_email_event(
        async_session,
        organization_id=org_id,
        email_domain_id=domain.id,
        event_type="email.received",
        event_id=domain.id,
        data=_data(),
    )
    # Only the per-domain webhook fires; the subscription doesn't match the event.
    assert n == 1


@pytest.mark.asyncio
async def test_fanout_skips_per_domain_when_inbound_disabled(async_session):
    org_id = uuid.uuid4()
    domain = await _seed_domain_with_webhook(async_session, org_id)
    domain.inbound_enabled = False
    await async_session.commit()

    n = await fanout_email_event(
        async_session,
        organization_id=org_id,
        email_domain_id=domain.id,
        event_type="email.received",
        event_id=domain.id,
        data=_data(),
    )
    assert n == 0


@pytest.mark.asyncio
async def test_fanout_skips_disabled_subscription(async_session):
    org_id = uuid.uuid4()
    domain = await _seed_domain_with_webhook(async_session, org_id)
    domain.inbound_enabled = False
    await async_session.commit()

    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=["email.received"],
            status="disabled",
        )
    )
    await async_session.commit()

    n = await fanout_email_event(
        async_session,
        organization_id=org_id,
        email_domain_id=domain.id,
        event_type="email.received",
        event_id=domain.id,
        data=_data(),
    )
    assert n == 0


def test_build_event_data_passes_through_attachments():
    data = build_event_data(
        email_id="x",
        direction="inbound",
        from_address="a@b",
        to_addresses=["c@d"],
        subject="s",
        message_id=None,
        in_reply_to=None,
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        raw_url=None,
        attachments=[
            {
                "id": "a1",
                "filename": "f.pdf",
                "content_type": "application/pdf",
                "size_bytes": 10,
                "content_id": None,
                "url": "https://api.hail.so/emails/x/attachments/a1",
            }
        ],
    )
    assert data["attachments"][0]["filename"] == "f.pdf"
    assert data["spam_verdict"] == "PASS"
