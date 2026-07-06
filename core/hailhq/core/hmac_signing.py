"""Shared HMAC-SHA256-over-body signing/verification.

The `X-Hail-Signature: sha256=<hex>` scheme used across every internal
auth boundary in this repo: `internal_webhook.py` signs voicebot/api →
hail-website calls; `providers/email/inbound/ses.py` verifies
Lambda → API SES notifications; `api/hailhq/api/routes/internal/auth.py`
verifies hail-website → API calls. One implementation so these three
call sites can't silently diverge on header parsing or comparison
semantics — see the byte- vs. str-comparison note on ``verify`` below.
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["sign", "verify"]


def sign(body: bytes, secret: str) -> str:
    """Return the `sha256=<hex>` header value for ``body`` signed with ``secret``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(header: str | None, body: bytes, secret: str) -> bool:
    """Constant-time check that ``header`` is ``sign(body, secret)``.

    Compares as bytes, not str: ``hmac.compare_digest`` raises
    ``TypeError`` on non-ASCII ``str`` input, which would surface as an
    unhandled 500 from a caller expecting a clean ``False``.
    """
    if not header or not header.startswith("sha256="):
        return False
    provided = header.split("=", 1)[1]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided.encode(), expected.encode())
