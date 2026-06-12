"""Webhook signing, retry schedule, and payload assembly.

Signing is Stripe-style: ``X-Hail-Signature: t=<unix_ts>,v1=<hex_hmac_sha256>``
signed over ``f"{t}.{body}"``. The retry schedule is fixed (0, 30s, 2m,
10m, 1h, 6h, 24h) for the inbound-email milestone; per-subscription
overrides land in a follow-up.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

__all__ = [
    "API_VERSION",
    "RETRY_SCHEDULE_SECONDS",
    "build_event_payload",
    "next_attempt_delay",
    "sign_payload",
]

API_VERSION = "2026-06-06"

# 0s, 30s, 2m, 10m, 1h, 6h, 24h. After the last attempt, the delivery is
# marked 'dead' and the subscription's consecutive_failures bumps.
RETRY_SCHEDULE_SECONDS: list[int] = [0, 30, 120, 600, 3600, 21600, 86400]


def sign_payload(body: bytes, secret: str, *, timestamp: int | None = None) -> str:
    """Return the value for the ``X-Hail-Signature`` header."""
    ts = timestamp if timestamp is not None else int(time.time())
    message = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def build_event_payload(
    *,
    delivery_id: str | UUID,
    event_type: str,
    organization_id: str | UUID,
    data: dict[str, Any],
    api_version: str = API_VERSION,
    created_at: datetime | None = None,
) -> bytes:
    payload = {
        "id": str(delivery_id),
        "type": event_type,
        "api_version": api_version,
        "created_at": (created_at or datetime.now(timezone.utc)).isoformat(),
        "organization_id": str(organization_id),
        "data": data,
    }
    return json.dumps(payload, separators=(",", ":")).encode()


def next_attempt_delay(attempt_index: int) -> int | None:
    """Seconds to wait before attempt N (0-indexed). ``None`` ⇒ dead."""
    if attempt_index >= len(RETRY_SCHEDULE_SECONDS):
        return None
    return RETRY_SCHEDULE_SECONDS[attempt_index]
