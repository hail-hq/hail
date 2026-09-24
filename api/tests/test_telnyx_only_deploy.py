"""Telnyx-only deployments: Twilio credentials blank, real carrier dependencies."""

from unittest.mock import AsyncMock

import pytest
from hailhq.api.main import app
from hailhq.api.routes import numbers as numbers_routes
from hailhq.api.routes import sms as sms_routes
from hailhq.core.config import settings
from hailhq.core.models import PhoneNumber
from hailhq.core.providers.sms import ProviderSmsResult
from hailhq.core.providers.sms.telnyx import TelnyxSmsProvider


@pytest.fixture()
def telnyx_only(client, monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    monkeypatch.setattr(settings, "telnyx_api_key", "key")
    monkeypatch.setattr(settings, "telnyx_public_key", "public")
    monkeypatch.setattr(sms_routes, "_sms_provider_singleton", None)
    monkeypatch.setattr(numbers_routes, "_voice_provider_singleton", None)
    # Use the real dependencies instead of the conftest mocks.
    app.dependency_overrides.pop(sms_routes.get_sms_provider, None)
    app.dependency_overrides.pop(numbers_routes.get_voice_provider, None)
    return client


async def seed_telnyx_number(async_session, org_id, **kwargs):
    number = PhoneNumber(
        organization_id=org_id,
        e164="+351211234567",
        country_code="PT",
        number_type="local",
        provider="telnyx",
        provider_resource_id="telnyx-owned-id",
        provisioning_state="active",
        capabilities=["voice", "sms"],
        **kwargs,
    )
    async_session.add(number)
    await async_session.commit()
    return number


def test_default_providers_build_without_twilio_credentials(telnyx_only):
    assert sms_routes.get_sms_provider() is not None
    assert numbers_routes.get_voice_provider() is not None


async def test_delete_telnyx_number_without_twilio(
    telnyx_only, async_session, org_and_key, monkeypatch
):
    org_id, _, plaintext = org_and_key
    number = await seed_telnyx_number(async_session, org_id)
    release = AsyncMock()
    monkeypatch.setattr(numbers_routes, "release_telnyx_number", release)
    resp = await telnyx_only.delete(
        f"/numbers/{number.id}", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 204, resp.text
    release.assert_awaited_once_with("telnyx-owned-id")


async def test_enable_sms_telnyx_number_without_twilio(
    telnyx_only, async_session, org_and_key, monkeypatch
):
    org_id, _, plaintext = org_and_key
    number = await seed_telnyx_number(async_session, org_id)
    monkeypatch.setattr(
        TelnyxSmsProvider, "ensure_messaging_service", AsyncMock(return_value="MS_1")
    )
    monkeypatch.setattr(TelnyxSmsProvider, "attach_number", AsyncMock())
    resp = await telnyx_only.post(
        f"/numbers/{number.id}/enable-sms",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200, resp.text


async def test_send_sms_from_telnyx_number_without_twilio(
    telnyx_only, async_session, org_and_key, monkeypatch
):
    org_id, _, plaintext = org_and_key
    await seed_telnyx_number(async_session, org_id)
    send = AsyncMock(
        return_value=ProviderSmsResult(
            provider_message_sid="tx-1", status="queued", segment_count=1
        )
    )
    monkeypatch.setattr(TelnyxSmsProvider, "send_sms", send)
    resp = await telnyx_only.post(
        "/sms",
        json={"to": "+14155551234", "body": "hi", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.text
    send.assert_awaited_once()
