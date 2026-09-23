"""Shared 402 balance gate for the create routes (/calls, /emails, /sms).

One home for the gate and its billing-console URL — previously three
verbatim copies, one per route.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi import status as http_status
from hailhq.api.deps import Principal
from hailhq.api.idempotency import IdempotencyContext, cache_failure
from hailhq.core.billing import has_funds
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["BILLING_URL", "FUNDS_RESPONSES", "require_funds"]

BILLING_URL = "https://hail.so/console/billing"
_NO_FUNDS_DETAIL = f"insufficient credits; top up at {BILLING_URL}"

# OpenAPI doc for the 402 `require_funds` can raise. FastAPI does not infer
# statuses from a plain `raise HTTPException` any more than it does from a
# middleware short-circuit, so every route decorator that calls
# `require_funds` must declare this (`responses=FUNDS_RESPONSES`, merged with
# any route-specific responses) for the generated spec — and the CLI codegen
# from it — to reflect the 402. Regenerate openapi/openapi.yaml after
# touching this (see docs/public/contributing.md).
FUNDS_RESPONSES: dict[int | str, dict[str, Any]] = {
    402: {"description": _NO_FUNDS_DETAIL},
}


async def require_funds(
    db: AsyncSession, principal: Principal, idem: IdempotencyContext | None = None
) -> None:
    """Raise 402 when the org has no credits. Cloud-only: only shared-key auth
    (``auth_kind == "shared"`` ⇒ HAIL_API_KEY path) lands on the unbilled
    "Self-hosted" org and skips the gate. Both real API keys and console/website
    session JWTs are billed principals and get the balance check — note
    ``api_key_id`` is None on the JWT path too, so it must NOT be used here.
    The 402 is cached under the idempotency key when one was supplied."""
    if principal.auth_kind == "shared":
        return
    if not await has_funds(db, principal.organization_id):
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                detail=_NO_FUNDS_DETAIL,
            ),
        )
