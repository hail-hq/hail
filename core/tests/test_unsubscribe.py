"""Tests for signed unsubscribe tokens."""

from __future__ import annotations

import time
import uuid

import pytest
from hailhq.core.config import settings
from hailhq.core.unsubscribe import (
    InvalidUnsubscribeToken,
    build_unsubscribe_url,
    mint_unsubscribe_token,
    verify_unsubscribe_token,
)


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "hail_unsubscribe_secret", "test-secret")


def test_round_trip():
    org_id = uuid.uuid4()
    token = mint_unsubscribe_token("alice@example.com", org_id)
    email, decoded_org_id = verify_unsubscribe_token(token)
    assert email == "alice@example.com"
    assert decoded_org_id == org_id


def test_rejects_tampered_token():
    org_id = uuid.uuid4()
    token = mint_unsubscribe_token("alice@example.com", org_id)
    # Flip a character well before the end, not the last character. Base64's
    # final partial group can carry "don't-care" bits that Python's decoder
    # doesn't validate — flipping *there* occasionally round-trips to the
    # same bytes (since sig is fixed-length but org_id is random per test,
    # the last char's don't-care-bit class varies run to run), producing a
    # ~1-in-16 flaky false pass. A middle index always sits in a full
    # 3-byte/4-char group, so tampering it is guaranteed to change the
    # decoded bytes.
    idx = len(token) // 2
    tampered = token[:idx] + ("A" if token[idx] != "A" else "B") + token[idx + 1 :]
    with pytest.raises(InvalidUnsubscribeToken):
        verify_unsubscribe_token(tampered)


def test_rejects_expired_token():
    org_id = uuid.uuid4()
    token = mint_unsubscribe_token("alice@example.com", org_id, ttl_seconds=-1)
    with pytest.raises(InvalidUnsubscribeToken):
        verify_unsubscribe_token(token)


def test_rejects_malformed_token():
    with pytest.raises(InvalidUnsubscribeToken):
        verify_unsubscribe_token("not-a-valid-token")


def test_rejects_token_signed_with_different_secret(monkeypatch: pytest.MonkeyPatch):
    org_id = uuid.uuid4()
    token = mint_unsubscribe_token("alice@example.com", org_id)
    monkeypatch.setattr(settings, "hail_unsubscribe_secret", "a-different-secret")
    with pytest.raises(InvalidUnsubscribeToken):
        verify_unsubscribe_token(token)


def test_build_unsubscribe_url_joins_against_api_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "hail_api_url", "https://api.hail.so")
    org_id = uuid.uuid4()
    url = build_unsubscribe_url("alice@example.com", org_id)
    assert url.startswith("https://api.hail.so/unsubscribe?token=")

    token = url.split("token=", 1)[1]
    email, decoded_org_id = verify_unsubscribe_token(token)
    assert email == "alice@example.com"
    assert decoded_org_id == org_id


def test_email_with_pipe_char_round_trips():
    """Local-part '|' is unusual but schema-legal — rsplit must not swallow it."""
    org_id = uuid.uuid4()
    token = mint_unsubscribe_token("weird|local@example.com", org_id)
    email, decoded_org_id = verify_unsubscribe_token(token)
    assert email == "weird|local@example.com"
    assert decoded_org_id == org_id


def test_expiry_is_time_gated(monkeypatch: pytest.MonkeyPatch):
    org_id = uuid.uuid4()
    token = mint_unsubscribe_token("alice@example.com", org_id, ttl_seconds=100)
    # Still valid now.
    verify_unsubscribe_token(token)
    # But not once we jump past the expiry.
    future = time.time() + 200
    monkeypatch.setattr(time, "time", lambda: future)
    with pytest.raises(InvalidUnsubscribeToken):
        verify_unsubscribe_token(token)
