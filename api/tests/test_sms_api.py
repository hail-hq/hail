"""Tests for POST/GET /sms."""

from __future__ import annotations

import uuid

from twilio.request_validator import RequestValidator

from .conftest import insert_org_and_key  # noqa: F401

STATUS_AUTH_TOKEN = "test-twilio-auth-token"
STATUS_URL = "http://t/sms/status"


def _signed_status_form(
    params: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    sig = RequestValidator(STATUS_AUTH_TOKEN).compute_signature(STATUS_URL, params)
    return params, {"X-Twilio-Signature": sig}


async def _seed_sent_sms(async_session, organization_id, *, provider_message_sid: str):
    from hailhq.core.models import Sms

    sms = Sms(
        organization_id=organization_id,
        from_e164="+14155559999",
        to_e164="+14155551234",
        direction="outbound",
        status="sent",
        body="hi",
        provider_message_sid=provider_message_sid,
    )
    async_session.add(sms)
    await async_session.commit()
    await async_session.refresh(sms)
    return sms


async def _seed_dedicated_number(async_session, organization_id) -> None:
    from hailhq.core.models import PhoneNumber

    pn = PhoneNumber(
        organization_id=organization_id,
        e164="+14155559999",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_test",
        provisioning_state="active",
    )
    async_session.add(pn)
    await async_session.commit()


async def test_create_sms_requires_auth(client) -> None:
    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hi", "recipient_consent": True},
    )
    assert resp.status_code == 401


async def test_create_sms_requires_consent(client, async_session, org_and_key) -> None:
    org_id, _, plaintext = org_and_key
    await _seed_dedicated_number(async_session, org_id)

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hi", "recipient_consent": False},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422


async def test_create_sms_without_dedicated_number_422s(client, org_and_key) -> None:
    org_id, _, plaintext = org_and_key

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hi", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422
    # Hand-raised 422s carry the documented HTTPValidationError shape.
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"] == ["body", "from"]
    assert "dedicated" in detail[0]["msg"]


