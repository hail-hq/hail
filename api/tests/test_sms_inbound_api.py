"""Tests for POST /sms/inbound — the Twilio inbound webhook."""

from __future__ import annotations

import uuid

from twilio.request_validator import RequestValidator

AUTH_TOKEN = "test-twilio-auth-token"
INBOUND_URL = "http://t/sms/inbound"


def _signed_form(params: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    sig = RequestValidator(AUTH_TOKEN).compute_signature(INBOUND_URL, params)
    return params, {"X-Twilio-Signature": sig}


async def _seed_number(async_session, organization_id) -> None:
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


async def test_inbound_rejects_bad_signature(client, monkeypatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)
    params = {
        "From": "+14155551234",
        "To": "+14155559999",
        "Body": "hi",
        "MessageSid": "SM1",
    }
    resp = await client.post(
        "/sms/inbound", data=params, headers={"X-Twilio-Signature": "sha1=bogus"}
    )
    assert resp.status_code == 403


async def test_inbound_rejects_missing_signature_header(client, monkeypatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)
    params = {
        "From": "+14155551234",
        "To": "+14155559999",
        "Body": "hi",
        "MessageSid": "SM_no_sig",
    }
    resp = await client.post("/sms/inbound", data=params)
    assert resp.status_code == 403


async def test_inbound_accepts_valid_signature_and_creates_row(
    client, async_session, monkeypatch
) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")
    org_id = uuid.uuid4()
    await _seed_number(async_session, org_id)

    params = {
        "From": "+14155551234",
        "To": "+14155559999",
        "Body": "hi",
        "MessageSid": "SM_ok",
    }
    form, headers = _signed_form(params)
    resp = await client.post("/sms/inbound", data=form, headers=headers)
    assert resp.status_code == 200

    # The route commits on the same session the test holds (get_session is
    # overridden to async_session), so assert the message was actually
    # persisted — a 200 alone can't distinguish "stored" from "dropped".
    from sqlalchemy import select

    from hailhq.core.models import Sms

    row = (
        await async_session.execute(
            select(Sms).where(Sms.provider_message_sid == "SM_ok")
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.direction == "inbound"
    assert row.status == "received"
    assert row.organization_id == org_id
    assert row.from_e164 == "+14155551234"


async def test_inbound_help_sends_reply_when_enabled(
    client, async_session, monkeypatch, sms_mock
) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")
    monkeypatch.setattr(settings, "hail_sms_compliance_replies_enabled", True)
    await _seed_number(async_session, uuid.uuid4())

    params = {
        "From": "+14155551234",
        "To": "+14155559999",
        "Body": "HELP",
        "MessageSid": "SM_help_api",
    }
    form, headers = _signed_form(params)
    resp = await client.post("/sms/inbound", data=form, headers=headers)
    assert resp.status_code == 200
    sms_mock.send_sms.assert_awaited()


async def test_inbound_unknown_number_still_returns_200(client, monkeypatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")
    params = {
        "From": "+14155551234",
        "To": "+19999999999",
        "Body": "hi",
        "MessageSid": "SM_unknown",
    }
    form, headers = _signed_form(params)
    resp = await client.post("/sms/inbound", data=form, headers=headers)
    # Twilio expects 200 regardless, to avoid retry storms on numbers we don't own.
    assert resp.status_code == 200
