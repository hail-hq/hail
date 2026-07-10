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
from hailhq.core.models import PhoneNumber, Sms, SmsEvent
from hailhq.core.webhook_fanout import fanout_sms_event

logger = logging.getLogger(__name__)

__all__ = ["IngestResult", "ingest_inbound_sms"]

# Unique-constraint name whose violation is a benign duplicate delivery
# (Twilio at-least-once retry) to absorb; every other IntegrityError
# (CHECK/FK/NOT NULL) must propagate. Mirrors email_ingest's
# _BENIGN_DEDUP_INDEXES pattern.
_SMS_SID_UNIQUE = "sms_provider_message_sid_key"

# Carrier opt-out keywords (CTIA/Twilio). We match the message body
# ourselves rather than relying solely on Twilio's ``OptOutType`` param,
# which is only populated when the number sits behind a Messaging Service
# with Advanced Opt-Out — a config Hail does not require. ``OptOutType``
# is honored as corroboration when present.
_STOP_KEYWORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"})
_START_KEYWORDS = frozenset({"START", "YES", "UNSTOP"})


@dataclass
class IngestResult:
    sms_id: UUID | None
    dropped_reason: str | None = None


def _opt_out_action(body: str, opt_out_type: str | None) -> str | None:
    """Return 'STOP', 'START', or None from the body keyword (with Twilio's
    OptOutType as corroboration)."""
    keyword = body.strip().upper()
    if opt_out_type == "STOP" or keyword in _STOP_KEYWORDS:
        return "STOP"
    if opt_out_type == "START" or keyword in _START_KEYWORDS:
        return "START"
    return None


async def _resolve_org_for_number(db: AsyncSession, to_e164: str) -> PhoneNumber | None:
    stmt = select(PhoneNumber).where(
        PhoneNumber.e164 == to_e164,
        PhoneNumber.is_pool.is_(False),
        PhoneNumber.provisioning_state == "active",
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def ingest_inbound_sms(
    db: AsyncSession,
    *,
    from_e164: str,
    to_e164: str,
    body: str,
    provider_message_sid: str | None,
    opt_out_type: str | None,
) -> IngestResult:
    number = await _resolve_org_for_number(db, to_e164)
    if number is None or number.organization_id is None:
        logger.info("inbound sms to unrecognized/pool number=%s dropped", to_e164)
        return IngestResult(sms_id=None, dropped_reason="unknown_number")

    organization_id = number.organization_id

    # A missing/blank MessageSid must be stored as NULL, not "": the column
    # is UNIQUE-but-nullable, so NULLs coexist while two blank strings would
    # collide and the second genuine message would be swallowed as a "dup".
    sid = provider_message_sid or None

    # Idempotent insert: a duplicate webhook delivery (Twilio retry) must
    # not create a second row. provider_message_sid is unique in the Sms
    # table, so a duplicate insert raises IntegrityError — catch it and
    # return the existing row's id, mirroring email_ingest.py's
    # SAVEPOINT dedup pattern. The try/except sits *outside* the
    # ``async with`` block so the context manager's own exit handling
    # rolls back to the savepoint on error; calling db.rollback() from
    # inside the block would close the transaction the context manager
    # still expects to manage, breaking the next nested-transaction call.
    # NOTE: for inbound rows `from_number_id` holds the org's *receiving*
    # number (the Twilio `To`), while `from_e164` is the external sender —
    # so the outbound invariant `from_number_id.e164 == from_e164` does NOT
    # hold here. The Sms table has no `to_number_id` column yet; until one
    # exists this FK records "which of our numbers this conversation touched".
    # Per-number analytics keyed on `from_number_id` must special-case
    # direction. See docs/operations.md "Carry-forwards" for the fix.
    sms = Sms(
        organization_id=organization_id,
        from_number_id=number.id,
        from_e164=from_e164,
        to_e164=to_e164,
        direction="inbound",
        status="received",
        body=body,
        provider_message_sid=sid,
    )
    try:
        async with db.begin_nested():
            db.add(sms)
            await db.flush()
    except IntegrityError as exc:
        # Only the provider_message_sid unique index is a benign duplicate
        # delivery; CHECK/FK/NOT NULL violations must surface, not silently
        # drop the message.
        if sid is None or _SMS_SID_UNIQUE not in str(exc.orig):
            raise
        existing = (
            await db.execute(select(Sms).where(Sms.provider_message_sid == sid))
        ).scalar_one_or_none()
        return IngestResult(sms_id=existing.id if existing else None)

    action = _opt_out_action(body, opt_out_type)
    if action == "STOP":
        await add_suppression(
            db,
            organization_id=organization_id,
            recipient=from_e164,
            channel="sms",
            reason="recipient replied STOP",
            source="stop_keyword",
        )
    elif action == "START":
        await remove_suppression(
            db, organization_id=organization_id, recipient=from_e164, channel="sms"
        )

    # Record a lifecycle event so the inbound message surfaces on the
    # org-wide GET /events stream (which is built solely from SmsEvent rows),
    # matching how outbound sends and inbound email appear there.
    db.add(
        SmsEvent(
            sms_id=sms.id,
            organization_id=organization_id,
            kind="received",
            payload={"from": from_e164, "to": to_e164, "body": body},
        )
    )
    await db.flush()

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
