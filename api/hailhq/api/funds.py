"""Shared 402 balance gate for the create routes (/calls, /emails, /sms).

One home for the gate and its billing-console URL — previously three
verbatim copies, one per route.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.deps import Principal
from hailhq.api.idempotency import IdempotencyContext, cache_failure
from hailhq.core.billing import has_funds

__all__ = ["require_funds"]

_NO_FUNDS_DETAIL = "insufficient credits; top up at https://hail.so/console/billing"


async def require_funds(
    db: AsyncSession, principal: Principal, idem: IdempotencyContext | None = None
) -> None:
    """Raise 402 when the org has no credits. Cloud-only: shared-key auth
    (``api_key_id is None`` ⇒ HAIL_API_KEY path) lands on the unbilled
    "Self-hosted" org and skips the gate. The 402 is cached under the
    idempotency key when one was supplied."""
    if principal.api_key_id is None:
        return
    if not await has_funds(db, principal.organization_id):
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                detail=_NO_FUNDS_DETAIL,
            ),
        )
