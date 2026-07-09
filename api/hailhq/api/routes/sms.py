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

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy import select
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
from hailhq.api.funds import require_funds
from hailhq.core.compliance_gate import check_sms_allowed
from hailhq.core.db import get_session
from hailhq.core.models import Sms, SmsEvent
from hailhq.core.providers.sms import SmsProvider, TwilioSmsProvider
from hailhq.core.schemas import SmsCreate, SmsListResponse, SmsResponse, SmsStatus

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


@router.post("", response_model=SmsResponse, status_code=http_status.HTTP_201_CREATED)
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

    # No pool fallback for SMS — a dedicated number is required (Decision 6).
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

    sms = Sms(
        organization_id=principal.organization_id,
        from_number_id=from_number.id,
        from_e164=from_number.e164,
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

    try:
        result = await provider.send_sms(
            from_e164=sms.from_e164, to_e164=sms.to_e164, body=sms.body
        )
    except Exception as exc:
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
        await db.commit()
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail=_SMS_SEND_FAILED_DETAIL,
            ),
        ) from exc

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
    await db.commit()

    if not carrier_rejected:
        await write_usage_event(
            organization_id=principal.organization_id,
            channel="sms",
            units=sms.segment_count,
            ref=f"sms:{sms.id}",
        )

    response.headers["Location"] = f"/sms/{sms.id}"
    sms_response = SmsResponse.model_validate(sms)

    if idem is not None:
        await idem.store(
            status_code=http_status.HTTP_201_CREATED,
            body=sms_response.model_dump(mode="json"),
        )

    return sms_response


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


__all__ = ["router", "get_sms_provider"]
