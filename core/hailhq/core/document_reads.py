"""The daily cap on AI document reads (``POST /verifications/document-check``).

Each file sent to the model is one read, whatever happens to the call after
(an error, a timeout, a cancellation): Hail may already be paying for it.
The reads of a request are reserved before any file is fetched; a reserved
read whose file was never sent (not readable, over the limits, not decoded)
is given back. Hail pays the model
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

from hailhq.core.db import key_lock
from hailhq.core.models import AgentSendLog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "DAILY_DOCUMENT_READS",
    "DOCUMENT_READ_CHANNEL",
    "document_reads_today",
    "release_document_reads",
    "reserve_document_reads",
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


async def reserve_document_reads(
    db: AsyncSession, organization_id: UUID, slots: list[str]
) -> dict[str, UUID]:
    """Reserve one read per slot, in order, as far as today's cap allows,
    in one short transaction: a lock of its own (not the workspace lock,
    which is held through carrier calls), the count, then the rows, then
    commit. Returns the reserved slots with their row ids; a slot left out
    is over the cap. Runs before any file is fetched or decoded, so a
    workspace over its cap costs nothing. Give it its own session: it
    commits."""
    if not slots:
        return {}
    try:
        await key_lock(db, f"document_read:{organization_id}")
        left = DAILY_DOCUMENT_READS - await document_reads_today(db, organization_id)
        rows = [
            AgentSendLog(
                organization_id=organization_id,
                channel=DOCUMENT_READ_CHANNEL,
                recipient=slot,
            )
            for slot in slots[: max(left, 0)]
        ]
        db.add_all(rows)
        await db.flush()
        reserved = {row.recipient: row.id for row in rows}
        await db.commit()
        return reserved
    except BaseException:
        # Also on cancellation.
        await db.rollback()
        raise


async def release_document_reads(db: AsyncSession, row_ids: list[UUID]) -> None:
    """Give back reserved reads whose file never went to the model (a file
    skipped, unsupported, over the limits or out of time): only a sent file
    counts. Give it its own session: it commits."""
    if not row_ids:
        return
    try:
        await db.execute(delete(AgentSendLog).where(AgentSendLog.id.in_(row_ids)))
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
