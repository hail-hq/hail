"""Signed one-click unsubscribe tokens for outbound email.

Token wire format: ``base64url(email|organization_id|expiry_unix|sig)``
where ``sig`` is an HMAC-SHA256 over ``email|organization_id|expiry_unix``,
keyed on ``settings.hail_unsubscribe_secret`` — a dedicated secret,
deliberately not ``hail_internal_secret`` (that one signs internal
API<->website calls, a different concern).

``GET /unsubscribe?token=...`` (see
``api/hailhq/api/routes/unsubscribe.py``) verifies the token and calls
``hailhq.core.compliance_gate.add_suppression``.
"""

from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.urls import join_url

__all__ = [
    "InvalidUnsubscribeToken",
    "mint_unsubscribe_token",
    "verify_unsubscribe_token",
    "build_unsubscribe_url",
]

# 30 days — long enough that a message read weeks later still unsubscribes.
_DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60


class InvalidUnsubscribeToken(Exception):
    """Raised by :func:`verify_unsubscribe_token` for a bad/expired/tampered token."""


def _sign(payload: str) -> str:
    mac = hmac.new(
        settings.hail_unsubscribe_secret.encode("utf-8"),
        payload.encode("utf-8"),
        sha256,
    ).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def mint_unsubscribe_token(
    email: str, organization_id: UUID, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS
) -> str:
    expiry = int(time.time()) + ttl_seconds
    payload = f"{email}|{organization_id}|{expiry}"
    sig = _sign(payload)
    raw = f"{payload}|{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def verify_unsubscribe_token(token: str) -> tuple[str, UUID]:
    """Return ``(email, organization_id)``, or raise ``InvalidUnsubscribeToken``."""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        email, org_str, expiry_str, sig = raw.rsplit("|", 3)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidUnsubscribeToken("malformed token") from exc

    expected_sig = _sign(f"{email}|{org_str}|{expiry_str}")
    if not hmac.compare_digest(sig, expected_sig):
        raise InvalidUnsubscribeToken("signature mismatch")

    try:
        expiry = int(expiry_str)
        organization_id = UUID(org_str)
    except ValueError as exc:
        raise InvalidUnsubscribeToken("malformed token") from exc

    if time.time() > expiry:
        raise InvalidUnsubscribeToken("token expired")

    return email, organization_id


def build_unsubscribe_url(email: str, organization_id: UUID) -> str:
    """Full ``GET /unsubscribe?token=...`` URL against ``settings.hail_api_url``
    (the API's own public URL — this link is served by the API, not the
    website)."""
    token = mint_unsubscribe_token(email, organization_id)
    return join_url(settings.hail_api_url, f"unsubscribe?token={token}")
