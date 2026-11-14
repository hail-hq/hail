"""Tests for POST/GET /sms."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from .conftest import approve_us_sms, insert_org_and_key  # noqa: F401

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


async def _seed_dedicated_number(
    async_session,
    organization_id,
    *,
    e164: str = "+14155559999",
    number_type: str = "local",
    provider: str = "twilio",
    register_sms: bool = True,
    country_code: str = "US",
) -> None:
    """Seed an active SMS number. US long codes need an approved ``sms``
    registration to send to the US, so ``register_sms`` adds one by default."""
    from hailhq.core.models import PhoneNumber

    pn = PhoneNumber(
        organization_id=organization_id,
        e164=e164,
        country_code=country_code,
        number_type=number_type,
        provider=provider,
        provider_resource_id=f"PN_{e164}",
        provisioning_state="active",
    )
    async_session.add(pn)
    await async_session.commit()
    if register_sms:
        await _approve_sms(async_session, organization_id, provider=provider)


async def _approve_sms(async_session, org_id, provider="twilio") -> None:
    await approve_us_sms(async_session, org_id, provider)


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
    _org_id, _, plaintext = org_and_key

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
    from hailhq.core.models import UsageEvent
    from sqlalchemy import select

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
    from hailhq.core.models import UsageEvent
    from hailhq.core.providers.sms import ProviderSmsResult
    from sqlalchemy import select

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
    from hailhq.core.models import Sms, WebhookDelivery, WebhookSubscription
    from sqlalchemy import select

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
    from hailhq.core.models import WebhookDelivery, WebhookSubscription
    from hailhq.core.providers.sms import ProviderSmsResult
    from sqlalchemy import select

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
    from hailhq.core.models import WebhookDelivery, WebhookSubscription
    from sqlalchemy import select

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


@pytest.mark.parametrize("name", ["twilio", "telnyx"])
async def test_sender_id_sms_goes_through_the_configured_carrier(
    client, async_session, org_and_key, sms_mock, monkeypatch, name
) -> None:
    from hailhq.core.config import settings
    from hailhq.core.models import Sms
    from hailhq.core.providers.sms.telnyx import TelnyxSmsProvider

    monkeypatch.setattr(settings, "sender_id_sms_carrier", name)
    monkeypatch.setattr(settings, "telnyx_api_key", "key")
    monkeypatch.setattr(settings, "telnyx_public_key", "public")
    monkeypatch.setattr(TelnyxSmsProvider, "send_sms", sms_mock.send_sms)
    profiles = AsyncMock(return_value="telnyx-profile")
    monkeypatch.setattr(TelnyxSmsProvider, "ensure_messaging_service", profiles)
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/sms",
        json={"to": "+491701234567", "body": "hallo", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.text
    sms = (await async_session.execute(select(Sms))).scalar_one()
    assert sms.provider == name
    sent = sms_mock.send_sms.call_args.kwargs
    assert sent["from_e164"] == "HAIL"
    if name == "telnyx":
        # Telnyx needs the org's messaging profile on a sender-ID send.
        assert sent["messaging_service_sid"] == "telnyx-profile"
        assert profiles.await_args.args[0] == sms.organization_id
    else:
        assert sent["messaging_service_sid"] is None
        profiles.assert_not_awaited()


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
    from hailhq.core.config import settings
    from hailhq.core.models import Sms, WebhookDelivery, WebhookSubscription
    from sqlalchemy import select

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
    from hailhq.core.config import settings
    from hailhq.core.models import SmsEvent, WebhookDelivery, WebhookSubscription
    from sqlalchemy import select

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
    from hailhq.core.config import settings
    from hailhq.core.models import Sms, WebhookDelivery, WebhookSubscription
    from sqlalchemy import select

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


async def test_sms_status_out_of_order_terminal_callback_is_absorbed(
    client, async_session, org_and_key, monkeypatch
) -> None:
    """A `failed` callback is terminal; a later, out-of-order `delivered`
    redelivery for the same message must not flip status back or fan out
    `sms.delivered` — Twilio's callback carries no ordering token, so the
    only safe rule is: once terminal, absorb everything else."""
    from hailhq.core.config import settings
    from hailhq.core.models import Sms, SmsEvent, WebhookDelivery, WebhookSubscription
    from sqlalchemy import select

    monkeypatch.setattr(settings, "twilio_auth_token", STATUS_AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")

    org_id, _, _ = org_and_key
    sms = await _seed_sent_sms(async_session, org_id, provider_message_sid="SM789")
    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=["sms.failed", "sms.delivered"],
        )
    )
    await async_session.commit()

    failed_form, failed_headers = _signed_status_form(
        {"MessageSid": "SM789", "MessageStatus": "failed"}
    )
    failed_resp = await client.post(
        "/sms/status", data=failed_form, headers=failed_headers
    )
    assert failed_resp.status_code == 200
    assert failed_resp.json() == {"status": "applied"}

    refreshed = await async_session.get(Sms, sms.id)
    assert refreshed.status == "failed"

    failed_deliveries = (
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
    assert len(failed_deliveries) == 1

    delivered_form, delivered_headers = _signed_status_form(
        {"MessageSid": "SM789", "MessageStatus": "delivered"}
    )
    delivered_resp = await client.post(
        "/sms/status", data=delivered_form, headers=delivered_headers
    )
    assert delivered_resp.status_code == 200
    assert delivered_resp.json() == {"status": "duplicate"}

    await async_session.refresh(refreshed)
    assert refreshed.status == "failed"

    failed_deliveries_after = (
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
    assert len(failed_deliveries_after) == 1

    delivered_deliveries = (
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
    assert len(delivered_deliveries) == 0

    events = (
        (
            await async_session.execute(
                select(SmsEvent).where(
                    SmsEvent.sms_id == sms.id, SmsEvent.kind == "state_change"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].payload == {"from": "sent", "to": "failed"}


async def test_sms_status_intermediate_status_is_ignored(
    client, async_session, org_and_key, monkeypatch
) -> None:
    from hailhq.core.config import settings
    from hailhq.core.models import Sms, SmsEvent, WebhookDelivery
    from sqlalchemy import select

    monkeypatch.setattr(settings, "twilio_auth_token", STATUS_AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")

    org_id, _, _ = org_and_key
    sms = await _seed_sent_sms(async_session, org_id, provider_message_sid="SM999")

    form, headers = _signed_status_form(
        {"MessageSid": "SM999", "MessageStatus": "queued"}
    )
    resp = await client.post("/sms/status", data=form, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}

    refreshed = await async_session.get(Sms, sms.id)
    assert refreshed.status == "sent"

    events = (
        (await async_session.execute(select(SmsEvent).where(SmsEvent.sms_id == sms.id)))
        .scalars()
        .all()
    )
    assert events == []

    deliveries = (await async_session.execute(select(WebhookDelivery))).scalars().all()
    assert deliveries == []


async def test_sms_status_missing_sid_is_unmatched(
    client, async_session, org_and_key, monkeypatch
) -> None:
    from hailhq.core.config import settings
    from hailhq.core.models import Sms, WebhookDelivery
    from sqlalchemy import select

    monkeypatch.setattr(settings, "twilio_auth_token", STATUS_AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")

    org_id, _, _ = org_and_key
    # A "sent" SMS with a null provider_message_sid: if the missing-MessageSid
    # guard were absent, `Sms.provider_message_sid == None` would match this
    # row (and any other null-sid row), triggering MultipleResultsFound or a
    # wrong-row status transition.
    sms = await _seed_sent_sms(async_session, org_id, provider_message_sid=None)

    form, headers = _signed_status_form({"MessageStatus": "delivered"})
    resp = await client.post("/sms/status", data=form, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "unmatched"}

    refreshed = await async_session.get(Sms, sms.id)
    assert refreshed.status == "sent"

    deliveries = (await async_session.execute(select(WebhookDelivery))).scalars().all()
    assert deliveries == []


def _us_text(**extra) -> dict:
    return {
        "to": "+14155550123",
        "body": "hi",
        "recipient_consent": True,
        **extra,
    }


@pytest.mark.parametrize(
    "country_code,e164",
    [
        ("US", "+14155559999"),
        ("VI", "+13405550100"),
        ("PR", "+17875550100"),
        ("GU", "+16715550100"),
        ("MP", "+16705550100"),
        ("AS", "+16845550100"),
    ],
)
async def test_us_to_us_from_unregistered_long_code_is_422(
    client, async_session, org_and_key, sms_mock, country_code, e164
) -> None:
    org_id, _, key = org_and_key
    await _seed_dedicated_number(
        async_session, org_id, register_sms=False, country_code=country_code, e164=e164
    )
    resp = await client.post(
        "/sms", json=_us_text(), headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 422, resp.text
    assert "SMS registration" in resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "from"]
    sms_mock.send_sms.assert_not_awaited()


async def test_us_to_us_passes_when_registered(
    client, async_session, org_and_key, sms_mock
) -> None:
    org_id, _, key = org_and_key
    await _seed_dedicated_number(async_session, org_id, register_sms=False)
    await _approve_sms(async_session, org_id)
    resp = await client.post(
        "/sms", json=_us_text(), headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 201, resp.text


async def test_number_not_on_the_registration_is_422(
    client, async_session, org_and_key, sms_mock
) -> None:
    # The org is registered, but this number was bought after and never
    # joined the campaign.
    org_id, _, key = org_and_key
    await _seed_dedicated_number(async_session, org_id)
    await _seed_dedicated_number(
        async_session, org_id, e164="+14155558888", register_sms=False
    )
    resp = await client.post(
        "/sms",
        json=_us_text(**{"from": "+14155558888"}),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text
    assert "SMS registration" in resp.text


async def test_no_from_prefers_a_registered_number(
    client, async_session, org_and_key, sms_mock
) -> None:
    org_id, _, key = org_and_key
    # The oldest number is not registered; the newer one is.
    await _seed_dedicated_number(
        async_session, org_id, e164="+14155558888", register_sms=False
    )
    await _seed_dedicated_number(
        async_session, org_id, e164="+14155557777", register_sms=False
    )
    from hailhq.core.models import CarrierVerification, PhoneNumber

    row = CarrierVerification(
        organization_id=org_id,
        provider="twilio",
        country_code="US",
        number_type="local",
        subject_type="business",
        purpose="sms",
        state="approved",
        provider_refs={},
        requirements_version="x",
    )
    async_session.add(row)
    await async_session.flush()
    newer = (
        await async_session.execute(
            select(PhoneNumber).where(PhoneNumber.e164 == "+14155557777")
        )
    ).scalar_one()
    newer.sms_registration_id = row.id
    await async_session.commit()
    resp = await client.post(
        "/sms", json=_us_text(), headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["from_e164"] == "+14155557777"


async def test_registration_is_per_carrier(
    client, async_session, org_and_key, sms_mock
) -> None:
    org_id, _, key = org_and_key
    await _seed_dedicated_number(
        async_session,
        org_id,
        e164="+14155557777",
        provider="telnyx",
        register_sms=False,
    )
    await _approve_sms(async_session, org_id, provider="twilio")
    resp = await client.post(
        "/sms",
        json=_us_text(**{"from": "+14155557777"}),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text
    assert "SMS registration" in resp.text


async def test_unapproved_registration_does_not_count(
    client, async_session, org_and_key, sms_mock
) -> None:
    from hailhq.core.models import CarrierVerification

    org_id, _, key = org_and_key
    await _seed_dedicated_number(async_session, org_id, register_sms=False)
    await _approve_sms(async_session, org_id)
    row = (
        await async_session.execute(
            select(CarrierVerification).where(CarrierVerification.purpose == "sms")
        )
    ).scalar_one()
    row.state = "submitted"
    await async_session.commit()
    resp = await client.post(
        "/sms", json=_us_text(), headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 422, resp.text


async def test_unverified_tollfree_is_gated_and_non_us_longcode_is_not(
    client, async_session, org_and_key, sms_mock
) -> None:
    org_id, _, key = org_and_key
    await _seed_dedicated_number(
        async_session,
        org_id,
        e164="+18005550100",
        number_type="toll_free",
        register_sms=False,
    )
    headers = {"Authorization": f"Bearer {key}"}
    resp = await client.post("/sms", json=_us_text(), headers=headers)
    assert resp.status_code == 422, resp.text
    assert "Toll-free" in resp.text
    # Long code to a non-US destination is not gated either.
    await _seed_dedicated_number(
        async_session, org_id, e164="+14155556666", register_sms=False
    )
    resp = await client.post(
        "/sms",
        json=_us_text(to="+447700900123", **{"from": "+14155556666"}),
        headers=headers,
    )
    assert resp.status_code == 201, resp.text


async def test_us_long_code_to_canada_is_not_gated(
    client, async_session, org_and_key, sms_mock
) -> None:
    # +1 also covers Canada and the Caribbean; US registration covers US
    # destinations only.
    org_id, _, key = org_and_key
    await _seed_dedicated_number(async_session, org_id, register_sms=False)
    resp = await client.post(
        "/sms",
        json=_us_text(to="+14165550100"),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 201, resp.text


async def test_canadian_long_code_to_us_is_not_gated(
    client, async_session, org_and_key, sms_mock
) -> None:
    from hailhq.core.models import PhoneNumber

    org_id, _, key = org_and_key
    async_session.add(
        PhoneNumber(
            organization_id=org_id,
            e164="+14165550100",
            country_code="CA",
            number_type="local",
            provider="twilio",
            provider_resource_id="PN_ca",
            provisioning_state="active",
        )
    )
    await async_session.commit()
    resp = await client.post(
        "/sms",
        json=_us_text(**{"from": "+14165550100"}),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 201, resp.text
