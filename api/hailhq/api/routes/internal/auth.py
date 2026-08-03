"""Shared-secret auth for hail-website → hail internal endpoints.

Reuses ``HAIL_INTERNAL_SECRET`` — already the shared HMAC secret for
internal API↔website calls in the other direction (see
``hailhq.core.internal_webhook``, which signs voicebot/api → website
calls with it) — rather than minting a second secret for this direction.
Same scheme as everywhere else in this repo (``hailhq.core.hmac_signing``):
HMAC-SHA256 over the raw request body, sent as
``X-Hail-Signature: sha256=<hex>``.

Used by ``routes/internal/org_closures.py`` and ``routes/internal/dsar.py``.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi import status as http_status
from hailhq.core.config import settings
from hailhq.core.hmac_signing import verify

__all__ = ["verify_internal_request"]


async def verify_internal_request(request: Request) -> None:
    """FastAPI dependency: 503 if unconfigured, 401 on a bad/missing
    signature. Reads the raw body via ``request.body()``, which Starlette
    caches — the route's own Pydantic body model re-reads the same bytes."""
    secret = settings.hail_internal_secret
    if not secret:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="internal endpoint disabled: HAIL_INTERNAL_SECRET is unset",
        )

    body = await request.body()
    if not verify(request.headers.get("x-hail-signature"), body, secret):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="invalid signature",
        )
