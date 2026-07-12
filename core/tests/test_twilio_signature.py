"""Tests for Twilio inbound webhook signature verification.

Twilio signs with HMAC-SHA1 over the full URL + sorted form-param
concatenation, base64-encoded — a different scheme from the repo's own
hailhq.core.hmac_signing (HMAC-SHA256 over a raw body), so this has its
own module rather than extending that one.
"""

from __future__ import annotations

from twilio.request_validator import RequestValidator

from hailhq.core.twilio_signature import verify_twilio_signature

AUTH_TOKEN = "test-auth-token"
URL = "https://api.hail.so/sms/inbound"


def _real_signature(params: dict[str, str]) -> str:
    return RequestValidator(AUTH_TOKEN).compute_signature(URL, params)


def test_verify_accepts_genuine_signature() -> None:
    params = {"From": "+14155551234", "To": "+14155559999", "Body": "hi"}
    sig = _real_signature(params)
    assert verify_twilio_signature(URL, params, sig, AUTH_TOKEN) is True


def test_verify_rejects_tampered_params() -> None:
    params = {"From": "+14155551234", "To": "+14155559999", "Body": "hi"}
    sig = _real_signature(params)
    tampered = {**params, "Body": "tampered"}
    assert verify_twilio_signature(URL, tampered, sig, AUTH_TOKEN) is False


def test_verify_rejects_missing_signature() -> None:
    params = {"From": "+14155551234", "To": "+14155559999", "Body": "hi"}
    assert verify_twilio_signature(URL, params, None, AUTH_TOKEN) is False


def test_verify_rejects_wrong_url() -> None:
    params = {"From": "+14155551234", "To": "+14155559999", "Body": "hi"}
    sig = _real_signature(params)
    assert (
        verify_twilio_signature(
            "https://api.hail.so/sms/wrong-path", params, sig, AUTH_TOKEN
        )
        is False
    )
