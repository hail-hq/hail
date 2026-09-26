"""Unit tests for ``TwilioVoiceProvider``.

We mock at the **HTTP boundary** (``responses``) rather than monkey-patching
Twilio SDK objects. That way the tests catch SDK API drift: if Twilio
renames a model field or moves an endpoint, these tests break the same
way real usage would.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs

import pytest
import responses
from hailhq.core.providers.voice import (
    ProviderCallStatus,
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
