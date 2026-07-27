import uuid
from datetime import datetime, timezone

import pytest
from hailhq.core.models import EmailDomain, WebhookDelivery, WebhookSubscription
from hailhq.core.webhook_fanout import (
    build_event_data,
    fanout_call_event,
    fanout_email_event,
)
from sqlalchemy import select


async def _seed_domain(session, org_id):
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
        forward_to=["ops@acme.com"],
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
async def test_fanout_creates_one_delivery_per_matching_subscription(async_session):
    org_id = uuid.uuid4()
    domain = await _seed_domain(async_session, org_id)
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
    assert n == 1

    rows = (await async_session.execute(select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1
    # The single delivery is subscription-owned AND stamped with the source domain.
    assert rows[0].subscription_id == sub.id
    assert rows[0].email_domain_id == domain.id


@pytest.mark.asyncio
async def test_fanout_skips_non_matching_event_type(async_session):
    org_id = uuid.uuid4()
    domain = await _seed_domain(async_session, org_id)
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
    assert n == 0


@pytest.mark.asyncio
async def test_fanout_skips_disabled_subscription(async_session):
    org_id = uuid.uuid4()
    domain = await _seed_domain(async_session, org_id)
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


@pytest.mark.asyncio
async def test_fanout_stamps_null_domain_when_source_unknown(async_session):
    org_id = uuid.uuid4()
    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=["email.received"],
        )
    )
    await async_session.commit()

    n = await fanout_email_event(
        async_session,
        organization_id=org_id,
        email_domain_id=None,
        event_type="email.received",
        event_id=uuid.uuid4(),
        data=_data(),
    )
    assert n == 1
    row = (await async_session.execute(select(WebhookDelivery))).scalars().one()
    assert row.subscription_id is not None
    assert row.email_domain_id is None


@pytest.mark.asyncio
async def test_fanout_call_event_inserts_for_matching_sub(async_session):
    org_id = uuid.uuid4()
    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=["call.completed"],
        )
    )
    await async_session.commit()

    n = await fanout_call_event(
        async_session,
        organization_id=org_id,
        event_type="call.completed",
        event_id=uuid.uuid4(),
        data={"id": "c1", "status": "completed"},
    )
    assert n == 1

    rows = (await async_session.execute(select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "call.completed"
    assert rows[0].email_domain_id is None


@pytest.mark.asyncio
async def test_fanout_call_event_skips_non_matching(async_session):
    org_id = uuid.uuid4()
    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=["call.failed"],
        )
    )
    await async_session.commit()

    n = await fanout_call_event(
        async_session,
        organization_id=org_id,
        event_type="call.completed",
        event_id=uuid.uuid4(),
        data={"id": "c1"},
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
