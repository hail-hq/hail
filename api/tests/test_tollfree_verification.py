"""Per-number TFV routing and free billing are independent of the website."""

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from hailhq.api.routes import verifications as routes
from hailhq.core.models import AccountCredit, CarrierVerification, PhoneNumber
from hailhq.core.sms_registration_gate import us_registration_block
from sqlalchemy.exc import IntegrityError


def verification(org_id, number_id):
    return CarrierVerification(
        organization_id=org_id,
        provider="twilio",
        purpose="sms",
        country_code="US",
        number_type="toll_free",
        number_id=number_id,
        subject_type="business",
        state="submitted",
        requirements_version="test",
        provider_refs={
            "registration_kind": "toll_free",
            "tollfree_verification_sid": "HH1",
        },
    )


async def test_two_tollfree_numbers_have_separate_live_registrations(async_session):
    org_id = uuid4()
    numbers = [
        PhoneNumber(
            organization_id=org_id,
            provider="twilio",
            provider_resource_id=f"PN{i}",
            e164=f"+1800555010{i}",
            country_code="US",
            number_type="toll_free",
            capabilities=["sms"],
            provisioning_state="active",
        )
        for i in range(2)
    ]
    async_session.add_all(numbers)
    await async_session.flush()
    rows = [verification(org_id, n.id) for n in numbers]
    async_session.add_all(rows)
    await async_session.commit()
    async_session.add(verification(org_id, numbers[0].id))
    with pytest.raises(IntegrityError):
        await async_session.flush()
    await async_session.rollback()


