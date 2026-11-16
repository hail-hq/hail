"""The daily cap on AI document reads (``POST /verifications/document-check``).

Each file the check sends to the model is one read. Hail pays the model
provider for every read, and reads are free to customers for now, so each
workspace gets at most ``DAILY_DOCUMENT_READS`` per UTC day. Past the cap the
check skips the model: the slot comes back ``read: false`` with no error and
no warning, and the customer fills the form by hand. A file that is not sent
(skipped, unsupported, over the limits) does not count.

Cost: at most 30 reads x about 1 US cent = about 30 cents per workspace per
day. That is a ceiling: a read sent 1.3k to 2.3k input tokens and got under
200 back in tests (2026-11), well under 1 cent at Haiku 5.5 list prices.

Open decision: charge per read or keep free. See
docs/operations/carrier-verification.md#document-check-ai-read.

The count lives in ``agent_send_log`` (the per-workspace send counter behind
the agent caps), one row per read with ``channel = 'document_read'`` and the
slot name as ``recipient``: no file, name or other personal data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from hailhq.core.db import org_lock
from hailhq.core.models import AgentSendLog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "DAILY_DOCUMENT_READS",
    "DOCUMENT_READ_CHANNEL",
    "document_reads_today",
    "take_document_read",
]

# Reads per workspace per UTC day. A placeholder until the pricing decision
# above; not in .env on purpose (one number for every deploy).
DAILY_DOCUMENT_READS = 30
DOCUMENT_READ_CHANNEL = "document_read"


def _day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


async def document_reads_today(
    db: AsyncSession, organization_id: UUID, now: datetime | None = None
) -> int:
    """The workspace's reads since 00:00 UTC."""
    start = _day_start(now or datetime.now(timezone.utc))
    return (
        await db.execute(
            select(func.count()).where(
                AgentSendLog.organization_id == organization_id,
                AgentSendLog.channel == DOCUMENT_READ_CHANNEL,
                AgentSendLog.created_at >= start,
            )
        )
    ).scalar_one()


async def take_document_read(
    db: AsyncSession, organization_id: UUID, slot: str
) -> bool:
    """Count one read for the workspace and return True, or return False when
    today's cap is used up. Takes the workspace lock for the count and the
    write, so two checks at once cannot pass the cap, and commits."""
    try:
        await org_lock(db, organization_id)
        if await document_reads_today(db, organization_id) >= DAILY_DOCUMENT_READS:
            await db.rollback()
            return False
        db.add(
            AgentSendLog(
                organization_id=organization_id,
                channel=DOCUMENT_READ_CHANNEL,
                recipient=slot,
            )
        )
        await db.commit()
        return True
    except Exception:
        await db.rollback()
        raise
