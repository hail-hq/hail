"""OutboundForwardWorker: drains status='queued' forward rows via the provider."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

from hailhq.core.models import Email, EmailAttachment, EmailDomain
from hailhq.core.outbound_worker import OutboundForwardWorker
from hailhq.core.providers.email.base import ProviderSendResult


def _domain(org_id):
    return EmailDomain(
        organization_id=org_id,
        kind="hail_mail",
        domain="alice+acme@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org="acme",
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )


def _queued_forward(org_id, domain_id, inbound_id, headers=None):
    return Email(
        organization_id=org_id,
        email_domain_id=domain_id,
        direction="outbound",
        from_address="forwarder+acme@mail.hail.so",
        to_addresses=["ops@example.com"],
        reply_to="alice@example.com",
        subject="Fwd: hi",
        body_text="forwarded",
        status="queued",
        provider="ses",
        metadata_={
            "forwarded_from": str(inbound_id),
            "forward_headers": headers or {"X-Hail-Forward-Hops": "1"},
        },
    )


def _worker(async_session, provider, s3=None, usage_callback=None):
    @asynccontextmanager
    async def session_factory():
        yield async_session

    return OutboundForwardWorker(
        session_factory=session_factory,
        provider_factory=lambda: provider,
        s3_factory=lambda: s3 or AsyncMock(),
        usage_callback=usage_callback,
    )


@pytest.mark.asyncio
async def test_tick_sends_queued_forward_and_marks_sent(async_session):
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    inbound_id = uuid.uuid4()
    row = _queued_forward(org_id, dom.id, inbound_id)
    async_session.add(row)
    await async_session.commit()

    provider = AsyncMock()
    provider.send_email.return_value = ProviderSendResult(provider_message_id="ses-1")

    processed = await _worker(async_session, provider).tick()
    assert processed == 1

    refreshed = (
        await async_session.execute(select(Email).where(Email.id == row.id))
    ).scalar_one()
    assert refreshed.status == "sent"
    assert refreshed.provider_message_id == "ses-1"
    kwargs = provider.send_email.await_args.kwargs
    assert kwargs["headers"]["X-Hail-Forward-Hops"] == "1"


@pytest.mark.asyncio
async def test_tick_meters_sent_forward(async_session):
    """Each delivered forward is a billable outbound send: the worker invokes
    the usage callback once per row it marks ``sent``, keyed by org + row id."""
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    row = _queued_forward(org_id, dom.id, uuid.uuid4())
    async_session.add(row)
    await async_session.commit()
    email_id = row.id

    provider = AsyncMock()
    provider.send_email.return_value = ProviderSendResult(provider_message_id="ses-1")
    usage = AsyncMock()

    await _worker(async_session, provider, usage_callback=usage).tick()

    usage.assert_awaited_once_with(organization_id=org_id, forward_email_id=email_id)


@pytest.mark.asyncio
async def test_tick_does_not_meter_failed_forward(async_session):
    """A forward that never sends must not be billed."""
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    row = _queued_forward(org_id, dom.id, uuid.uuid4())
    async_session.add(row)
    await async_session.commit()

    provider = AsyncMock()
    provider.send_email.side_effect = RuntimeError("ses down")
    usage = AsyncMock()

    await _worker(async_session, provider, usage_callback=usage).tick()

    usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_survives_metering_failure(async_session):
    """Metering is best-effort: a raising usage callback must not roll back or
    re-send delivery — the row stays ``sent`` and the tick completes normally."""
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    row = _queued_forward(org_id, dom.id, uuid.uuid4())
    async_session.add(row)
    await async_session.commit()

    provider = AsyncMock()
    provider.send_email.return_value = ProviderSendResult(provider_message_id="ses-1")
    usage = AsyncMock(side_effect=RuntimeError("ledger down"))

    processed = await _worker(async_session, provider, usage_callback=usage).tick()

    assert processed == 1
    usage.assert_awaited_once()
    refreshed = (
        await async_session.execute(select(Email).where(Email.id == row.id))
    ).scalar_one()
    assert refreshed.status == "sent"


@pytest.mark.asyncio
async def test_tick_reattaches_inbound_attachments(async_session):
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()

    inbound = Email(
        organization_id=org_id,
        email_domain_id=dom.id,
        direction="inbound",
        from_address="alice@example.com",
        to_addresses=[dom.domain],
        subject="hi",
        body_text="x",
        status="received",
        provider="ses",
    )
    async_session.add(inbound)
    await async_session.flush()
    att = EmailAttachment(
        email_id=inbound.id,
        filename="invoice.pdf",
        content_type="application/pdf",
        size_bytes=8,
        s3_key=f"attachments/{inbound.id}/x",
    )
    async_session.add(att)
    row = _queued_forward(org_id, dom.id, inbound.id)
    async_session.add(row)
    await async_session.commit()

    provider = AsyncMock()
    provider.send_email.return_value = ProviderSendResult(provider_message_id="ses-2")
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"%PDF-1.4"

    await _worker(async_session, provider, s3).tick()

    kwargs = provider.send_email.await_args.kwargs
    assert kwargs["attachments"][0].filename == "invoice.pdf"
    assert kwargs["attachments"][0].payload == b"%PDF-1.4"


@pytest.mark.asyncio
async def test_provider_failure_marks_failed_with_reason(async_session):
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    row = _queued_forward(org_id, dom.id, uuid.uuid4())
    async_session.add(row)
    await async_session.commit()

    provider = AsyncMock()
    provider.send_email.side_effect = RuntimeError("ses down")

    await _worker(async_session, provider).tick()

    refreshed = (
        await async_session.execute(select(Email).where(Email.id == row.id))
    ).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.end_reason == "RuntimeError"


@pytest.mark.asyncio
async def test_tick_drains_multiple_rows(async_session):
    """The claim-one-per-transaction loop must still drain >1 row per tick."""
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    rows = [
        _queued_forward(org_id, dom.id, uuid.uuid4()),
        _queued_forward(org_id, dom.id, uuid.uuid4()),
    ]
    async_session.add_all(rows)
    await async_session.commit()

    provider = AsyncMock()
    provider.send_email.side_effect = [
        ProviderSendResult(provider_message_id="ses-n1"),
        ProviderSendResult(provider_message_id="ses-n2"),
    ]

    processed = await _worker(async_session, provider).tick()
    assert processed == 2

    statuses = (
        (
            await async_session.execute(
                select(Email.status).where(Email.id.in_([r.id for r in rows]))
            )
        )
        .scalars()
        .all()
    )
    assert statuses == ["sent", "sent"]


@pytest.mark.asyncio
async def test_malformed_forwarded_from_marks_failed(async_session):
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    row = Email(
        organization_id=org_id,
        email_domain_id=dom.id,
        direction="outbound",
        from_address="forwarder+acme@mail.hail.so",
        to_addresses=["ops@example.com"],
        subject="Fwd: hi",
        body_text="forwarded",
        status="queued",
        provider="ses",
        metadata_={"forwarded_from": "not-a-uuid", "forward_headers": {}},
    )
    async_session.add(row)
    await async_session.commit()

    provider = AsyncMock()
    await _worker(async_session, provider).tick()

    refreshed = (
        await async_session.execute(select(Email).where(Email.id == row.id))
    ).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.end_reason == "ValueError"
    provider.send_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_s3_transient_failure_leaves_row_queued(async_session):
    """An S3 blip must not permanently fail deliverable mail — the row
    stays queued and the next tick retries."""
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    inbound = Email(
        organization_id=org_id,
        email_domain_id=dom.id,
        direction="inbound",
        from_address="alice@example.com",
        to_addresses=[dom.domain],
        subject="hi",
        body_text="x",
        status="received",
        provider="ses",
    )
    async_session.add(inbound)
    await async_session.flush()
    async_session.add(
        EmailAttachment(
            email_id=inbound.id,
            filename="invoice.pdf",
            content_type="application/pdf",
            size_bytes=8,
            s3_key=f"attachments/{inbound.id}/x",
        )
    )
    row = _queued_forward(org_id, dom.id, inbound.id)
    async_session.add(row)
    await async_session.commit()

    provider = AsyncMock()
    s3 = AsyncMock()
    s3.fetch_raw.side_effect = RuntimeError("s3 down")

    processed = await _worker(async_session, provider, s3).tick()
    assert processed == 0

    refreshed = (
        await async_session.execute(select(Email).where(Email.id == row.id))
    ).scalar_one()
    assert refreshed.status == "queued"
    provider.send_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_s3_missing_object_marks_failed(async_session):
    """NoSuchKey means the attachment is gone for good — fail the row
    instead of retrying forever."""
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    inbound = Email(
        organization_id=org_id,
        email_domain_id=dom.id,
        direction="inbound",
        from_address="alice@example.com",
        to_addresses=[dom.domain],
        subject="hi",
        body_text="x",
        status="received",
        provider="ses",
    )
    async_session.add(inbound)
    await async_session.flush()
    async_session.add(
        EmailAttachment(
            email_id=inbound.id,
            filename="invoice.pdf",
            content_type="application/pdf",
            size_bytes=8,
            s3_key=f"attachments/{inbound.id}/x",
        )
    )
    row = _queued_forward(org_id, dom.id, inbound.id)
    async_session.add(row)
    await async_session.commit()

    provider = AsyncMock()
    s3 = AsyncMock()
    s3.fetch_raw.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "GetObject"
    )

    await _worker(async_session, provider, s3).tick()

    refreshed = (
        await async_session.execute(select(Email).where(Email.id == row.id))
    ).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.end_reason == "ClientError"
    provider.send_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_ignores_direct_post_emails_queued_rows(async_session):
    """POST /emails rows (no metadata.forwarded_from) are sent inline by the
    route — the worker must never race it."""
    org_id = uuid.uuid4()
    dom = _domain(org_id)
    async_session.add(dom)
    await async_session.flush()
    direct = Email(
        organization_id=org_id,
        email_domain_id=dom.id,
        direction="outbound",
        from_address=dom.domain,
        to_addresses=["bob@example.com"],
        subject="direct",
        body_text="x",
        status="queued",
        provider="ses",
    )
    async_session.add(direct)
    await async_session.commit()

    provider = AsyncMock()
    processed = await _worker(async_session, provider).tick()
    assert processed == 0
    provider.send_email.assert_not_awaited()