async def test_tollfree_carrier_route_requires_owned_number_and_matches_type(
    async_session,
):
    org_id = uuid4()
    number = PhoneNumber(
        organization_id=org_id,
        provider="twilio",
        provider_resource_id="PN1",
        e164="+18005550100",
        country_code="CA",
        number_type="toll_free",
        capabilities=["sms"],
        provisioning_state="active",
    )
    async_session.add(number)
    await async_session.commit()
    assert (
        await routes._sms_provider_name(
            async_session,
            org_id,
            number.id,
            "body",
            country_code="CA",
            number_type="toll_free",
        )
        == "twilio"
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await routes._sms_provider_name(
            async_session,
            org_id,
            number.id,
            "body",
            country_code="US",
            number_type="local",
        )
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await routes._sms_provider_name(
            async_session,
            uuid4(),
            number.id,
            "body",
            country_code="CA",
            number_type="toll_free",
        )
    assert exc.value.status_code == 404


async def test_tollfree_route_refuses_carrier_without_tollfree_verification(
    async_session,
):
    from fastapi import HTTPException

    org_id = uuid4()
    number = PhoneNumber(
        organization_id=org_id,
        provider="telnyx",
        provider_resource_id="TX1",
        e164="+18005550199",
        country_code="US",
        number_type="toll_free",
        capabilities=["voice", "sms"],
        provisioning_state="active",
    )
    async_session.add(number)
    await async_session.commit()
    with pytest.raises(HTTPException) as exc:
        await routes._sms_provider_name(
            async_session,
            org_id,
            number.id,
            "body",
            country_code="US",
            number_type="toll_free",
        )
    assert exc.value.status_code == 422
    assert "not available for this number" in str(exc.value.detail)


async def test_tollfree_to_us_and_canada_waits_for_exact_approved_number(async_session):
    org_id = uuid4()
    number = PhoneNumber(
        organization_id=org_id,
        provider="twilio",
        provider_resource_id="PN1",
        e164="+18005550100",
        country_code="CA",
        number_type="toll_free",
        capabilities=["sms"],
        provisioning_state="active",
    )
    async_session.add(number)
    await async_session.flush()
    for target in ("+12125550100", "+14165550100"):
        assert await us_registration_block(async_session, org_id, number, target)
    row = verification(org_id, number.id)
    row.state = "approved"
    async_session.add(row)
    await async_session.flush()
    number.sms_registration_id = row.id
    for target in ("+12125550100", "+14165550100"):
        assert (
            await us_registration_block(async_session, org_id, number, target) is None
        )


def test_tollfree_never_calls_legacy_website_fee_rater(monkeypatch):
    notify = Mock()
    monkeypatch.setattr(routes, "notify_sms_registration_changed", notify)
    routes._notify_billing([verification(uuid4(), uuid4())])
    assert not notify.called


@pytest.mark.parametrize(
    "supported,registration_kind",
    [(False, "toll_free"), (True, "toll_free"), (True, "")],
)
async def test_tollfree_submit_is_free_attaches_only_target_and_keeps_number_id(
    client, async_session, org_and_key, monkeypatch, supported, registration_kind
):
    import json

    from hailhq.api.main import app
    from hailhq.core.providers.verification.base import DraftResult, ProviderStatus

    from .test_verifications_api import FakeSmsCarrier

    class TollfreeCarrier(FakeSmsCarrier):
        name = "twilio"
        campaign_ref = "tollfree_verification_sid"

        async def create_draft(self, **kwargs):
            self.drafts.append(kwargs)
            return DraftResult(
                refs={
                    "registration_kind": "toll_free",
                    "tollfree_verification_sid": "HH1",
                    "provider_resource_id": kwargs["provider_resource_id"],
                    "e164": kwargs["e164"],
                }
            )

        def confirmation(self, refs):
            return None

    org_id, _, key = org_and_key
    from sqlalchemy import delete

    await async_session.execute(
        delete(AccountCredit).where(AccountCredit.organization_id == org_id)
    )
    numbers = [
        PhoneNumber(
            organization_id=org_id,
            provider="twilio",
            provider_resource_id=f"PN{i}",
            e164=f"+1800555010{i}",
            country_code="US",
            number_type="toll_free",
            capabilities=["sms"],
            provisioning_state="active",
        )
        for i in range(2)
    ]
    async_session.add_all(numbers)
    await async_session.commit()
    provider = TollfreeCarrier()
    provider.remote_status = ProviderStatus(state="approved")
    app.dependency_overrides[routes.get_verification_registry] = (
        lambda: lambda name, purpose="number": provider
    )
    monkeypatch.setattr(
        routes, "tollfree_verification_supported", AsyncMock(return_value=supported)
    )
    notify = Mock()
    monkeypatch.setattr(routes, "notify_sms_registration_changed", notify)
    try:
        response = await client.post(
            "/verifications",
            headers={"Authorization": f"Bearer {key}"},
            data={
                "purpose": "sms",
                "country_code": "US",
                "number_type": "toll_free",
                "number_id": str(numbers[0].id),
                "subject_type": "business",
                "fields": json.dumps(
                    {
                        "registration_kind": registration_kind,
                        "legal_name": "Acme",
                        "campaign_description": "Order updates",
                    }
                ),
                "address": json.dumps(
                    {
                        "customer_name": "Acme",
                        "street": "1 Main St",
                        "city": "Austin",
                        "region": "TX",
                        "postal_code": "78701",
                        "country_code": "US",
                    }
                ),
            },
            files={"documents": (None, "{}")},
        )
        if not supported or not registration_kind:
            assert response.status_code == (
                503 if not supported else 422
            ), response.text
            assert provider.drafts == []
            from sqlalchemy import select

            assert (
                await async_session.execute(select(CarrierVerification))
            ).scalars().all() == []
            assert not notify.called
            # /check and submission share the same type/kind and billing gates.
            check = await client.post(
                "/verifications/check",
                headers={"Authorization": f"Bearer {key}"},
                data={
                    "purpose": "sms",
                    "country_code": "US",
                    "number_type": "toll_free",
                    "number_id": str(numbers[0].id),
                    "subject_type": "business",
                    "fields": json.dumps(
                        {
                            "registration_kind": registration_kind,
                            "legal_name": "Acme",
                            "campaign_description": "Order updates",
                        }
                    ),
                },
                files={"documents": (None, "{}")},
            )
            assert check.status_code == response.status_code
            assert provider.drafts == []
            return
        assert response.status_code == 201, response.text
        assert response.json()["number_id"] == str(numbers[0].id)
        assert not notify.called
        routes._last_polled.clear()
        response = await client.get(
            f"/verifications/{response.json()['id']}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "approved"
        assert len(provider.attached) == 1
        assert provider.attached[0][1] == "PN0"
        assert not notify.called
    finally:
        app.dependency_overrides.pop(routes.get_verification_registry, None)


@pytest.mark.parametrize("approved", [False, True, "unknown"])
async def test_manual_tollfree_approval_keeps_existing_sender_working(
    client, async_session, org_and_key, sms_mock, monkeypatch, approved
):
    from hailhq.core import carrier_routing

    from .test_sms_api import _seed_dedicated_number, _us_text

    org_id, _, key = org_and_key
    await _seed_dedicated_number(
        async_session,
        org_id,
        e164="+18005550100",
        number_type="toll_free",
        register_sms=False,
    )
    lookup = AsyncMock(return_value=approved)
    from types import SimpleNamespace

    monkeypatch.setattr(
        carrier_routing,
        "sms_route",
        lambda provider: SimpleNamespace(tollfree_approved=lookup),
    )
    response = await client.post(
        "/sms", json=_us_text(), headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == (201 if approved is True else 422), response.text
    lookup.assert_awaited_once()
    assert lookup.await_args.kwargs["e164"] == "+18005550100"
    if approved is True:
        sms_mock.send_sms.assert_awaited_once()
    else:
        sms_mock.send_sms.assert_not_awaited()


async def test_local_preview_with_missing_primary_profile_returns_controlled_error(
    client, async_session, org_and_key, monkeypatch
):
    from hailhq.api.main import app
    from hailhq.core.config import settings
    from hailhq.core.providers.verification.twilio_sms import (
        TwilioSmsRegistrationProvider,
    )
    from twilio.rest import Client

    org_id, _, key = org_and_key
    number = PhoneNumber(
        organization_id=org_id,
        provider="twilio",
        provider_resource_id="PN1",
        e164="+12125550100",
        country_code="US",
        number_type="local",
        capabilities=["sms"],
        provisioning_state="active",
    )
    async_session.add(number)
    await async_session.commit()
    monkeypatch.setattr(settings, "twilio_primary_profile_sid", "")
    provider = TwilioSmsRegistrationProvider(
        account_sid="AC" + "0" * 32,
        auth_token="offline",
        client=Client("AC" + "0" * 32, "offline"),
    )
    app.dependency_overrides[routes.get_verification_registry] = (
        lambda: lambda name, purpose="number": provider
    )
    try:
        response = await client.post(
            "/verifications/sms/preview",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "subject_type": "business",
                "number_id": str(number.id),
                "fields": {},
            },
        )
        assert response.status_code == 503, response.text
        assert "carrier unavailable" in response.text
    finally:
        app.dependency_overrides.pop(routes.get_verification_registry, None)
