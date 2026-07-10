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


async def test_inbound_accepts_valid_signature_and_creates_row(
    client, async_session, monkeypatch
) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)
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


async def test_inbound_unknown_number_still_returns_200(client, monkeypatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)
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
