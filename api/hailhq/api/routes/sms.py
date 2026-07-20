"""Routes for the outbound SMS channel.

POST /sms - send an outbound SMS from the org's dedicated number.
GET /sms/{id} - read a single message (org-scoped).
GET /sms - cursor-paginated list (org-scoped, optional status / to filters).

No pool fallback: SMS requires a dedicated PhoneNumber on the org (see
Decision 6 of the SMS design spec) — inbound replies need unambiguous
number-to-org routing, which a shared pool number can't provide.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi import status as http_status
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.audit import write_audit_log
from hailhq.api.consent import enforce_consent, isoformat_or_none
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.idempotency import (
    IdempotencyContext,
    cache_failure,
    idempotency_dep,
    replay_cached,
)
from hailhq.api.numbers import resolve_org_number
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.usage import write_usage_event
from hailhq.api.agent_gate import (
    RATE_LIMITED_RESPONSES,
    require_agent_send_allowed,
)
from hailhq.api.funds import require_funds
from hailhq.core.compliance_gate import check_sms_allowed, remove_suppression
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.models import Sms, SmsEvent, SmsSenderIdentity, Suppression
from hailhq.core.pricing_tier import classify_pricing_tier
from hailhq.core.providers.sms import SmsProvider, TwilioSmsProvider
from hailhq.core.providers.sms.status_map import map_twilio_message_status
from hailhq.core.sender_id import PLATFORM_DEFAULT_SENDER_ID, resolve_sender
from hailhq.core.schemas import (
    SenderIdPatch,
    SenderIdResponse,
    SmsCreate,
    SmsListResponse,
    SmsResponse,
    SmsStatus,
    SuppressionListResponse,
    SuppressionResponse,
)
from hailhq.core.sms_ingest import ingest_inbound_sms
from hailhq.core.twilio_signature import verify_twilio_signature
from hailhq.core.urls import join_url
from hailhq.core.webhook_fanout import fanout_sms_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sms", tags=["sms"])

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200
_SMS_SEND_FAILED_DETAIL = "sms send failed"


_sms_provider_singleton: SmsProvider | None = None


def get_sms_provider() -> SmsProvider:
    """Return a process-wide ``SmsProvider``. Tests override via
    ``app.dependency_overrides``."""
    global _sms_provider_singleton
    if _sms_provider_singleton is None:
        _sms_provider_singleton = TwilioSmsProvider()
    return _sms_provider_singleton


async def deliver_sms(db: AsyncSession, provider: SmsProvider, sms: Sms) -> str | None:
    """Wire-send one queued Sms row and reconcile its status.

    Shared by POST /sms and the internal agent-send route. Returns None
    when the carrier accepted (status='sent', usage billed),
    'provider_error' on transport failure, or the carrier error code on
    rejection. Never raises — the caller owns HTTP semantics.
    """
    callback_url = join_url(settings.hail_api_url, "sms/status")
    try:
        result = await provider.send_sms(
            from_e164=sms.from_e164,
            to_e164=sms.to_e164,
            body=sms.body,
            status_callback_url=callback_url,
        )
    except Exception:
        logger.warning("sms send failed for sms_id=%s", sms.id, exc_info=True)
        sms.status = "failed"
        db.add(
            SmsEvent(
                sms_id=sms.id,
                organization_id=sms.organization_id,
                kind="state_change",
                payload={"from": "queued", "to": "failed", "reason": "provider_error"},
            )
        )
        await fanout_sms_event(
            db,
            organization_id=sms.organization_id,
            event_type="sms.failed",
            event_id=sms.id,
            data={
                "id": str(sms.id),
                "to": sms.to_e164,
                "from": sms.from_e164,
                "status": "failed",
                "reason": "provider_error",
            },
        )
        await db.commit()
        return "provider_error"

    # A carrier rejection surfaces via error_code AND/OR a failure status
    # (base.ProviderSmsResult contract: "status reflecting the failure").
    # Check both so a provider that reports failure by status alone isn't
    # recorded as sent and billed.
    carrier_rejected = result.error_code is not None or result.status.lower() in {
        "failed",
        "undelivered",
    }
    new_status = "failed" if carrier_rejected else "sent"
    sms.status = new_status
    sms.provider_message_sid = result.provider_message_sid
    sms.segment_count = result.segment_count
    sms.error_code = result.error_code
    # ``sent_at`` means "the carrier accepted the message" — a rejected
    # send keeps it NULL, matching emails (sent_at only with status='sent')
    # and this route's own transport-failure branch above.
    if not carrier_rejected:
        sms.sent_at = datetime.now(timezone.utc)
    event_payload: dict[str, Any] = {"from": "queued", "to": new_status}
    if carrier_rejected:
        event_payload["error_code"] = result.error_code
    db.add(
        SmsEvent(
            sms_id=sms.id,
            organization_id=sms.organization_id,
            kind="state_change",
            payload=event_payload,
        )
    )
    if carrier_rejected:
        await fanout_sms_event(
            db,
            organization_id=sms.organization_id,
            event_type="sms.failed",
            event_id=sms.id,
            data={
                "id": str(sms.id),
                "to": sms.to_e164,
                "from": sms.from_e164,
                "status": "failed",
                "error_code": result.error_code,
            },
        )
    await db.commit()

    if carrier_rejected:
        return result.error_code or "carrier_rejected"

    tier = classify_pricing_tier(sms.to_e164)
    await write_usage_event(
        organization_id=sms.organization_id,
        channel="sms",
        units=sms.segment_count,
        ref=f"sms:{sms.id}:{tier}",
    )
    return None


@router.post(
    "",
    response_model=SmsResponse,
    status_code=http_status.HTTP_201_CREATED,
    responses=RATE_LIMITED_RESPONSES,
)
async def create_sms(
    body: SmsCreate,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SmsProvider, Depends(get_sms_provider)],
    idem: Annotated[IdempotencyContext | None, Depends(idempotency_dep)] = None,
) -> SmsResponse:
    if idem is not None and idem.is_replay:
        cached_id, cached = replay_cached(idem, response, resource_prefix="/sms")
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="sms.create.replayed",
            resource_type="sms",
            resource_id=cached_id,
            payload={"to": cached.get("to_e164"), "from": cached.get("from_e164")},
        )
        return SmsResponse.model_validate(cached)

    try:
        enforce_consent(
            recipient_consent=body.recipient_consent,
            consent_source=body.consent_source,
            message_type=body.message_type,
        )
    except HTTPException as exc:
        raise await cache_failure(idem, exc) from None

    gate = await check_sms_allowed(db, principal.organization_id, body.to)
    if not gate.allowed:
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="sms.blocked",
            resource_type="sms",
            resource_id=None,
            payload={"to": body.to, "reason": gate.reason, "checks": gate.checks},
        )
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN, detail=gate.reason
            ),
        )

    await require_funds(db, principal, idem)
    await require_agent_send_allowed(db, principal, "sms", [body.to], idem)

    # The dedicated-number requirement is conditional on the destination
    # corridor (Task 5). For alphanumeric-eligible corridors with no explicit
    # ``from``, the org's Sender ID is used and NO dedicated number is needed;
    # otherwise a dedicated SMS-capable number is still required (Decision 6).
    #
    # An explicit ``from`` always resolves a dedicated number regardless of
    # corridor, so the Sender ID lookup is skipped entirely in that case — no
    # SmsSenderIdentity query on explicit-``from`` sends.
    from_number = None
    from_e164 = None
    if body.from_ is None:
        sender_id_row = (
            await db.execute(
                select(SmsSenderIdentity).where(
                    SmsSenderIdentity.organization_id == principal.organization_id
                )
            )
        ).scalar_one_or_none()
        resolution = resolve_sender(
            body.to,
            custom_sender_id=sender_id_row.custom_sender_id if sender_id_row else None,
        )
        if resolution.kind == "alphanumeric":
            # Alphanumeric corridor with no explicit ``from`` → send from the
            # resolved Sender ID; no dedicated number is provisioned.
            from_e164 = resolution.sender_id

    if from_e164 is None:
        # Explicit ``from``, or a corridor that still requires a dedicated
        # SMS-capable number.
        from_number = await resolve_org_number(
            db, principal.organization_id, body.from_, capability="sms"
        )
        if from_number is None:
            if body.from_ is not None:
                msg = (
                    f"phone number {body.from_} is not registered to this "
                    "organization, is not active, or lacks the sms capability"
                )
            else:
                msg = (
                    "no dedicated SMS-capable phone number on this organization; "
                    "SMS requires a dedicated number, not the shared voice pool"
                )
            raise await cache_failure(idem, unprocessable(msg, loc=["body", "from"]))
        from_e164 = from_number.e164

    sms = Sms(
        organization_id=principal.organization_id,
        from_number_id=from_number.id if from_number is not None else None,
        from_e164=from_e164,
        to_e164=body.to,
        direction="outbound",
        status="queued",
        body=body.body,
        metadata_=dict(body.metadata),
    )
    # No refresh needed: expire_on_commit=False and asyncpg INSERT..RETURNING
    # (eager_defaults) leave id/timestamps populated on the instance.
    db.add(sms)
    await db.commit()

    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="sms.create",
        resource_type="sms",
        resource_id=sms.id,
        payload={
            "to": sms.to_e164,
            "from": sms.from_e164,
            "recipient_consent": body.recipient_consent,
            "consent_source": body.consent_source,
            "consent_obtained_at": isoformat_or_none(body.consent_obtained_at),
            "message_type": body.message_type,
            "compliance": gate.checks,
        },
    )

    # Provider send — best-effort with status reconciliation.
    err = await deliver_sms(db, provider, sms)
    if err == "provider_error":
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail=_SMS_SEND_FAILED_DETAIL,
            ),
        )
    # carrier rejection: row already reconciled to failed; fall through and
    # return the SmsResponse exactly as before.

    response.headers["Location"] = f"/sms/{sms.id}"
    sms_response = SmsResponse.model_validate(sms)

    if idem is not None:
        await idem.store(
            status_code=http_status.HTTP_201_CREATED,
            body=sms_response.model_dump(mode="json"),
        )

    return sms_response


@router.post("/inbound", include_in_schema=False)
async def receive_inbound_sms(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SmsProvider, Depends(get_sms_provider)],
) -> Response:
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature")
    url = join_url(settings.hail_api_url, "sms/inbound")

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
    )
    await db.commit()
    return Response(status_code=http_status.HTTP_200_OK)


# Once an Sms reaches any of these, it is done: no later callback — however
# delayed or out-of-order Twilio's at-least-once redelivery makes it — may
# change status or fan out again.
_TERMINAL_SMS_STATUSES = frozenset({"delivered", "undelivered", "failed"})


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
    url = join_url(settings.hail_api_url, "sms/status")
    if not verify_twilio_signature(url, params, signature, settings.twilio_auth_token):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN, detail="invalid signature"
        )

    new_status = map_twilio_message_status(params.get("MessageStatus", ""))
    if new_status is None:
        return {"status": "ignored"}

    sid = params.get("MessageSid")
    sms = (
        await db.execute(
            select(Sms).where(Sms.provider_message_sid == sid).with_for_update()
        )
    ).scalar_one_or_none()
    if sms is None:
        return {"status": "unmatched"}

    if sms.status in _TERMINAL_SMS_STATUSES or new_status == sms.status:
        # Emit-once: the row lock serializes concurrent/duplicate callbacks
        # for this message; a terminal status is absorbing (an out-of-order
        # redelivery must not flip it back), and no status change means no
        # new event either way.
        return {"status": "duplicate"}

    prior = sms.status
    sms.status = new_status
    db.add(
        SmsEvent(
            sms_id=sms.id,
            organization_id=sms.organization_id,
            kind="state_change",
            payload={"from": prior, "to": new_status},
        )
    )
    if new_status in {"delivered", "undelivered", "failed"}:
        await fanout_sms_event(
            db,
            organization_id=sms.organization_id,
            event_type=f"sms.{new_status}",
            event_id=sms.id,
            data={
                "id": str(sms.id),
                "to": sms.to_e164,
                "from": sms.from_e164,
                "status": new_status,
            },
        )
    await db.commit()
    return {"status": "applied"}


@router.get("/suppressions", response_model=SuppressionListResponse)
async def list_sms_suppressions(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
) -> SuppressionListResponse:
    stmt = select(Suppression).where(
        Suppression.organization_id == principal.organization_id,
        Suppression.channel == "sms",
    )
    rows, next_cursor = await fetch_cursor_page(
        db,
        stmt,
        Suppression.created_at,
        Suppression.id,
        cursor=cursor,
        limit=limit,
        newest_first=True,
    )
    return SuppressionListResponse(
        items=[SuppressionResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.delete("/suppressions/{number}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_sms_suppression(
    number: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    removed = await remove_suppression(
        db, organization_id=principal.organization_id, recipient=number, channel="sms"
    )
    if not removed:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="suppression not found"
        )
    await db.commit()


@router.get("/sender-id", response_model=SenderIdResponse)
async def get_sender_id(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SenderIdResponse:
    stmt = select(SmsSenderIdentity).where(
        SmsSenderIdentity.organization_id == principal.organization_id
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    return SenderIdResponse(
        custom_sender_id=row.custom_sender_id if row else None,
        effective_default=PLATFORM_DEFAULT_SENDER_ID,
    )


@router.patch("/sender-id", response_model=SenderIdResponse)
async def patch_sender_id(
    body: SenderIdPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SenderIdResponse:
    if body.custom_sender_id is None:
        # Clear: unconditional delete (a no-op when no row exists), so there is
        # no read-then-delete window.
        await db.execute(
            delete(SmsSenderIdentity).where(
                SmsSenderIdentity.organization_id == principal.organization_id
            )
        )
        await db.commit()
        return SenderIdResponse(
            custom_sender_id=None, effective_default=PLATFORM_DEFAULT_SENDER_ID
        )

    # Set: atomic upsert keyed on the org PK, so two concurrent first-writes
    # can't both INSERT and 500 on a duplicate-key IntegrityError (one round
    # trip instead of SELECT-then-INSERT/UPDATE).
    await db.execute(
        pg_insert(SmsSenderIdentity)
        .values(
            organization_id=principal.organization_id,
            custom_sender_id=body.custom_sender_id,
        )
        .on_conflict_do_update(
            index_elements=["organization_id"],
            set_={"custom_sender_id": body.custom_sender_id, "updated_at": func.now()},
        )
    )
    await db.commit()
    return SenderIdResponse(
        custom_sender_id=body.custom_sender_id,
        effective_default=PLATFORM_DEFAULT_SENDER_ID,
    )


@router.get("/{sms_id}", response_model=SmsResponse)
async def get_sms(
    sms_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SmsResponse:
    stmt = select(Sms).where(
        Sms.id == sms_id,
        Sms.organization_id == principal.organization_id,
    )
    sms = (await db.execute(stmt)).scalar_one_or_none()
    if sms is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="sms not found"
        )
    return SmsResponse.model_validate(sms)


@router.get("", response_model=SmsListResponse)
async def list_sms(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
    status: SmsStatus | None = Query(default=None),
    to: str | None = Query(default=None),
) -> SmsListResponse:
    stmt = select(Sms).where(Sms.organization_id == principal.organization_id)
    if status is not None:
        stmt = stmt.where(Sms.status == status)
    if to is not None:
        stmt = stmt.where(Sms.to_e164 == to)
    rows, next_cursor = await fetch_cursor_page(
        db, stmt, Sms.created_at, Sms.id, cursor=cursor, limit=limit, newest_first=True
    )
    return SmsListResponse(
        items=[SmsResponse.model_validate(s) for s in rows],
        next_cursor=next_cursor,
    )


__all__ = ["router", "get_sms_provider", "deliver_sms"]
