import asyncio
import hashlib
import hmac
import json

import pytest

from hailhq.core.providers.email.inbound.ses import SesInboundProvider

SAMPLE = {
    "message_id": "abc123",
    "envelope_from": "alice@example.com",
    "recipients": ["bob+acme@mail.hail.so"],
    "verdicts": {
        "spam": "PASS",
        "virus": "PASS",
        "spf": "PASS",
        "dkim": "PASS",
        "dmarc": "PASS",
    },
    "s3_bucket": "hail-inbound",
    "s3_key": "raw/abc123",
    "timestamp": "2026-06-06T10:11:12Z",
}


def _signed(body: bytes, secret: str) -> dict[str, str]:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hail-Signature": f"sha256={sig}"}


def test_verify_notification_accepts_valid_signature():
    p = SesInboundProvider(hmac_secret="s3cret")
    body = json.dumps(SAMPLE).encode()
    headers = _signed(body, "s3cret")
    assert asyncio.run(p.verify_notification(headers, body)) is True


def test_verify_notification_accepts_lowercase_header():
    # urllib in Lambda lowercases header keys; mirror that.
    p = SesInboundProvider(hmac_secret="s3cret")
    body = json.dumps(SAMPLE).encode()
    headers = {"x-hail-signature": _signed(body, "s3cret")["X-Hail-Signature"]}
    assert asyncio.run(p.verify_notification(headers, body)) is True


def test_verify_notification_rejects_bad_signature():
    p = SesInboundProvider(hmac_secret="s3cret")
    body = json.dumps(SAMPLE).encode()
    headers = {"X-Hail-Signature": "sha256=deadbeef"}
    assert asyncio.run(p.verify_notification(headers, body)) is False


def test_verify_notification_rejects_missing_header():
    p = SesInboundProvider(hmac_secret="s3cret")
    body = json.dumps(SAMPLE).encode()
    assert asyncio.run(p.verify_notification({}, body)) is False


def test_verify_notification_rejects_wrong_prefix():
    p = SesInboundProvider(hmac_secret="s3cret")
    body = json.dumps(SAMPLE).encode()
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    headers = {"X-Hail-Signature": f"md5={sig}"}
    assert asyncio.run(p.verify_notification(headers, body)) is False


@pytest.mark.asyncio
async def test_non_ascii_signature_is_rejected_not_500():
    provider = SesInboundProvider(hmac_secret="s")
    ok = await provider.verify_notification({"X-Hail-Signature": "sha256=héllo"}, b"{}")
    assert ok is False


def test_constructor_rejects_empty_secret():
    with pytest.raises(ValueError):
        SesInboundProvider(hmac_secret="")


def test_parse_notification_round_trip():
    p = SesInboundProvider(hmac_secret="s3cret")
    msg = asyncio.run(p.parse_notification(json.dumps(SAMPLE).encode()))
    assert msg.provider_message_id == "abc123"
    assert msg.envelope_recipients == ["bob+acme@mail.hail.so"]
    assert msg.spam_verdict == "PASS"
    assert msg.virus_verdict == "PASS"
    assert msg.raw_s3_bucket == "hail-inbound"
    assert msg.raw_s3_key == "raw/abc123"
    assert msg.received_at is not None


def test_parse_notification_handles_missing_verdicts():
    payload = dict(SAMPLE)
    del payload["verdicts"]
    p = SesInboundProvider(hmac_secret="s3cret")
    msg = asyncio.run(p.parse_notification(json.dumps(payload).encode()))
    assert msg.spam_verdict is None


def test_parse_notification_handles_missing_timestamp():
    payload = dict(SAMPLE)
    del payload["timestamp"]
    p = SesInboundProvider(hmac_secret="s3cret")
    msg = asyncio.run(p.parse_notification(json.dumps(payload).encode()))
    assert msg.received_at is None