async def test_create_sms_explicit_from_requires_active_number(
    client, async_session, org_and_key
) -> None:
    """A named ``from`` must be an *active* org number — a pending/released
    row is rejected the same as an unregistered one."""
    from hailhq.core.models import PhoneNumber

    org_id, _, plaintext = org_and_key
    pn = PhoneNumber(
        organization_id=org_id,
        e164="+14155558888",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_pending",
        provisioning_state="pending",
    )
    async_session.add(pn)
    await async_session.commit()

    resp = await client.post(
        "/sms",
        json={
            "to": "+14155551234",
            "from": "+14155558888",
            "body": "hi",
            "recipient_consent": True,
        },
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail[0]["loc"] == ["body", "from"]


async def test_create_sms_happy_path(
    client, async_session, org_and_key, sms_mock
) -> None:
    from sqlalchemy import select

    from hailhq.core.models import UsageEvent

    org_id, _, plaintext = org_and_key
    await _seed_dedicated_number(async_session, org_id)

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hello", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "sent"
    assert body["from_e164"] == "+14155559999"
    assert body["to_e164"] == "+14155551234"
    assert body["segment_count"] == 1
    sms_mock.send_sms.assert_awaited_once()

    row = (
        await async_session.execute(
            select(UsageEvent).where(UsageEvent.channel == "sms")
        )
    ).scalar_one()
    assert row.ref == f"sms:{body['id']}:us"  # +14155551234 is a US number


async def test_create_sms_carrier_rejection_not_billed(
    client, async_session, org_and_key, sms_mock
) -> None:
    from sqlalchemy import select

    from hailhq.core.models import UsageEvent
    from hailhq.core.providers.sms import ProviderSmsResult

    org_id, _, plaintext = org_and_key
    await _seed_dedicated_number(async_session, org_id)

    sms_mock.send_sms.side_effect = None
    sms_mock.send_sms.return_value = ProviderSmsResult(
        provider_message_sid="SM_rejected",
        status="failed",
        segment_count=1,
        error_code="30006",
    )

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hello", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "30006"
    # A rejected message was never sent — sent_at stays null.
    assert body["sent_at"] is None

    # Filter on channel, not an exact ref string: a regression that bills the
    # rejected send under any ref shape must still fail this assertion.
    stmt = select(UsageEvent).where(UsageEvent.channel == "sms")
    rows = (await async_session.execute(stmt)).scalars().all()
    assert rows == []


async def test_create_sms_transport_failure_emits_sms_failed_webhook(
    client, async_session, org_and_key, sms_mock
) -> None:
    """A provider-level exception (transport failure) fans out sms.failed."""
    from sqlalchemy import select

    from hailhq.core.models import Sms, WebhookDelivery, WebhookSubscription

    org_id, _, plaintext = org_and_key
    await _seed_dedicated_number(async_session, org_id)

    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=["sms.failed"],
        )
    )
    await async_session.commit()

    sms_mock.send_sms.side_effect = RuntimeError("carrier link down")

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hello", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 502

    sms = (await async_session.execute(select(Sms))).scalar_one()
    assert sms.status == "failed"

    rows = (
        (
            await async_session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "sms.failed"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload["data"]["id"] == str(sms.id)
    assert rows[0].payload["data"]["status"] == "failed"
    assert rows[0].payload["data"]["reason"] == "provider_error"


async def test_create_sms_carrier_rejection_emits_sms_failed_webhook(
    client, async_session, org_and_key, sms_mock
) -> None:
    """A carrier-level rejection (error_code / undelivered status) fans out
    sms.failed too — not just a transport exception."""
    from sqlalchemy import select

    from hailhq.core.models import WebhookDelivery, WebhookSubscription
    from hailhq.core.providers.sms import ProviderSmsResult

    org_id, _, plaintext = org_and_key
    await _seed_dedicated_number(async_session, org_id)

    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=["sms.failed"],
        )
    )
    await async_session.commit()

    sms_mock.send_sms.side_effect = None
    sms_mock.send_sms.return_value = ProviderSmsResult(
        provider_message_sid="SM_rejected",
        status="failed",
        segment_count=1,
        error_code="30006",
    )

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hello", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201
    sms_id = resp.json()["id"]

    rows = (
        (
            await async_session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "sms.failed"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload["data"]["id"] == sms_id
    assert rows[0].payload["data"]["error_code"] == "30006"


async def test_create_sms_happy_path_emits_no_sms_failed_webhook(
    client, async_session, org_and_key, sms_mock
) -> None:
    """A successful send must not fan out sms.failed — over-emit guard."""
    from sqlalchemy import select

    from hailhq.core.models import WebhookDelivery, WebhookSubscription

    org_id, _, plaintext = org_and_key
    await _seed_dedicated_number(async_session, org_id)

    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=["sms.failed"],
        )
    )
    await async_session.commit()

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hello", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "sent"

    rows = (
        (
            await async_session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "sms.failed"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_create_sms_blocked_by_suppression(
    client, async_session, org_and_key
) -> None:
    from hailhq.core.compliance_gate import add_suppression

    org_id, _, plaintext = org_and_key
    await _seed_dedicated_number(async_session, org_id)
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155551234",
        channel="sms",
        reason="opted out",
        source="manual",
    )
    await async_session.commit()

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hi", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 403


async def test_create_sms_4xx_is_cached_for_idempotent_retry(
    client, async_session, org_and_key
) -> None:
    """An early 4xx must be stored under the idempotency key — a same-key
    retry replays the failure instead of 409ing on the in-flight sentinel."""
    from hailhq.core.compliance_gate import add_suppression

    org_id, _, plaintext = org_and_key
    await _seed_dedicated_number(async_session, org_id)
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155551234",
        channel="sms",
        reason="opted out",
        source="manual",
    )
    await async_session.commit()

    payload = {"to": "+14155551234", "body": "hi", "recipient_consent": True}
    headers = {
        "Authorization": f"Bearer {plaintext}",
        "Idempotency-Key": "sms-retry-after-403",
    }

    first = await client.post("/sms", json=payload, headers=headers)
    assert first.status_code == 403

    retry = await client.post("/sms", json=payload, headers=headers)
    assert retry.status_code == 403
    assert retry.headers.get("Idempotency-Replay") == "true"


