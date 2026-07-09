"""Unit tests for ``TwilioSmsProvider``.

Same approach as ``test_twilio_voice.py``: mock at the HTTP boundary via
``responses`` rather than monkeypatching Twilio SDK objects, so SDK drift
surfaces as a test failure the same way real usage would.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import pytest
import responses

from hailhq.core.providers.sms import ProviderSmsResult, TwilioSmsProvider

ACCOUNT_SID = "ACtest1234567890abcdef1234567890ab"
AUTH_TOKEN = "test-auth-token"
API_BASE = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}"


@pytest.fixture()
def provider() -> TwilioSmsProvider:
    return TwilioSmsProvider(account_sid=ACCOUNT_SID, auth_token=AUTH_TOKEN)


@responses.activate
async def test_send_sms_success(provider: TwilioSmsProvider) -> None:
    responses.add(
        responses.POST,
        f"{API_BASE}/Messages.json",
        json={
            "sid": "SM1234567890abcdef1234567890abcd",
            "account_sid": ACCOUNT_SID,
            "to": "+14155551234",
            "from": "+14155559999",
            "body": "Hello from Hail",
            "status": "queued",
            "num_segments": "1",
            "error_code": None,
            "date_created": "Wed, 22 Apr 2026 12:00:00 +0000",
            "uri": f"/2010-04-01/Accounts/{ACCOUNT_SID}/Messages/SM1234567890abcdef1234567890abcd.json",
        },
        status=201,
    )

    result = await provider.send_sms(
        from_e164="+14155559999", to_e164="+14155551234", body="Hello from Hail"
    )

    assert isinstance(result, ProviderSmsResult)
    assert result.provider_message_sid == "SM1234567890abcdef1234567890abcd"
    assert result.status == "queued"
    assert result.segment_count == 1
    assert result.error_code is None

    sent_body = parse_qs(responses.calls[0].request.body)
    assert sent_body == {
        "To": ["+14155551234"],
        "From": ["+14155559999"],
        "Body": ["Hello from Hail"],
    }


@responses.activate
async def test_send_sms_multi_segment(provider: TwilioSmsProvider) -> None:
    long_body = "x" * 200  # over the 160-char single-segment threshold
    responses.add(
        responses.POST,
        f"{API_BASE}/Messages.json",
        json={
            "sid": "SM_multiseg",
            "account_sid": ACCOUNT_SID,
            "to": "+14155551234",
            "from": "+14155559999",
            "body": long_body,
            "status": "queued",
            "num_segments": "2",
            "error_code": None,
        },
        status=201,
    )

    result = await provider.send_sms(
        from_e164="+14155559999", to_e164="+14155551234", body=long_body
    )

    assert result.segment_count == 2


@responses.activate
async def test_send_sms_carrier_rejection(provider: TwilioSmsProvider) -> None:
    responses.add(
        responses.POST,
        f"{API_BASE}/Messages.json",
        json={
            "sid": "SM_rejected1234567890abcdef1234",
            "account_sid": ACCOUNT_SID,
            "to": "+14155551234",
            "from": "+14155559999",
            "body": "Hello from Hail",
            "status": "failed",
            "num_segments": "1",
            "error_code": 30006,
            "date_created": "Wed, 22 Apr 2026 12:00:00 +0000",
            "uri": f"/2010-04-01/Accounts/{ACCOUNT_SID}/Messages/SM_rejected1234567890abcdef1234.json",
        },
        status=201,
    )

    result = await provider.send_sms(
        from_e164="+14155559999", to_e164="+14155551234", body="Hello from Hail"
    )

    assert isinstance(result, ProviderSmsResult)
    assert result.error_code == "30006"
    assert result.status == "failed"


@responses.activate
async def test_send_sms_invalid_number_returns_failed_result(
    provider: TwilioSmsProvider,
) -> None:
    # Twilio raises TwilioRestException (HTTP 4xx + a 21xxx code) at create
    # time for an invalid/unreachable recipient — no message resource is
    # created. Per the SmsProvider contract this comes back as a failed
    # ProviderSmsResult (not an exception) so the route records an unbilled
    # failed send rather than a 502 transport error.
    responses.add(
        responses.POST,
        f"{API_BASE}/Messages.json",
        json={
            "code": 21211,
            "message": "The 'To' number +1000 is not a valid phone number.",
            "more_info": "https://www.twilio.com/docs/errors/21211",
            "status": 400,
        },
        status=400,
    )

    result = await provider.send_sms(
        from_e164="+14155559999", to_e164="+1000", body="Hello"
    )

    assert isinstance(result, ProviderSmsResult)
    assert result.status == "failed"
    assert result.error_code == "21211"
    assert result.provider_message_sid is None


@responses.activate
async def test_send_sms_auth_failure_raises(provider: TwilioSmsProvider) -> None:
    # Account-level failures (auth, rate-limit) and 5xx are transport failures:
    # they propagate so the route surfaces a 502, not a per-recipient "failed"
    # (an auth failure means every send fails — not the recipient's fault).
    from twilio.base.exceptions import TwilioRestException

    responses.add(
        responses.POST,
        f"{API_BASE}/Messages.json",
        json={"code": 20003, "message": "Authentication Error", "status": 401},
        status=401,
    )

    with pytest.raises(TwilioRestException):
        await provider.send_sms(
            from_e164="+14155559999", to_e164="+14155551234", body="Hello"
        )


def test_constructor_raises_without_creds() -> None:
    with pytest.raises(ValueError, match="requires twilio_account_sid"):
        TwilioSmsProvider(account_sid="", auth_token="")


def test_constructor_falls_back_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_account_sid", ACCOUNT_SID)
    monkeypatch.setattr(settings, "twilio_auth_token", AUTH_TOKEN)

    provider = TwilioSmsProvider()
    assert provider.account_sid == ACCOUNT_SID
