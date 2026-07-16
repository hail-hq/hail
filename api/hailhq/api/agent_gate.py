"""Agent velocity-cap gate for the create routes — the 429 sibling of
funds.require_funds (same shape: shared-key skip, idempotency caching)."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.deps import Principal
from hailhq.api.idempotency import IdempotencyContext, cache_failure
from hailhq.core.agent_caps import check_agent_send_allowed

__all__ = ["require_agent_send_allowed"]


async def require_agent_send_allowed(
    db: AsyncSession,
    principal: Principal,
    channel: str,
    recipients: list[str],
    idem: IdempotencyContext | None = None,
) -> None:
    """Raise 429 when an agent-origin org is over its velocity caps.

    ``recipients`` is the full recipient set for the send — email passes
    to+cc+bcc (deduped/normalized); sms and calls pass a one-element list
    for their single destination. Every recipient counts toward the caps.

    No-op for human-origin orgs and for the self-hosted shared-key path
    (``api_key_id is None`` — same posture as require_funds)."""
    if principal.api_key_id is None:
        return
    denial = await check_agent_send_allowed(
        db, principal.organization_id, channel, recipients
    )
    if denial is None:
        return
    raise await cache_failure(
        idem,
        HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail=denial.reason,
            headers={"Retry-After": str(denial.retry_after_seconds)},
        ),
    )
