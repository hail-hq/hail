"""Unit tests for ``TwilioVoiceProvider``.

We mock at the **HTTP boundary** (``responses``) rather than monkey-patching
Twilio SDK objects. That way the tests catch SDK API drift: if Twilio
renames a model field or moves an endpoint, these tests break the same
way real usage would.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
import responses
from hailhq.core.providers.voice import (
    NumberNotProvisionable,
    ProviderCallStatus,
    ProviderNumber,
    TwilioVoiceProvider,
)
from twilio.base.exceptions import TwilioRestException

ACCOUNT_SID = "ACtest1234567890abcdef1234567890ab"
AUTH_TOKEN = "test-auth-token"
API_BASE = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}"


@pytest.fixture()
def provider() -> TwilioVoiceProvider:
    return TwilioVoiceProvider(account_sid=ACCOUNT_SID, auth_token=AUTH_TOKEN)


@responses.activate
async def test_acquire_local_number_us(provider: TwilioVoiceProvider) -> None:
    responses.add(
        responses.GET,
        f"{API_BASE}/AvailablePhoneNumbers/US/Local.json",
        json={
            "available_phone_numbers": [
                {
                    "friendly_name": "(415) 555-1234",
                    "phone_number": "+14155551234",
                    "iso_country": "US",
                    "capabilities": {
                        "voice": True,
                        "SMS": True,
                        "MMS": True,
                        "fax": False,
                    },
                }
            ],
            "uri": f"/2010-04-01/Accounts/{ACCOUNT_SID}/AvailablePhoneNumbers/US/Local.json",
        },
        status=200,
    )
    responses.add(
        responses.POST,
        f"{API_BASE}/IncomingPhoneNumbers.json",
        json={
            "sid": "PN1234567890abcdef1234567890abcd",
            "account_sid": ACCOUNT_SID,
            "friendly_name": "(415) 555-1234",
            "phone_number": "+14155551234",
            "date_created": "Wed, 22 Apr 2026 12:00:00 +0000",
            "date_updated": "Wed, 22 Apr 2026 12:00:00 +0000",
            "capabilities": {
                "voice": True,
                "sms": True,
                "mms": True,
                "fax": False,
            },
            "status": "in-use",
            "uri": f"/2010-04-01/Accounts/{ACCOUNT_SID}/IncomingPhoneNumbers/PN1234567890abcdef1234567890abcd.json",
        },
        status=201,
    )

    number = await provider.acquire_number(
        country_code="US",
        number_type="local",
        capabilities=["voice", "sms"],
    )

    assert isinstance(number, ProviderNumber)
    assert number.provider_resource_id == "PN1234567890abcdef1234567890abcd"
    assert number.e164 == "+14155551234"
    assert number.country_code == "US"
    assert number.number_type == "local"
    assert "voice" in number.capabilities
    assert "sms" in number.capabilities
    assert "fax" not in number.capabilities

    search_qs = parse_qs(urlsplit(responses.calls[0].request.url).query)
    assert search_qs.get("VoiceEnabled") == ["true"]
    assert search_qs.get("SmsEnabled") == ["true"]
    assert "MmsEnabled" not in search_qs

    purchase_body = parse_qs(responses.calls[1].request.body)
    assert purchase_body == {"PhoneNumber": ["+14155551234"]}


@responses.activate
async def test_acquire_toll_free_number(provider: TwilioVoiceProvider) -> None:
    responses.add(
        responses.GET,
        f"{API_BASE}/AvailablePhoneNumbers/US/TollFree.json",
        json={
            "available_phone_numbers": [
                {
                    "phone_number": "+18005551234",
                    "iso_country": "US",
                    "capabilities": {
                        "voice": True,
                        "SMS": False,
                        "MMS": False,
                        "fax": False,
                    },
                }
            ],
            "uri": "/x",
        },
        status=200,
    )
    responses.add(
        responses.POST,
        f"{API_BASE}/IncomingPhoneNumbers.json",
        json={
            "sid": "PNtollfreesid000000000000000000aa",
            "account_sid": ACCOUNT_SID,
            "phone_number": "+18005551234",
            "capabilities": {"voice": True, "sms": False, "mms": False, "fax": False},
            "date_created": "Wed, 22 Apr 2026 12:00:00 +0000",
            "date_updated": "Wed, 22 Apr 2026 12:00:00 +0000",
            "status": "in-use",
        },
        status=201,
    )

    number = await provider.acquire_number(
        country_code="US",
        number_type="toll_free",
        capabilities=["voice"],
    )

    assert number.e164 == "+18005551234"
    assert number.number_type == "toll_free"
    assert "/AvailablePhoneNumbers/US/TollFree.json" in responses.calls[0].request.url


@responses.activate
async def test_acquire_raises_when_no_numbers_available(
    provider: TwilioVoiceProvider,
) -> None:
    responses.add(
        responses.GET,
        f"{API_BASE}/AvailablePhoneNumbers/US/Local.json",
        json={"available_phone_numbers": [], "uri": "/x"},
        status=200,
    )

    with pytest.raises(LookupError):
        await provider.acquire_number(
            country_code="US",
            number_type="local",
            capabilities=["voice"],
        )


@responses.activate
async def test_acquire_raises_not_provisionable_on_bundle_required(
    provider: TwilioVoiceProvider,
) -> None:
    """A 400 at purchase (e.g. GB mobile 'Bundle required') is translated into
    a typed NumberNotProvisionable — not a raw TwilioRestException that would
    surface as a 500."""
    responses.add(
        responses.GET,
        f"{API_BASE}/AvailablePhoneNumbers/GB/Mobile.json",
        json={
            "available_phone_numbers": [
                {
                    "phone_number": "+447700900123",
                    "iso_country": "GB",
                    "capabilities": {
                        "voice": True,
                        "SMS": True,
                        "MMS": False,
                        "fax": False,
                    },
                }
            ],
            "uri": "/x",
        },
        status=200,
    )
    responses.add(
        responses.POST,
        f"{API_BASE}/IncomingPhoneNumbers.json",
        json={
            "code": 21649,
            "message": (
                "Unable to create record: Bundle required and not provided "
                "for country: [GB] and numberType: [MOBILE]"
            ),
            "status": 400,
        },
        status=400,
    )

    with pytest.raises(NumberNotProvisionable) as excinfo:
        await provider.acquire_number(
            country_code="GB",
            number_type="mobile",
            capabilities=["voice", "sms"],
        )
    assert "Bundle required" in excinfo.value.detail


ORG_ID = "11111111-2222-3333-4444-555555555555"
BUNDLES_URL = "https://numbers.twilio.com/v2/RegulatoryCompliance/Bundles"


def _add_gb_mobile_search_and_bundle_400() -> None:
    responses.add(
        responses.GET,
        f"{API_BASE}/AvailablePhoneNumbers/GB/Mobile.json",
        json={
            "available_phone_numbers": [
                {
                    "phone_number": "+447700900123",
                    "iso_country": "GB",
                    "capabilities": {"voice": True, "SMS": True},
                }
            ],
            "uri": "/x",
        },
        status=200,
    )
    responses.add(
        responses.POST,
        f"{API_BASE}/IncomingPhoneNumbers.json",
        json={
            "code": 21649,
            "message": "Bundle required and not provided for country: [GB]",
            "status": 400,
        },
        status=400,
    )


def _bundles_page(*friendly_names: str) -> dict:
    return {
        "results": [
            {"sid": f"BU{i:032d}", "friendly_name": name, "status": "twilio-approved"}
            for i, name in enumerate(friendly_names)
        ],
        "meta": {
            "page": 0,
            "page_size": 20,
            "first_page_url": BUNDLES_URL,
            "previous_page_url": None,
            "url": BUNDLES_URL,
            "next_page_url": None,
            "key": "results",
        },
    }


@responses.activate
async def test_acquire_retries_with_org_bundle_when_bundle_required(
    provider: TwilioVoiceProvider,
) -> None:
    """A 400 'bundle required' + an approved ``hail-<org>`` bundle -> the
    purchase is retried once with BundleSid and succeeds."""
    _add_gb_mobile_search_and_bundle_400()
    responses.add(
        responses.GET,
        BUNDLES_URL,
        json=_bundles_page("someone-else", f"hail-{ORG_ID}"),
        status=200,
    )
    responses.add(
        responses.POST,
        f"{API_BASE}/IncomingPhoneNumbers.json",
        json={
            "sid": "PNgbmobile000000000000000000000000",
            "account_sid": ACCOUNT_SID,
            "phone_number": "+447700900123",
            "capabilities": {"voice": True, "sms": True, "mms": False},
            "status": "in-use",
        },
        status=201,
    )

    number = await provider.acquire_number(
        country_code="GB",
        number_type="mobile",
        capabilities=["voice", "sms"],
        organization_id=ORG_ID,
    )

    assert number.provider_resource_id == "PNgbmobile000000000000000000000000"
    bundle_qs = parse_qs(urlsplit(responses.calls[2].request.url).query)
    assert bundle_qs["Status"] == ["twilio-approved"]
    assert bundle_qs["FriendlyName"] == [f"hail-{ORG_ID}"]
    assert bundle_qs["IsoCountry"] == ["GB"]
    assert bundle_qs["NumberType"] == ["mobile"]
    retry_body = parse_qs(responses.calls[3].request.body)
    assert retry_body["BundleSid"] == [f"BU{1:032d}"]


@responses.activate
async def test_acquire_not_provisionable_when_org_has_no_approved_bundle(
    provider: TwilioVoiceProvider,
) -> None:
    """No approved ``hail-<org>`` bundle -> NumberNotProvisionable, and no
    second purchase attempt."""
    _add_gb_mobile_search_and_bundle_400()
    responses.add(
        responses.GET, BUNDLES_URL, json=_bundles_page("someone-else"), status=200
    )

    with pytest.raises(NumberNotProvisionable):
        await provider.acquire_number(
            country_code="GB",
            number_type="mobile",
            capabilities=["voice", "sms"],
            organization_id=ORG_ID,
        )
    purchases = [c for c in responses.calls if c.request.method == "POST"]
    assert len(purchases) == 1


@responses.activate
async def test_acquire_propagates_non_400_purchase_error(
    provider: TwilioVoiceProvider,
) -> None:
    """A 5xx at purchase is a transport failure and propagates unchanged — it
    must NOT be swallowed as NumberNotProvisionable (which would wrongly tell
    the caller the number is unavailable)."""
    responses.add(
        responses.GET,
        f"{API_BASE}/AvailablePhoneNumbers/US/Local.json",
        json={
            "available_phone_numbers": [
                {
                    "phone_number": "+14155551234",
                    "iso_country": "US",
                    "capabilities": {
                        "voice": True,
                        "SMS": True,
                        "MMS": True,
                        "fax": False,
                    },
                }
            ],
            "uri": "/x",
        },
        status=200,
    )
    responses.add(
        responses.POST,
        f"{API_BASE}/IncomingPhoneNumbers.json",
        json={"code": 20500, "message": "Internal server error", "status": 500},
        status=500,
    )

    with pytest.raises(TwilioRestException):
        await provider.acquire_number(
            country_code="US",
            number_type="local",
            capabilities=["voice", "sms"],
        )


@responses.activate
async def test_release_number(provider: TwilioVoiceProvider) -> None:
    responses.add(
        responses.DELETE,
        f"{API_BASE}/IncomingPhoneNumbers/PN9999999999999999999999999999.json",
        status=204,
    )

    result = await provider.release_number("PN9999999999999999999999999999")

    assert result is None
    assert len(responses.calls) == 1
    assert responses.calls[0].request.method == "DELETE"


@responses.activate
async def test_release_number_tolerates_carrier_404(
    provider: TwilioVoiceProvider,
) -> None:
    """A number already gone at Twilio (out-of-band release, or a retry of a
    half-completed release) is treated as released — no raise."""
    responses.add(
        responses.DELETE,
        f"{API_BASE}/IncomingPhoneNumbers/PN9999999999999999999999999999.json",
        json={"code": 20404, "message": "not found", "status": 404},
        status=404,
    )

    await provider.release_number("PN9999999999999999999999999999")

    assert len(responses.calls) == 1


@responses.activate
async def test_release_number_raises_on_other_errors(
    provider: TwilioVoiceProvider,
) -> None:
    responses.add(
        responses.DELETE,
        f"{API_BASE}/IncomingPhoneNumbers/PN9999999999999999999999999999.json",
        json={"code": 20003, "message": "auth error", "status": 401},
        status=401,
    )

    with pytest.raises(TwilioRestException):
        await provider.release_number("PN9999999999999999999999999999")


@responses.activate
async def test_get_call_status(provider: TwilioVoiceProvider) -> None:
    responses.add(
        responses.GET,
        f"{API_BASE}/Calls/CA1234567890abcdef1234567890abcd.json",
        json={
            "sid": "CA1234567890abcdef1234567890abcd",
            "account_sid": ACCOUNT_SID,
            "status": "completed",
            "start_time": "Wed, 22 Apr 2026 12:00:00 +0000",
            "end_time": "Wed, 22 Apr 2026 12:01:30 +0000",
            "duration": "90",
            "from": "+14155551234",
            "to": "+14155559999",
            "direction": "outbound-api",
            "date_created": "Wed, 22 Apr 2026 12:00:00 +0000",
            "date_updated": "Wed, 22 Apr 2026 12:01:30 +0000",
        },
        status=200,
    )

    status = await provider.get_call_status("CA1234567890abcdef1234567890abcd")

    assert isinstance(status, ProviderCallStatus)
    assert status.provider_call_sid == "CA1234567890abcdef1234567890abcd"
    assert status.status == "completed"
    assert status.answered_at == datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    assert status.ended_at == datetime(2026, 4, 22, 12, 1, 30, tzinfo=timezone.utc)
    assert status.duration_seconds == 90


@responses.activate
async def test_get_call_status_in_progress_has_nullable_fields(
    provider: TwilioVoiceProvider,
) -> None:
    responses.add(
        responses.GET,
        f"{API_BASE}/Calls/CAringing00000000000000000000000.json",
        json={
            "sid": "CAringing00000000000000000000000",
            "account_sid": ACCOUNT_SID,
            "status": "ringing",
            "start_time": None,
            "end_time": None,
            "duration": None,
            "date_created": "Wed, 22 Apr 2026 12:00:00 +0000",
            "date_updated": "Wed, 22 Apr 2026 12:00:00 +0000",
        },
        status=200,
    )

    status = await provider.get_call_status("CAringing00000000000000000000000")

    assert status.status == "ringing"
    assert status.answered_at is None
    assert status.ended_at is None
    assert status.duration_seconds is None


@responses.activate
async def test_hangup_call(provider: TwilioVoiceProvider) -> None:
    responses.add(
        responses.POST,
        f"{API_BASE}/Calls/CA1234567890abcdef1234567890abcd.json",
        json={
            "sid": "CA1234567890abcdef1234567890abcd",
            "account_sid": ACCOUNT_SID,
            "status": "completed",
            "date_created": "Wed, 22 Apr 2026 12:00:00 +0000",
            "date_updated": "Wed, 22 Apr 2026 12:01:30 +0000",
        },
        status=200,
    )

    result = await provider.hangup_call("CA1234567890abcdef1234567890abcd")

    assert result is None
    body = parse_qs(responses.calls[0].request.body)
    assert body == {"Status": ["completed"]}


def test_constructor_falls_back_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without explicit creds, the adapter pulls from ``settings``."""
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "twilio_account_sid", "ACfromsettings")
    monkeypatch.setattr(config.settings, "twilio_auth_token", "tokfromsettings")

    p = TwilioVoiceProvider()
    assert p.account_sid == "ACfromsettings"


def test_constructor_raises_without_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "twilio_account_sid", "")
    monkeypatch.setattr(config.settings, "twilio_auth_token", "")

    with pytest.raises(ValueError):
        TwilioVoiceProvider()


# ---------------------------------------------------------------------------
# Live smoke test — default-skipped. Acquires then immediately releases a
# real number so it exercises both endpoints without leaking paid resources.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("HAIL_TWILIO_LIVE"),
    reason="Set HAIL_TWILIO_LIVE=1 (and configure twilio creds) to run.",
)
async def test_live_acquire_then_release() -> None:  # pragma: no cover
    provider = TwilioVoiceProvider()
    number = await provider.acquire_number(
        country_code="US",
        number_type="local",
        capabilities=["voice"],
    )
    try:
        assert number.provider_resource_id.startswith("PN")
        assert number.e164.startswith("+1")
    finally:
        await provider.release_number(number.provider_resource_id)
