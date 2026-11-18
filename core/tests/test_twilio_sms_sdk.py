"""Exercise the pinned SDK object tree and request arguments, offline."""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from hailhq.core.providers.verification.twilio_sms import TwilioSmsRegistrationProvider
from twilio.http.http_client import HttpClient
from twilio.http.response import Response
from twilio.rest import Client

from .test_twilio_sms_registration import ADDRESS, FIELDS, FakeSms


class RecordingHttp(HttpClient):
    def __init__(self):
        super().__init__(logging.getLogger(__name__), False)
        self.calls = []
        self.brand_status = "PENDING"
        self.identity_status = "UNVERIFIED"

    def request(self, method, uri, **kwargs):
        self.calls.append((method, uri, kwargs))
        payload = {"sid": "IT1"}
        if "CustomerProfiles" in uri:
            payload = {
                "sid": "BU1",
                "status": "compliant",
                "results": [],
                "customer_profiles_entity_assignments": [],
            }
        elif "TrustProducts" in uri:
            payload = {"sid": "BU2", "status": "compliant", "results": []}
        elif "Addresses" in uri:
            payload = {"sid": "AD1"}
        elif "SupportingDocuments" in uri:
            payload = {"sid": "RD1"}
        elif "BrandRegistrations" in uri:
            payload = {
                "sid": "BN1",
                "status": self.brand_status,
                "identity_status": self.identity_status,
                "customer_profile_bundle_sid": "BU1",
            }
        elif "/Compliance/Usa2p" in uri:
            payload = {
                "sid": "QE1",
                "us_app_to_person": [],
                "meta": {"key": "us_app_to_person"},
            }
        elif "IncomingPhoneNumbers" in uri:
            payload = {"incoming_phone_numbers": []}
        return Response(200, json.dumps(payload))


def sdk_provider():
    http = RecordingHttp()
    client = Client("AC" + "0" * 32, "test", http_client=http)
    provider = TwilioSmsRegistrationProvider(
        account_sid=client.account_sid,
        auth_token="test",
        client=client,
        primary_profile_sid="BU0",
        sms_provider=FakeSms(),
    )
    return provider, http


async def test_pinned_sdk_builds_brand_and_polls_it():
    provider, http = sdk_provider()
    req = await provider.requirements("US", "local", "business")
    draft = await provider.create_draft(
        organization_id="org",
        contact_email="ops@hail.so",
        requirements=req,
        fields=FIELDS,
        address=ADDRESS,
        documents={},
    )
    assert not draft.problems
    assert draft.refs["brand_sid"] == "BN1"
    assert (await provider.status(draft.refs)).state == "pending"
    http.brand_status = "APPROVED"
    assert (await provider.status(draft.refs)).state == "pending"
    brand_calls = [
        (method, uri) for method, uri, _ in http.calls if "/BrandRegistrations" in uri
    ]
    assert brand_calls == [
        ("POST", "https://messaging.twilio.com/v1/a2p/BrandRegistrations"),
        ("GET", "https://messaging.twilio.com/v1/a2p/BrandRegistrations/BN1"),
        ("GET", "https://messaging.twilio.com/v1/a2p/BrandRegistrations/BN1"),
    ]
    assert any(
        "/Compliance/Usa2p" in uri and method == "POST" for method, uri, _ in http.calls
    )


