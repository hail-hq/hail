import base64
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hailhq.core.config import settings
from hailhq.core.models import Sms, SmsEvent, Suppression
from hailhq.core.providers.sms import ProviderSmsResult
from sqlalchemy import select


async def telnyx_number(db, org, add_phone_number):
    number = await add_phone_number(db, org, provider_resource_id=str(uuid4()))
    number.provider = "telnyx"
    number.messaging_service_sid = str(uuid4())
    await db.commit()
    return number


async def test_call_uses_telnyx_trunk_and_auth_header(
    client, async_session, org_and_key, add_phone_number, livekit_mock, monkeypatch
):
    org, _, key = org_and_key
    number = await telnyx_number(async_session, org, add_phone_number)
    monkeypatch.setattr(settings, "livekit_telnyx_sip_outbound_trunk_id", "ST_telnyx")
    monkeypatch.setattr(settings, "telnyx_sip_username", "hail-sip")
    response = await client.post(
        "/calls",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "to": "+14155559999",
            "from": number.e164,
            "system_prompt": "Hello",
            "recipient_consent": True,
        },
    )
    assert response.status_code == 201, response.text
    args = livekit_mock.create_sip_participant.await_args.kwargs
    assert args["sip_trunk_id"] == "ST_telnyx"
    assert args["headers"] == {"X-Telnyx-Username": "hail-sip"}
    assert args["from_e164"] == number.e164


async def test_sms_sender_routes_to_telnyx(
    client, async_session, org_and_key, add_phone_number, monkeypatch
):
    org, _, key = org_and_key
    number = await telnyx_number(async_session, org, add_phone_number)
    monkeypatch.setattr(settings, "telnyx_api_key", "test")
    monkeypatch.setattr(settings, "telnyx_public_key", "configured")
    send = AsyncMock(
        return_value=ProviderSmsResult(
            provider_message_sid=str(uuid4()), status="queued", segment_count=1
        )
    )
    monkeypatch.setattr(
        "hailhq.core.providers.sms.telnyx.TelnyxSmsProvider.send_sms", send
    )
    response = await client.post(
        "/sms",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "to": "+14155559999",
            "from": number.e164,
            "body": "Hello",
            "recipient_consent": True,
        },
    )
    assert response.status_code == 201, response.text
    assert send.await_args.kwargs["status_callback_url"].endswith("/sms/telnyx")
    row = (await async_session.execute(select(Sms))).scalar_one()
    assert row.provider == "telnyx"


def signed_event(monkeypatch, event):
    private = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        settings,
        "telnyx_public_key",
        base64.b64encode(private.public_key().public_bytes_raw()).decode(),
    )
    raw = json.dumps({"data": event}).encode()
    timestamp = str(int(time.time()))
    sig = base64.b64encode(private.sign(timestamp.encode() + b"|" + raw)).decode()
    return raw, {
        "telnyx-signature-ed25519": sig,
        "telnyx-timestamp": timestamp,
        "content-type": "application/json",
    }


async def test_signed_inbound_stop_is_org_scoped_and_deduplicated(
    client, async_session, org_and_key, add_phone_number, monkeypatch
):
    org, _, _ = org_and_key
    number = await telnyx_number(async_session, org, add_phone_number)
    monkeypatch.setattr(settings, "telnyx_api_key", "test")
    raw, headers = signed_event(
        monkeypatch,
        {
            "event_type": "message.received",
            "payload": {
                "id": str(uuid4()),
                "from": {"phone_number": "+14155559999"},
                "to": [{"phone_number": number.e164}],
                "text": "STOP",
            },
        },
    )
    for _ in range(2):
        response = await client.post("/sms/telnyx", content=raw, headers=headers)
        assert response.status_code == 200, response.text
    rows = (await async_session.execute(select(Sms))).scalars().all()
    assert len(rows) == 1 and rows[0].provider == "telnyx"
    suppression = (await async_session.execute(select(Suppression))).scalar_one()
    assert suppression.organization_id == org
    assert suppression.recipient == "+14155559999"
    assert (
        await client.post("/sms/telnyx", content=raw + b" ", headers=headers)
    ).status_code == 403


