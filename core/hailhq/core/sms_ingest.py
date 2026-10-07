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

from hailhq.core.compliance_gate import (
    add_suppression,
    is_suppressed,
    remove_suppression,
)
from hailhq.core.config import settings
from hailhq.core.models import PhoneNumber, Sms, SmsEvent
from hailhq.core.providers.sms import ProviderSmsResult, SmsProvider
from hailhq.core.text_agent import should_queue_reply
from hailhq.core.threads import ACTIVE_CALL_SKIP, active_call_for_thread
from hailhq.core.webhook_fanout import fanout_sms_event
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

__all__ = ["IngestResult", "active_dedicated_number", "ingest_inbound_sms"]

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
_HELP_KEYWORDS = frozenset({"HELP", "INFO"})


@dataclass
class IngestResult:
    sms_id: UUID | None
    dropped_reason: str | None = None


def _opt_out_action(body: str, opt_out_type: str | None) -> str | None:
    """Return 'STOP', 'START', 'HELP', or None from the body keyword (with
    Twilio's OptOutType as corroboration)."""
    keyword = body.strip().upper()
    if opt_out_type == "STOP" or keyword in _STOP_KEYWORDS:
        return "STOP"
    if opt_out_type == "START" or keyword in _START_KEYWORDS:
        return "START"
    if opt_out_type == "HELP" or keyword in _HELP_KEYWORDS:
        return "HELP"
    return None


async def _send_compliance_reply(
    db: AsyncSession,
    provider: SmsProvider,
    *,
    org_number: PhoneNumber,
    sender_e164: str,
    body: str,
) -> None:
    """Send a single carrier-mandated compliance reply and persist it as an
    outbound Sms row for audit. Bypasses check_sms_allowed / usage / funds.
    Failures are logged and swallowed so the webhook still returns 200."""
    try:
        result: ProviderSmsResult = await provider.send_sms(
            from_e164=org_number.e164, to_e164=sender_e164, body=body
        )
    except Exception:
        logger.exception("compliance reply send failed to %s", sender_e164)
        return
    # Normalize the provider's raw status into the sms_status_check set
    # (queued|sent|delivered|failed|undelivered|received) exactly as the
    # outbound send path does — Twilio returns non-terminal statuses like
    # "accepted"/"sending" that would otherwise violate the CHECK and abort
    # the whole webhook transaction (rolling back the inbound row + STOP
    # suppression) on the flush below.
    carrier_rejected = result.error_code is not None or result.status.lower() in {
        "failed",
        "undelivered",
    }
    reply_status = "failed" if carrier_rejected else "sent"
    db.add(
        Sms(
            organization_id=org_number.organization_id,
            from_number_id=org_number.id,
            to_number_id=None,
            from_e164=org_number.e164,
            to_e164=sender_e164,
            direction="outbound",
            status=reply_status,
            body=body,
            provider=org_number.provider,
            provider_message_sid=result.provider_message_sid,
            segment_count=result.segment_count,
            error_code=result.error_code,
        )
    )
    await db.flush()


async def active_dedicated_number(db: AsyncSession, to_e164: str) -> PhoneNumber | None:
    """The active, org-owned (non-pool) number ``to_e164``, or None. Shared by
    inbound texts and inbound calls (``hailhq.core.inbound_calls``)."""
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
    carrier: str,
    provider: SmsProvider | None = None,
) -> IngestResult:
    number = await active_dedicated_number(db, to_e164)
    if number is None or number.organization_id is None:
        logger.info("inbound sms to unrecognized/pool number=%s dropped", to_e164)
        return IngestResult(sms_id=None, dropped_reason="unknown_number")

    if number.provider != carrier or "sms" not in number.capabilities:
        logger.info(
            "inbound sms to number=%s dropped: carrier=%s number carrier=%s "
            "capabilities=%s",
            to_e164,
            carrier,
            number.provider,
            number.capabilities,
        )
        return IngestResult(sms_id=None, dropped_reason="wrong_carrier_or_capability")

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
    # Inbound: the external sender has no PhoneNumber row, so from_number_id is
    # NULL; the org's receiving number (Twilio `To`) is recorded in to_number_id.
    # Outbound sends do the mirror (from_number_id set, to_number_id NULL).
    sms = Sms(
        organization_id=organization_id,
        from_number_id=None,
        to_number_id=number.id,
        from_e164=from_e164,
        to_e164=to_e164,
        direction="inbound",
        status="received",
        body=body,
        provider=carrier,
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
    # YES/START from someone who never opted out is an answer to the agent,
    # not an opt-in: leave it to the text agent. Hail only handles it as an
    # opt-in when the sender is currently opted out.
    if action == "START" and not await is_suppressed(
        db, organization_id, from_e164, "sms"
    ):
        action = None
    reply_body: str | None = None
    if action == "STOP":
        await add_suppression(
            db,
            organization_id=organization_id,
            recipient=from_e164,
            channel="sms",
            reason="recipient replied STOP",
            source="stop_keyword",
        )
        reply_body = settings.hail_sms_stop_reply
    elif action == "START":
        await remove_suppression(
            db, organization_id=organization_id, recipient=from_e164, channel="sms"
        )
        reply_body = settings.hail_sms_start_reply
    elif action == "HELP":
        reply_body = settings.hail_sms_help_reply

    if (
        reply_body is not None
        and provider is not None
        and settings.hail_sms_compliance_replies_enabled
    ):
        await _send_compliance_reply(
            db, provider, org_number=number, sender_e164=from_e164, body=reply_body
        )

    # Hand a plain message to the number's text agent. STOP, HELP and an
    # opt-in from an opted-out sender are Hail's to answer, never the agent's.
    # The row belongs to the number's text agent (live or not): that is what
    # puts it in the agent's thread.
    sms.agent_id = number.sms_agent_id
    if action is None and await should_queue_reply(db, number):
        if await active_call_for_thread(
            db, organization_id, number.sms_agent_id, from_e164
        ):
            # The voice agent on the line gets this text (voicebot text watch).
            # The marker tells the watcher and the call-end requeue that this
            # skip is theirs; other skips (expiry, routing) are never revived.
            sms.agent_reply_state = "skipped"
            sms.metadata_ = {
                **(sms.metadata_ or {}),
                "skipped_reason": ACTIVE_CALL_SKIP,
            }
        else:
            sms.agent_reply_state = "pending"

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
