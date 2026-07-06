"""Tests for the shared HMAC-SHA256-over-body signing/verification scheme
(`X-Hail-Signature: sha256=<hex>`), used by internal_webhook.py (signing),
ses.py, and api/hailhq/api/routes/internal/auth.py (verifying)."""

from __future__ import annotations

from hailhq.core.hmac_signing import sign, verify


def test_sign_produces_sha256_prefixed_hex():
    header = sign(b'{"a":1}', "s3cret")
    assert header.startswith("sha256=")
    assert len(header) == len("sha256=") + 64  # sha256 hex digest is 64 chars


def test_verify_accepts_a_correctly_signed_body():
    body = b'{"a":1}'
    header = sign(body, "s3cret")
    assert verify(header, body, "s3cret") is True


def test_verify_rejects_wrong_secret():
    body = b'{"a":1}'
    header = sign(body, "s3cret")
    assert verify(header, body, "wrong-secret") is False


def test_verify_rejects_tampered_body():
    header = sign(b'{"a":1}', "s3cret")
    assert verify(header, b'{"a":2}', "s3cret") is False


def test_verify_rejects_missing_header():
    assert verify(None, b'{"a":1}', "s3cret") is False
    assert verify("", b'{"a":1}', "s3cret") is False


def test_verify_rejects_wrong_prefix():
    assert verify("md5=deadbeef", b'{"a":1}', "s3cret") is False


def test_verify_rejects_non_ascii_signature_without_raising():
    """Regression: hmac.compare_digest on two `str` raises TypeError for
    non-ASCII input. verify() must compare bytes internally, not str, so
    a malformed header degrades to a clean False, not an unhandled
    exception (which would surface as a 500 from a FastAPI dependency)."""
    assert verify("sha256=héllo", b'{"a":1}', "s3cret") is False