async def test_sms_lifecycle_surfaces_on_events_stream(
    client, async_session, org_and_key, sms_mock
) -> None:
    org_id, _, plaintext = org_and_key
    await _seed_dedicated_number(async_session, org_id)

    resp = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hello", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201
    sms_id = resp.json()["id"]

    events = await client.get(
        "/events",
        params={"id": f"sms:{sms_id}"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert events.status_code == 200
    items = events.json()["items"]
    assert len(items) == 1
    assert items[0]["source"] == "sms"
    assert items[0]["sms_id"] == sms_id
    assert items[0]["kind"] == "state_change"
    assert items[0]["payload"] == {"from": "queued", "to": "sent"}


async def test_get_sms_not_found(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.get(
        f"/sms/{uuid.uuid4()}", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 404


async def test_list_sms_empty(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.get("/sms", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


async def test_create_sms_to_germany_uses_platform_default_without_dedicated_number(
    client, org_and_key, sms_mock
) -> None:
    """No dedicated number needed at all for a no-registration corridor —
    the send goes out under the platform-default Sender ID."""
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/sms",
        json={"to": "+491701234567", "body": "hallo", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["from_e164"] == "HAIL"


async def test_create_sms_to_india_still_requires_dedicated_number(
    client, org_and_key
) -> None:
    """An excluded corridor keeps the dedicated-number requirement — without
    one the send 422s with the documented HTTPValidationError shape."""
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/sms",
        json={"to": "+919876543210", "body": "hi", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"] == ["body", "from"]
    assert "dedicated" in detail[0]["msg"]


async def test_sms_status_delivered_persists_and_fans_out(
    client, async_session, org_and_key, monkeypatch
) -> None:
    from sqlalchemy import select

    from hailhq.core.config import settings
    from hailhq.core.models import Sms, WebhookDelivery, WebhookSubscription

    monkeypatch.setattr(settings, "twilio_auth_token", STATUS_AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")

    org_id, _, _ = org_and_key
    sms = await _seed_sent_sms(async_session, org_id, provider_message_sid="SM123")
    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=["sms.delivered"],
        )
    )
    await async_session.commit()

    form, headers = _signed_status_form(
        {"MessageSid": "SM123", "MessageStatus": "delivered"}
    )
    resp = await client.post("/sms/status", data=form, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "applied"}

    refreshed = await async_session.get(Sms, sms.id)
    assert refreshed.status == "delivered"

    deliveries = (
        (
            await async_session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "sms.delivered"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(deliveries) == 1


async def test_sms_status_bad_signature_rejected(
    client, async_session, org_and_key, monkeypatch
) -> None:
    from hailhq.core.config import settings
    from hailhq.core.models import Sms

    monkeypatch.setattr(settings, "twilio_auth_token", STATUS_AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")

    org_id, _, _ = org_and_key
    sms = await _seed_sent_sms(async_session, org_id, provider_message_sid="SM123")

    resp = await client.post(
        "/sms/status",
        data={"MessageSid": "SM123", "MessageStatus": "delivered"},
        headers={"X-Twilio-Signature": "sha1=bogus"},
    )
    assert resp.status_code == 403

    refreshed = await async_session.get(Sms, sms.id)
    assert refreshed.status == "sent"


async def test_sms_status_duplicate_callback_is_idempotent(
    client, async_session, org_and_key, monkeypatch
) -> None:
    from sqlalchemy import select

    from hailhq.core.config import settings
    from hailhq.core.models import SmsEvent, WebhookDelivery, WebhookSubscription

    monkeypatch.setattr(settings, "twilio_auth_token", STATUS_AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")

    org_id, _, _ = org_and_key
    await _seed_sent_sms(async_session, org_id, provider_message_sid="SM123")
    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=["sms.delivered"],
        )
    )
    await async_session.commit()

    form, headers = _signed_status_form(
        {"MessageSid": "SM123", "MessageStatus": "delivered"}
    )
    first = await client.post("/sms/status", data=form, headers=headers)
    assert first.json() == {"status": "applied"}

    second = await client.post("/sms/status", data=form, headers=headers)
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}

    events = (
        (
            await async_session.execute(
                select(SmsEvent).where(SmsEvent.kind == "state_change")
            )
        )
        .scalars()
        .all()
    )
    assert len([e for e in events if e.payload.get("to") == "delivered"]) == 1

    deliveries = (
        (
            await async_session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "sms.delivered"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(deliveries) == 1


async def test_sms_status_unknown_sid_is_noop(
    client, async_session, monkeypatch
) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_auth_token", STATUS_AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")

    form, headers = _signed_status_form(
        {"MessageSid": "SM_nope", "MessageStatus": "delivered"}
    )
    resp = await client.post("/sms/status", data=form, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "unmatched"}


async def test_sms_status_failed_fans_out(
    client, async_session, org_and_key, monkeypatch
) -> None:
    from sqlalchemy import select

    from hailhq.core.config import settings
    from hailhq.core.models import Sms, WebhookDelivery, WebhookSubscription

    monkeypatch.setattr(settings, "twilio_auth_token", STATUS_AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")

    org_id, _, _ = org_and_key
    sms = await _seed_sent_sms(async_session, org_id, provider_message_sid="SM456")
    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=["sms.failed"],
        )
    )
    await async_session.commit()

    form, headers = _signed_status_form(
        {"MessageSid": "SM456", "MessageStatus": "failed"}
    )
    resp = await client.post("/sms/status", data=form, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "applied"}

    refreshed = await async_session.get(Sms, sms.id)
    assert refreshed.status == "failed"

    deliveries = (
        (
            await async_session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "sms.failed"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(deliveries) == 1
