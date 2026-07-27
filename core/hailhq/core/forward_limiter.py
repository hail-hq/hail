"""Per-domain forward rate cap.

Counts forwarded outbound rows in the last hour for the domain —
``Email.direction='outbound'`` rows whose ``metadata.forwarded_from``
points at an inbound row. The cap is the per-domain override on
``email_domains.forward_rate_per_hour``; if NULL, the per-deployment
default from ``settings.hail_forward_rate_per_hour`` applies.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from hailhq.core.models import Email
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["ForwardLimiter"]


class ForwardLimiter:
    def __init__(self, *, default_per_hour: int) -> None:
        self._default = default_per_hour

    async def can_forward(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        email_domain_id: UUID,
        override: int | None,
    ) -> bool:
        cap = override if override is not None else self._default
        if cap <= 0:
            return False
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        stmt = (
            select(func.count())
            .select_from(Email)
            .where(Email.organization_id == organization_id)
            .where(Email.email_domain_id == email_domain_id)
            .where(Email.direction == "outbound")
            .where(Email.created_at >= since)
            .where(Email.metadata_["forwarded_from"].astext.isnot(None))
        )
        used = (await db.execute(stmt)).scalar_one()
        return used < cap