async def test_finalized_status_is_absorbing(
    client, async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    sid = str(uuid4())
    row = Sms(
        organization_id=org,
        provider="telnyx",
        provider_message_sid=sid,
        from_e164="+14155551234",
        to_e164="+14155559999",
        direction="outbound",
        status="sent",
        body="Hello",
    )
    async_session.add(row)
    await async_session.commit()
    for status in ["delivered", "delivered", "delivery_failed"]:
        raw, headers = signed_event(
            monkeypatch,
            {
                "event_type": "message.finalized",
                "payload": {
                    "id": sid,
                    "to": [{"phone_number": row.to_e164, "status": status}],
                },
            },
        )
        response = await client.post("/sms/telnyx", content=raw, headers=headers)
        assert response.status_code == 200
    await async_session.refresh(row)
    assert row.status == "delivered"
    events = (
        (await async_session.execute(select(SmsEvent).where(SmsEvent.sms_id == row.id)))
        .scalars()
        .all()
    )
    assert len(events) == 1


async def test_release_routes_to_stored_carrier(
    client,
    async_session,
    org_and_key,
    add_phone_number,
    monkeypatch,
    voice_provider_mock,
):
    org, _, key = org_and_key
    number = await telnyx_number(async_session, org, add_phone_number)
    monkeypatch.setattr(settings, "telnyx_api_key", "test")
    release = AsyncMock()
    monkeypatch.setattr(
        "hailhq.core.providers.telnyx.TelnyxClient.release_number", release
    )
    response = await client.delete(
        f"/numbers/{number.id}", headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 204
    release.assert_awaited_once_with(number.provider_resource_id)
    voice_provider_mock.release_number.assert_not_awaited()


async def test_enable_sms_does_not_reuse_a_twilio_service(
    client, async_session, org_and_key, add_phone_number, monkeypatch
):
    org, _, key = org_and_key
    twilio = await add_phone_number(async_session, org, e164="+14155550001")
    twilio.messaging_service_sid = "MG_twilio"
    number = await telnyx_number(async_session, org, add_phone_number)
    number.messaging_service_sid = None
    await async_session.commit()
    monkeypatch.setattr(settings, "telnyx_api_key", "test")
    monkeypatch.setattr(settings, "telnyx_public_key", "configured")
    profile_id = str(uuid4())
    create = AsyncMock(return_value=profile_id)
    attach = AsyncMock()
    monkeypatch.setattr(
        "hailhq.core.providers.sms.telnyx.TelnyxSmsProvider.ensure_messaging_service",
        create,
    )
    monkeypatch.setattr(
        "hailhq.core.providers.sms.telnyx.TelnyxSmsProvider.attach_number", attach
    )
    response = await client.post(
        f"/numbers/{number.id}/enable-sms", headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 200, response.text
    create.assert_awaited_once_with(organization_id=org, existing_sid=None)
    attach.assert_awaited_once_with(
        messaging_service_sid=profile_id,
        provider_resource_id=number.provider_resource_id,
    )


async def finalized(monkeypatch, sender, occurred_at):
    return signed_event(
        monkeypatch,
        {
            "event_type": "message.finalized",
            "occurred_at": occurred_at,
            "payload": {
                "id": str(uuid4()),
                "from": {"phone_number": sender},
                "to": [{"phone_number": "+14155559999", "status": "delivered"}],
            },
        },
    )


async def test_finalized_event_for_unrecorded_message_only_retries_when_racing_our_send(
    client, async_session, org_and_key, add_phone_number, monkeypatch
):
    org, _, _ = org_and_key
    number = await telnyx_number(async_session, org, add_phone_number)
    now = datetime.now(timezone.utc)
    for sender, occurred_at, expected in [
        (number.e164, now.isoformat(), 503),
        (number.e164, (now - timedelta(minutes=30)).isoformat(), 200),
        ("+14155550123", now.isoformat(), 200),
    ]:
        raw, headers = await finalized(monkeypatch, sender, occurred_at)
        response = await client.post("/sms/telnyx", content=raw, headers=headers)
        assert response.status_code == expected, (sender, response.text)


async def test_failed_number_cannot_be_released_or_enter_renewal_billing(
    client,
    async_session,
    org_and_key,
    add_phone_number,
    monkeypatch,
    voice_provider_mock,
):
    org, _, key = org_and_key
    number = await telnyx_number(async_session, org, add_phone_number)
    number.provisioning_state = "failed"
    number.provider_resource_id = None
    number.acquired_at = None
    await async_session.commit()
    release = AsyncMock()
    monkeypatch.setattr(
        "hailhq.core.providers.telnyx.TelnyxClient.release_number", release
    )
    response = await client.delete(
        f"/numbers/{number.id}", headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 409
    await async_session.refresh(number)
    assert number.provisioning_state == "failed"
    assert number.released_at is None
    release.assert_not_awaited()
    voice_provider_mock.release_number.assert_not_awaited()
