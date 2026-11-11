# core/tests/test_sms_ingest.py
"""Tests for inbound SMS ingest: org resolution, idempotent insert,
opt-out handling, and webhook fan-out."""

from __future__ import annotations

import uuid

from hailhq.core.models import (
    PhoneNumber,
    Sms,
    Suppression,
    WebhookDelivery,
    WebhookSubscription,
)
from hailhq.core.sms_ingest import ingest_inbound_sms
from sqlalchemy import select


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
        carrier="twilio",
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
        carrier="twilio",
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
        carrier="twilio",
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="hi",
        provider_message_sid="SM_dupe",
        opt_out_type=None,
    )
    await async_session.commit()

    second = await ingest_inbound_sms(
        async_session,
        carrier="twilio",
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
        carrier="twilio",
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
        carrier="twilio",
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


async def test_ingest_sets_to_number_id_and_null_from_number_id(async_session) -> None:
    org_id = uuid.uuid4()
    number = await _seed_number(async_session, org_id)
    await async_session.commit()

    result = await ingest_inbound_sms(
        async_session,
        carrier="twilio",
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="hi",
        provider_message_sid="SM_numfk",
        opt_out_type=None,
    )
    await async_session.commit()

    sms = (
        await async_session.execute(select(Sms).where(Sms.id == result.sms_id))
    ).scalar_one()
    assert sms.to_number_id == number.id
    assert sms.from_number_id is None


async def test_help_keyword_sends_reply_when_enabled(
    async_session, monkeypatch
) -> None:
    from unittest.mock import AsyncMock

    from hailhq.core import config
    from hailhq.core.providers.sms import ProviderSmsResult

    monkeypatch.setattr(config.settings, "hail_sms_compliance_replies_enabled", True)
    provider = AsyncMock()
    provider.send_sms.return_value = ProviderSmsResult(
        provider_message_sid="SM_reply", status="queued", segment_count=1
    )

    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    await async_session.commit()

    await ingest_inbound_sms(
        async_session,
        carrier="twilio",
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="HELP",
        provider_message_sid="SM_help",
        opt_out_type=None,
        provider=provider,
    )
    await async_session.commit()

    provider.send_sms.assert_awaited_once()
    kwargs = provider.send_sms.await_args.kwargs
    assert kwargs["from_e164"] == "+14155559999"  # org receiving number
    assert kwargs["to_e164"] == "+14155551234"  # external sender


async def test_stop_reply_nonterminal_status_persists_and_keeps_suppression(
    async_session, monkeypatch
) -> None:
    """A provider status outside sms_status_check (e.g. Twilio "accepted")
    must be normalized before the reply row is flushed — otherwise the flush
    raises and rolls back the inbound row + STOP suppression. Also asserts the
    persisted outbound reply row fields."""
    from unittest.mock import AsyncMock

    from hailhq.core import config
    from hailhq.core.models import Suppression
    from hailhq.core.providers.sms import ProviderSmsResult

    monkeypatch.setattr(config.settings, "hail_sms_compliance_replies_enabled", True)
    provider = AsyncMock()
    provider.send_sms.return_value = ProviderSmsResult(
        provider_message_sid="SM_reply_acc", status="accepted", segment_count=1
    )

    org_id = uuid.uuid4()
    number = await _seed_number(async_session, org_id)
    await async_session.commit()

    result = await ingest_inbound_sms(
        async_session,
        carrier="twilio",
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="STOP",
        provider_message_sid="SM_stop_acc",
        opt_out_type="STOP",
        provider=provider,
    )
    await async_session.commit()

    # Inbound row + STOP suppression survived (no rollback from a bad status).
    assert result.sms_id is not None
    suppression = (
        await async_session.execute(
            select(Suppression).where(Suppression.recipient == "+14155551234")
        )
    ).scalar_one_or_none()
    assert suppression is not None

    # The persisted outbound reply row has a normalized, CHECK-valid status.
    reply = (
        await async_session.execute(
            select(Sms).where(
                Sms.provider_message_sid == "SM_reply_acc", Sms.direction == "outbound"
            )
        )
    ).scalar_one()
    assert reply.status == "sent"
    assert reply.from_number_id == number.id
    assert reply.to_number_id is None
    assert reply.to_e164 == "+14155551234"


async def test_help_keyword_no_reply_when_disabled(async_session, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_sms_compliance_replies_enabled", False)
    provider = AsyncMock()

    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    await async_session.commit()

    await ingest_inbound_sms(
        async_session,
        carrier="twilio",
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="HELP",
        provider_message_sid="SM_help2",
        opt_out_type=None,
        provider=provider,
    )
    provider.send_sms.assert_not_awaited()


async def test_stop_writes_suppression_regardless_of_reply_flag(
    async_session, monkeypatch
) -> None:
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_sms_compliance_replies_enabled", False)

    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)
    await async_session.commit()

    await ingest_inbound_sms(
        async_session,
        carrier="twilio",
        from_e164="+14155551234",
        to_e164="+14155559999",
        body="STOP",
        provider_message_sid="SM_stop_flagoff",
        opt_out_type="STOP",
        provider=None,
    )
    await async_session.commit()

    hit = (
        await async_session.execute(
            select(Suppression).where(Suppression.recipient == "+14155551234")
        )
    ).scalar_one_or_none()
    assert hit is not None


async def test_browser_quota_still_acknowledges_stop_start_help_once(
    async_session, monkeypatch
):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    from hailhq.core.compliance_gate import is_suppressed
    from hailhq.core.config import settings
    from hailhq.core.models import Call
    from hailhq.core.providers.sms import ProviderSmsResult

    org = uuid.uuid4()
    call = Call(
        organization_id=org,
        transport="browser",
        provider="browser",
        status="in_progress",
        voice_config={},
        max_duration_seconds=300,
        answered_at=datetime.now(timezone.utc),
        metadata_={"browser_trial": True, "browser_contacts": {"sms": "+46705550123"}},
    )
    async_session.add(call)
    await async_session.flush()
    number = PhoneNumber(
        organization_id=None,
        is_pool=True,
        e164="+14155550111",
        country_code="US",
        number_type="local",
        provider="twilio",
        provider_resource_id="pool",
        provisioning_state="active",
        reserved_call_id=call.id,
    )
    async_session.add(number)
    await async_session.flush()
    call.metadata_ = {**call.metadata_, "browser_sms_number_id": str(number.id)}
    for i in range(3):
        async_session.add(
            Sms(
                organization_id=org,
                direction="inbound",
                status="received",
                from_e164="+46705550123",
                to_e164=number.e164,
                to_number_id=number.id,
                body="Test",
                provider="twilio",
                provider_message_sid=f"quota-{i}",
                metadata_={"browser_trial": True, "call_id": str(call.id)},
            )
        )
    await async_session.commit()
    monkeypatch.setattr(settings, "hail_sms_compliance_replies_enabled", True)
    provider = AsyncMock()
    replies = []

    async def send(**kwargs):
        replies.append(kwargs["body"])
        return ProviderSmsResult(
            provider_message_sid=f"ack-{len(replies)}",
            status="sent",
            segment_count=1,
            error_code=None,
        )

    provider.send_sms.side_effect = send

    async def incoming(keyword, sid, sender_provider=provider):
        result = await ingest_inbound_sms(
            async_session,
            carrier="twilio",
            from_e164="+46705550123",
            to_e164=number.e164,
            body=keyword,
            provider_message_sid=sid,
            opt_out_type=None,
            provider=sender_provider,
        )
        await async_session.commit()
        return result

    for keyword, reply in (
        ("STOP", settings.hail_sms_stop_reply),
        ("START", settings.hail_sms_start_reply),
        ("HELP", settings.hail_sms_help_reply),
    ):
        await incoming(keyword, keyword)
        assert replies[-1] == reply
        count = provider.send_sms.await_count
        await incoming(keyword, keyword)
        assert provider.send_sms.await_count == count
        assert await is_suppressed(async_session, org, "+46705550123", "sms") is (
            keyword == "STOP"
        )
    assert provider.send_sms.await_count == 3
    assert (
        await incoming("Ordinary fourth text", "fourth")
    ).dropped_reason == "browser_inbound_limit"
    assert (
        await incoming("STOP", "preview-stop", None)
    ).dropped_reason == "browser_inbound_limit"
    assert await is_suppressed(async_session, org, "+46705550123", "sms")
    assert provider.send_sms.await_count == 3

    await incoming("START", "preview-start", None)
    await incoming("HELP", "preview-help", None)
    assert not await is_suppressed(async_session, org, "+46705550123", "sms")
    assert provider.send_sms.await_count == 3
    rows = (
        (await async_session.execute(select(Sms).where(Sms.direction == "inbound")))
        .scalars()
        .all()
    )
    assert sum(bool(row.metadata_.get("browser_compliance")) for row in rows) == 3
    assert len(rows) == 6  # three normal texts plus three deduped control messages
