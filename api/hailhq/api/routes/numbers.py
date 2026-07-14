"""Routes for generic, cross-channel dedicated-number provisioning.

Not SMS-specific: a dedicated PhoneNumber is a shared resource across
voice, SMS, and (later) MMS. Acquisition and listing live here alongside
`POST /numbers/{id}/enable-sms`: the route is SMS-shaped (Messaging
Service attachment) but operates on a PhoneNumber resource by id, so it
stays with the rest of the `/numbers` router rather than splitting onto
`routes/sms.py`.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.idempotency import (
    IdempotencyContext,
    idempotency_dep,
    replay_cached,
)
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.routes.sms import get_sms_provider
from hailhq.core.db import get_session
from hailhq.core.models import PhoneNumber
from hailhq.core.providers.sms import SmsProvider
from hailhq.core.providers.voice import VoiceProvider
from hailhq.core.schemas import (
    NumberAcquireRequest,
    PhoneNumberListResponse,
    PhoneNumberResponse,
)

router = APIRouter(prefix="/numbers", tags=["numbers"])

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200

# Reuses the calls.py get_livekit-style lazy-singleton pattern for the
# voice provider used to acquire a physical number (acquire_number is a
# carrier-numbers concern, not a call-dialing concern, per
# providers/voice/base.py's own docstring).
_voice_provider_singleton: VoiceProvider | None = None


def get_voice_provider() -> VoiceProvider:
    global _voice_provider_singleton
    if _voice_provider_singleton is None:
        from hailhq.core.providers.voice import TwilioVoiceProvider

        _voice_provider_singleton = TwilioVoiceProvider()
    return _voice_provider_singleton


@router.post(
    "", response_model=PhoneNumberResponse, status_code=http_status.HTTP_201_CREATED
)
async def acquire_number(
    body: NumberAcquireRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[VoiceProvider, Depends(get_voice_provider)],
    idem: Annotated[IdempotencyContext | None, Depends(idempotency_dep)] = None,
) -> PhoneNumberResponse:
    if idem is not None and idem.is_replay:
        cached_id, cached = replay_cached(idem, response, resource_prefix="/numbers")
        return PhoneNumberResponse.model_validate(cached)

    try:
        acquired = await provider.acquire_number(
            country_code=body.country_code,
            number_type=body.number_type,
            capabilities=["voice", "sms"],
        )
    except LookupError as exc:
        # Transient: the carrier has no matching inventory right now. Release the
        # in-flight idempotency sentinel (rather than caching the 503) so a
        # same-key retry can succeed once inventory returns — mirroring the
        # shared-pool-exhausted path in calls.py. Caching it would freeze the
        # 503 under this key for the full 24h TTL.
        if idem is not None:
            await idem.release()
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    number = PhoneNumber(
        organization_id=principal.organization_id,
        e164=acquired.e164,
        country_code=acquired.country_code,
        number_type=acquired.number_type,
        capabilities=acquired.capabilities,
        provider_resource_id=acquired.provider_resource_id,
        provisioning_state="active",
        is_pool=False,
    )
    db.add(number)
    await db.commit()
    # No refresh: `id` (the PK) is populated via the INSERT's implicit RETURNING
    # and expire_on_commit=False keeps it live; PhoneNumberResponse reads no
    # other server-generated column.

    response.headers["Location"] = f"/numbers/{number.id}"
    number_response = PhoneNumberResponse.model_validate(number)
    if idem is not None:
        await idem.store(
            status_code=http_status.HTTP_201_CREATED,
            body=number_response.model_dump(mode="json"),
        )
    return number_response


@router.get("/{number_id}", response_model=PhoneNumberResponse)
async def get_number(
    number_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> PhoneNumberResponse:
    stmt = select(PhoneNumber).where(
        PhoneNumber.id == number_id,
        PhoneNumber.organization_id == principal.organization_id,
    )
    number = (await db.execute(stmt)).scalar_one_or_none()
    if number is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="number not found"
        )
    return PhoneNumberResponse.model_validate(number)


@router.get("", response_model=PhoneNumberListResponse)
async def list_numbers(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
) -> PhoneNumberListResponse:
    stmt = select(PhoneNumber).where(
        PhoneNumber.organization_id == principal.organization_id,
        PhoneNumber.is_pool.is_(False),
    )
    rows, next_cursor = await fetch_cursor_page(
        db,
        stmt,
        PhoneNumber.created_at,
        PhoneNumber.id,
        cursor=cursor,
        limit=limit,
        newest_first=True,
    )
    return PhoneNumberListResponse(
        items=[PhoneNumberResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.post("/{number_id}/enable-sms", response_model=PhoneNumberResponse)
async def enable_sms(
    number_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SmsProvider, Depends(get_sms_provider)],
) -> PhoneNumberResponse:
    stmt = select(PhoneNumber).where(
        PhoneNumber.id == number_id,
        PhoneNumber.organization_id == principal.organization_id,
    )
    number = (await db.execute(stmt)).scalar_one_or_none()
    if number is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="number not found"
        )

    if "sms" not in number.capabilities:
        raise unprocessable(
            "this number does not support sms (fixed at purchase time by the "
            "carrier); acquire a new number with sms capability instead",
            loc=["path", "number_id"],
        )

    # Idempotent: an already-enabled number is attached to its Messaging
    # Service; re-attaching would error at Twilio. Return the current state.
    if number.messaging_service_sid is not None:
        return PhoneNumberResponse.model_validate(number)

    # One Messaging Service per org (a shared sender pool). Reuse the org's
    # existing service if any of its numbers already has one; only when the org
    # has none does ensure_messaging_service create a fresh one — otherwise
    # every enabled number would spawn its own orphan Messaging Service.
    existing_sid = (
        await db.execute(
            select(PhoneNumber.messaging_service_sid)
            .where(
                PhoneNumber.organization_id == principal.organization_id,
                PhoneNumber.messaging_service_sid.is_not(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    messaging_service_sid = await provider.ensure_messaging_service(
        organization_id=principal.organization_id, existing_sid=existing_sid
    )
    await provider.attach_number(
        messaging_service_sid=messaging_service_sid,
        provider_resource_id=number.provider_resource_id,
    )

    # Stored for future send routing: this provisions and records the org's
    # Messaging Service, but POST /sms does not yet send *through* it (it sends
    # with an explicit from_e164 / alphanumeric sender). Routing outbound SMS
    # via the Messaging Service is a later phase — the SID is persisted now so
    # that wiring has it ready.
    number.messaging_service_sid = messaging_service_sid
    await db.commit()
    return PhoneNumberResponse.model_validate(number)


__all__ = ["router", "get_voice_provider"]
