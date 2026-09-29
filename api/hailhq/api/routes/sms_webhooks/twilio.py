"""Twilio SMS webhooks: inbound messages and delivery status. The URLs are
set in the Twilio console and in ``CARRIERS`` (``sms_status_path``)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import status as http_status
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.api.route_prefixes import request_mount_prefix
from hailhq.api.routes.sms import (
    TERMINAL_SMS_STATUSES,
    apply_sms_status,
    get_sms_provider,
)
from hailhq.core.carrier_routing import TWILIO
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.models import Sms
from hailhq.core.providers.sms import SmsProvider
from hailhq.core.providers.sms.status_map import map_twilio_message_status
from hailhq.core.sms_ingest import ingest_inbound_sms
from hailhq.core.twilio_signature import verify_twilio_signature
from hailhq.core.urls import join_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/sms", tags=["sms"], responses=GENERAL_RATE_LIMITED_RESPONSES
)


@router.post("/inbound", include_in_schema=False)
async def receive_inbound_sms(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SmsProvider, Depends(get_sms_provider)],
) -> Response:
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature")
    # Twilio signs the URL it was configured with, so verify against the mount
    # (/v1 or legacy) this request actually arrived on.
    url = join_url(
        settings.hail_api_url, f"{request_mount_prefix(request)}/sms/inbound"
    )

    if not verify_twilio_signature(url, params, signature, settings.twilio_auth_token):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN, detail="invalid signature"
        )

    await ingest_inbound_sms(
        db,
        from_e164=params.get("From", ""),
        to_e164=params.get("To", ""),
        body=params.get("Body", ""),
        provider_message_sid=params.get("MessageSid") or None,
        opt_out_type=params.get("OptOutType"),
        provider=provider,
        carrier=TWILIO,
    )
    await db.commit()
    return Response(status_code=http_status.HTTP_200_OK)


@router.post("/status", include_in_schema=False)
async def receive_sms_status(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Twilio delivery-status callback — transitions ``Sms.status`` and fans
    out ``sms.delivered`` / ``sms.undelivered`` / ``sms.failed``.

    Emit-once relies on a ``SELECT ... FOR UPDATE`` row lock plus a
    status-unchanged short-circuit rather than a dedup constraint: Twilio
    redelivers at-least-once, and locking the row serializes concurrent
    callbacks for the same message so only the callback that actually
    changes ``status`` writes an event or fans out. ``sms.sent`` is not a
    subscribable event, so fan-out is gated to the three terminal statuses.
    Those same terminal statuses are also absorbing: once set, no later
    callback — including an out-of-order redelivery for an earlier status —
    may change ``status`` or fan out again.
    """
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature")
    # Twilio signs the URL it was configured with, so verify against the mount
    # (/v1 or legacy) this request actually arrived on.
    url = join_url(settings.hail_api_url, f"{request_mount_prefix(request)}/sms/status")
    if not verify_twilio_signature(url, params, signature, settings.twilio_auth_token):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN, detail="invalid signature"
        )

    new_status = map_twilio_message_status(params.get("MessageStatus", ""))
    if new_status is None:
        return {"status": "ignored"}

    sid = params.get("MessageSid")
    if not sid:
        return {"status": "unmatched"}
    sms = (
        await db.execute(
            select(Sms)
            .where(Sms.provider_message_sid == sid, Sms.provider == TWILIO)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if sms is None:
        return {"status": "unmatched"}

    if sms.status in TERMINAL_SMS_STATUSES or new_status == sms.status:
        # Emit-once: the row lock serializes concurrent/duplicate callbacks
        # for this message; a terminal status is absorbing (an out-of-order
        # redelivery must not flip it back), and no status change means no
        # new event either way.
        return {"status": "duplicate"}

    await apply_sms_status(db, sms, new_status)
    await db.commit()
    return {"status": "applied"}