async def test_pinned_sdk_resends_person_otp_and_reads_identity():
    provider, http = sdk_provider()
    http.brand_status = "APPROVED"
    refs = {
        "brand_sid": "BN1",
        "profile_sid": "BU1",
        "subject_type": "person",
        "otp_sent_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    }
    assert (await provider.status(refs)).state == "pending"
    assert any("/SmsOtp" in uri for _, uri, _ in http.calls)
    assert await provider.application_identity(refs, "person") is None


async def test_coding_failure_has_safe_diagnostics(caplog):
    import pytest
    from hailhq.core.providers.verification.base import VerificationProviderError

    provider, _ = sdk_provider()

    def broken(*args):
        raise AttributeError("secret=do-not-log contact=private@customer.com")

    provider._build = broken
    req = await provider.requirements("US", "local", "business")
    with pytest.raises(VerificationProviderError):
        await provider.create_draft(
            organization_id="org",
            contact_email="ops@hail.so",
            requirements=req,
            fields=FIELDS,
            address=ADDRESS,
            documents={},
        )
    assert "exception=AttributeError" in caplog.text
    assert "do-not-log" not in caplog.text
    assert "private@customer.com" not in caplog.text


@pytest.fixture(autouse=True)
def _no_tollfree_cache():
    from hailhq.core.providers.sms import twilio

    twilio._tollfree_approved_until.clear()
    yield
    twilio._tollfree_approved_until.clear()


def _tollfree_sms(records):
    """A Twilio SMS provider whose number PN1 has these TFV records."""
    from hailhq.core.providers.sms.twilio import TwilioSmsProvider

    provider, http = sdk_provider()
    account = provider.account_sid

    def request(method, uri, **kwargs):
        http.calls.append((method, uri, kwargs))
        if "IncomingPhoneNumbers/" in uri:
            payload = {
                "sid": "PN1",
                "account_sid": account,
                "phone_number": "+18005550100",
            }
        else:
            payload = {
                "verifications": [
                    {
                        "sid": f"HH{i}",
                        "account_sid": account,
                        "tollfree_phone_number_sid": "PN1",
                        **record,
                    }
                    for i, record in enumerate(records)
                ],
                "meta": {"key": "verifications"},
            }
        return Response(200, json.dumps(payload))

    http.request = request
    return (
        TwilioSmsProvider(
            account_sid=account, auth_token="test", client=provider._client
        ),
        http,
    )


@pytest.mark.parametrize(
    "records,approved",
    [
        # Rejected once, approved later: the newest record decides.
        (
            [
                {"status": "TWILIO_APPROVED", "date_created": "2026-11-02T00:00:00Z"},
                {"status": "TWILIO_REJECTED", "date_created": "2026-11-01T00:00:00Z"},
            ],
            True,
        ),
        (
            [
                {"status": "TWILIO_REJECTED", "date_created": "2026-11-02T00:00:00Z"},
                {"status": "TWILIO_APPROVED", "date_created": "2026-11-01T00:00:00Z"},
            ],
            False,
        ),
        ([], False),
    ],
)
async def test_tollfree_approval_uses_newest_record(records, approved):
    sms, _ = _tollfree_sms(records)
    assert (
        await sms.tollfree_approved(provider_resource_id="PN1", e164="+18005550100")
        is approved
    )


async def test_tollfree_approval_caches_yes_only():
    sms, http = _tollfree_sms([{"status": "TWILIO_APPROVED"}])
    for _ in range(3):
        assert await sms.tollfree_approved(
            provider_resource_id="PN1", e164="+18005550100"
        )
    assert len(http.calls) == 2  # One phone fetch and one record list.


async def test_tollfree_approval_does_not_cache_no():
    sms, http = _tollfree_sms([{"status": "PENDING_REVIEW"}])
    for _ in range(2):
        assert not await sms.tollfree_approved(
            provider_resource_id="PN1", e164="+18005550100"
        )
    assert len(http.calls) == 4


@pytest.mark.parametrize(
    "change,approved",
    [
        ({}, True),
        ({"tollfree_phone_number_sid": "PN-other"}, False),
        ({"account_sid": "AC-other"}, False),
        ({"status": "PENDING_REVIEW"}, False),
    ],
)
async def test_legacy_tollfree_requires_exact_current_owned_resource(change, approved):
    sms, http = _tollfree_sms([{"status": "TWILIO_APPROVED", **change}])
    assert (
        await sms.tollfree_approved(provider_resource_id="PN1", e164="+18005550100")
        is approved
    )
    assert all(method == "GET" for method, _, _ in http.calls)
