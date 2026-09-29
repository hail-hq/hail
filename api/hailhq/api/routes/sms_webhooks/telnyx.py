"""Telnyx SMS webhook: one signed endpoint for inbound messages and
delivery receipts. The URL is set on the Telnyx messaging profile and in
``CARRIERS`` (``sms_status_path``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.api.routes.sms import TERMINAL_SMS_STATUSES, apply_sms_status
from hailhq.core.carrier_routing import TELNYX
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.models import PhoneNumber, Sms
from hailhq.core.providers.sms import SmsProvider
from hailhq.core.providers.sms.status_map import map_telnyx_message_status
from hailhq.core.providers.sms.telnyx import TelnyxSmsProvider
from hailhq.core.providers.telnyx import verify_webhook
from hailhq.core.sms_ingest import ingest_inbound_sms
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/sms", tags=["sms"], responses=GENERAL_RATE_LIMITED_RESPONSES
)


def _finalized_within(occurred_at: object, window: timedelta) -> bool:
    """True when the event time is unknown or newer than ``window``."""
    try:
        when = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when < window


@router.post("/telnyx", include_in_schema=False)
async def receive_telnyx_sms(
    request: Request, db: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, str]:
    """One signed Telnyx endpoint for inbound messages and delivery receipts."""

    raw = await request.body()
    if not verify_webhook(
        raw,
        request.headers.get("telnyx-signature-ed25519"),
        request.headers.get("telnyx-timestamp"),
        settings.telnyx_public_key,
    ):
        raise HTTPException(status_code=403, detail="invalid signature")
    try:
        event = (await request.json())["data"]
        payload = event["payload"]
        message_id = payload["id"]
        event_type = event["event_type"]
        recipients = payload["to"]
        if not message_id or len(recipients) != 1:
            raise ValueError("Expected one SMS recipient")
        recipient = recipients[0]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid messaging event") from None
    if event_type == "message.received":
        try:
            sender_e164 = payload["from"]["phone_number"]
            recipient_e164 = recipient["phone_number"]
        except (KeyError, TypeError):
            raise HTTPException(
                status_code=400, detail="invalid messaging event"
            ) from None
        try:
            reply_provider: SmsProvider | None = TelnyxSmsProvider()
        except ValueError:
            # No Telnyx API key. The provider only sends compliance replies, so
            # still store the message and apply a STOP instead of failing the webhook.
            reply_provider = None
        await ingest_inbound_sms(
            db,
            from_e164=sender_e164,
            to_e164=recipient_e164,
            body=payload.get("text") or "",
            provider_message_sid=message_id,
            opt_out_type=None,
            provider=reply_provider,
            carrier=TELNYX,
        )
        await db.commit()
        return {"status": "received"}
    if event_type != "message.finalized":
        return {"status": "ignored"}
    new_status = map_telnyx_message_status(recipient.get("status"))
    if not new_status:
        return {"status": "ignored"}
    sms = (
        await db.execute(
            select(Sms)
            .where(
                Sms.provider == TELNYX,
                Sms.provider_message_sid == message_id,
                Sms.direction == "outbound",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if sms is None:
        # The profile-level webhook reports every message on the profile,
        # including ones Hail never recorded (sent from the Telnyx portal).
        # Only a message from one of our numbers, reported moments ago, can be
        # racing POST /messages' DB commit. Ask for a retry only then.
        sender = (payload.get("from") or {}).get("phone_number")
        ours = (
            sender is not None
            and (
                await db.execute(
                    select(PhoneNumber.id)
                    .where(
                        PhoneNumber.provider == TELNYX,
                        PhoneNumber.e164 == sender,
                        PhoneNumber.provisioning_state == "active",
                    )
                    .limit(1)
                )
            ).first()
            is not None
        )
        if ours and _finalized_within(event.get("occurred_at"), timedelta(minutes=5)):
            raise HTTPException(status_code=503, detail="message not yet recorded")
        return {"status": "unrecorded"}
    if sms.status in TERMINAL_SMS_STATUSES:
        return {"status": "duplicate"}
    errors = payload.get("errors") or []
    error_code = errors[0].get("code") if errors else None
    sms.error_code = str(error_code) if error_code is not None else None
    await apply_sms_status(db, sms, new_status)
    await db.commit()
    return {"status": "applied"}
