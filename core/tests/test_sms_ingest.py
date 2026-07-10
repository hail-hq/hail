# core/tests/test_sms_ingest.py
"""Tests for inbound SMS ingest: org resolution, idempotent insert,
opt-out handling, and webhook fan-out."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from hailhq.core.models import (
    PhoneNumber,
    Sms,
    Suppression,
    WebhookDelivery,
    WebhookSubscription,
)
from hailhq.core.sms_ingest import ingest_inbound_sms


async def _seed_number(session, organization_id) -> PhoneNumber:
    pn = PhoneNumber(
        organization_id=organization_id,
        e164="+14155559999",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_test",
        provisioning_state="active",
    )
    session.add(pn)
    await session.flush()
    return pn


async def test_ingest_unknown_number_is_dropped_not_error(async_session) -> None:
    result = await ingest_inbound_sms(
        async_session,
        from_e164="+14155551234",
        to_e164="+19999999999",  # not registered to anyone
        body="hi",
        provider_message_sid="SM_test_1",
        opt_out_type=None,
    )
    assert result.dropped_reason == "unknown_number"
    assert result.sms_id is None


async def test_ingest_creates_inbound_row_and_fans_out(async_session) -> None:
    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/hook",
            secret_encrypted="fake",
            status="active",
            event_types=["sms.received"],
        )
    )
    await async_session.commit()

    result = await ingest_inbound_sms(
        async_session,
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="hello back",
        provider_message_sid="SM_test_2",
        opt_out_type=None,
    )
    await async_session.commit()

    assert result.sms_id is not None
    sms = (
        await async_session.execute(select(Sms).where(Sms.id == result.sms_id))
    ).scalar_one()
    assert sms.direction == "inbound"
    assert sms.status == "received"
    assert sms.organization_id == org_id

    deliveries = (
        (
            await async_session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "sms.received"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(deliveries) == 1


async def test_ingest_duplicate_message_sid_is_idempotent(async_session) -> None:
    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    await async_session.commit()

    first = await ingest_inbound_sms(
        async_session,
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="hi",
        provider_message_sid="SM_dupe",
        opt_out_type=None,
    )
    await async_session.commit()

    second = await ingest_inbound_sms(
        async_session,
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="hi",
        provider_message_sid="SM_dupe",
        opt_out_type=None,
    )
    await async_session.commit()

    assert second.sms_id == first.sms_id
    count = (
        (
            await async_session.execute(
                select(Sms).where(Sms.provider_message_sid == "SM_dupe")
            )
        )
        .scalars()
        .all()
    )
    assert len(count) == 1


async def test_ingest_stop_keyword_adds_suppression(async_session) -> None:
    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    await async_session.commit()

    await ingest_inbound_sms(
        async_session,
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="STOP",
        provider_message_sid="SM_stop",
        opt_out_type="STOP",
    )
    await async_session.commit()

    hit = (
        await async_session.execute(
            select(Suppression).where(
                Suppression.recipient == "+14155551234", Suppression.channel == "sms"
            )
        )
    ).scalar_one_or_none()
    assert hit is not None
    assert hit.source == "stop_keyword"


async def test_ingest_start_removes_suppression(async_session) -> None:
    from hailhq.core.compliance_gate import add_suppression

    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155551234",
        channel="sms",
        reason="prior stop",
        source="stop_keyword",
    )
    await async_session.commit()

    await ingest_inbound_sms(
        async_session,
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="START",
        provider_message_sid="SM_start",
        opt_out_type="START",
    )
    await async_session.commit()

    hit = (
        await async_session.execute(
            select(Suppression).where(
                Suppression.recipient == "+14155551234", Suppression.channel == "sms"
            )
        )
    ).scalar_one_or_none()
    assert hit is None
