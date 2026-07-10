# core/hailhq/core/sms_ingest.py
"""Inbound SMS ingest: resolve the owning org by the Twilio `To` number,
idempotently persist the message, detect and apply STOP/START opt-out
signals, and fan out to the org's webhook subscribers.

Mirrors core/hailhq/core/email_ingest.py's shape (verify -> resolve org ->
persist -> fan out) but simpler: there's no forwarding, no attachment
parsing, and org resolution is a single PhoneNumber lookup rather than a
domain-suffix match.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.compliance_gate import add_suppression, remove_suppression
from hailhq.core.models import PhoneNumber, Sms
from hailhq.core.webhook_fanout import fanout_sms_event

logger = logging.getLogger(__name__)

__all__ = ["IngestResult", "ingest_inbound_sms"]


@dataclass
class IngestResult:
    sms_id: UUID | None
    dropped_reason: str | None = None


async def _resolve_org_for_number(db: AsyncSession, to_e164: str) -> PhoneNumber | None:
    stmt = select(PhoneNumber).where(
        PhoneNumber.e164 == to_e164,
        PhoneNumber.is_pool.is_(False),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def ingest_inbound_sms(
    db: AsyncSession,
    *,
    from_e164: str,
    to_e164: str,
    body: str,
    provider_message_sid: str,
    opt_out_type: str | None,
) -> IngestResult:
    number = await _resolve_org_for_number(db, to_e164)
    if number is None or number.organization_id is None:
        logger.info("inbound sms to unrecognized/pool number=%s dropped", to_e164)
        return IngestResult(sms_id=None, dropped_reason="unknown_number")

    organization_id = number.organization_id

    # Idempotent insert: a duplicate webhook delivery (Twilio retry) must
    # not create a second row. provider_message_sid is unique in the Sms
    # table, so a duplicate insert raises IntegrityError — catch it and
    # return the existing row's id, mirroring email_ingest.py's
    # SAVEPOINT dedup pattern. The try/except sits *outside* the
    # ``async with`` block so the context manager's own exit handling
    # rolls back to the savepoint on error; calling db.rollback() from
    # inside the block would close the transaction the context manager
    # still expects to manage, breaking the next nested-transaction call.
    sms = Sms(
        organization_id=organization_id,
        from_number_id=number.id,
        from_e164=from_e164,
        to_e164=to_e164,
        direction="inbound",
        status="received",
        body=body,
        provider_message_sid=provider_message_sid,
    )
    try:
        async with db.begin_nested():
            db.add(sms)
            await db.flush()
    except IntegrityError:
        existing = (
            await db.execute(
                select(Sms).where(Sms.provider_message_sid == provider_message_sid)
            )
        ).scalar_one_or_none()
        return IngestResult(sms_id=existing.id if existing else None)

    if opt_out_type == "STOP":
        await add_suppression(
            db,
            organization_id=organization_id,
            recipient=from_e164,
            channel="sms",
            reason="recipient replied STOP",
            source="stop_keyword",
        )
    elif opt_out_type == "START":
        await remove_suppression(
            db, organization_id=organization_id, recipient=from_e164, channel="sms"
        )

    await fanout_sms_event(
        db,
        organization_id=organization_id,
        event_type="sms.received",
        event_id=sms.id,
        data={
            "id": str(sms.id),
            "from": from_e164,
            "to": to_e164,
            "body": body,
        },
    )

    return IngestResult(sms_id=sms.id)
