"""Routes for generic, cross-channel dedicated-number provisioning.

Not SMS-specific: a dedicated PhoneNumber is a shared resource across
voice, SMS, and (later) MMS. Acquisition and listing live here alongside
`POST /numbers/{id}/enable-sms`: the route is SMS-shaped (Messaging
Service attachment) but operates on a PhoneNumber resource by id, so it
stays with the rest of the `/numbers` router rather than splitting onto
`routes/sms.py`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi import status as http_status
from hailhq.api.audit import write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.funds import BILLING_URL, FUNDS_RESPONSES, require_funds
from hailhq.api.idempotency import (
    IdempotencyContext,
    cache_failure,
    idempotency_dep,
    replay_cached,
)
from hailhq.api.number_orders import acquire_offer, reconcile_order
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.api.route_prefixes import request_mount_prefix
from hailhq.api.routes.sms import get_sms_provider
from hailhq.core import telephony_catalog
from hailhq.core.billing import get_balance_cents
from hailhq.core.carrier_routing import sms_route
from hailhq.core.db import get_session
from hailhq.core.models import AccountCredit, NumberOffer, PhoneNumber
from hailhq.core.number_offers import CarrierOffer, discover_offers, rank_offers
from hailhq.core.providers.sms import SmsProvider
from hailhq.core.providers.telnyx import TelnyxClient
from hailhq.core.providers.voice import (
    NumberNotProvisionable,
    TwilioVoiceProvider,
    VoiceProvider,
)
from hailhq.core.schemas import (
    NumberAcquireRequest,
    NumberType,
    PhoneNumberListResponse,
    PhoneNumberResponse,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/numbers", tags=["numbers"], responses=GENERAL_RATE_LIMITED_RESPONSES
)

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
        _voice_provider_singleton = TwilioVoiceProvider()
    return _voice_provider_singleton


async def _get_org_number_or_404(
    db: AsyncSession, number_id: UUID, organization_id: UUID
) -> PhoneNumber:
    """Fetch an org-scoped PhoneNumber by id, or raise 404."""
    number = (
        await db.execute(
            select(PhoneNumber).where(
                PhoneNumber.id == number_id,
                PhoneNumber.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if number is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="number not found"
        )
    return number


@router.post(
    "",
    response_model=PhoneNumberResponse,
    status_code=http_status.HTTP_201_CREATED,
    responses={
        402: {
            "description": (
                FUNDS_RESPONSES[402]["description"]
                + ". Also returned when the balance doesn't cover this "
                "number's full monthly price."
            ),
        },
    },
)
async def acquire_number(
    body: NumberAcquireRequest,
    response: Response,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[VoiceProvider, Depends(get_voice_provider)],
    idem: Annotated[IdempotencyContext | None, Depends(idempotency_dep)] = None,
) -> PhoneNumberResponse:
    """Buy a dedicated phone number for the caller's organization.

    This purchases a real number at the carrier and starts a recurring
    monthly fee immediately — it is not a reservation. The number is usable
    for voice, SMS, or both depending on the requested capabilities and
    what the carrier offers for the given country_code/number_type.
    """
    if idem is not None and idem.is_replay:
        _cached_id, cached = replay_cached(
            idem, response, request, resource_prefix="/numbers"
        )
        return PhoneNumberResponse.model_validate(cached)

    # Acquiring buys a real number at the carrier and starts a monthly fee —
    # same balance gate as the other paid create routes (/calls, /sms,
    # /emails). Must run before the carrier purchase below.
    if body.quote_id is None and body.provider in ("auto", "telnyx"):
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=422,
                detail="Request a live quote from POST /numbers/quotes, then pass its quote_id",
            ),
        )
    if body.quote_id is not None:
        try:
            number = await acquire_offer(
                db,
                principal.organization_id,
                body.quote_id,
                country=body.country_code,
                kind=body.number_type,
                provider=body.provider or "auto",
                billed=principal.auth_kind != "shared",
            )
        except HTTPException as exc:
            raise await cache_failure(idem, exc)
        response.headers["Location"] = (
            f"{request_mount_prefix(request)}/numbers/{number.id}"
        )
        result = PhoneNumberResponse.model_validate(number)
        if idem is not None:
            await idem.store(status_code=201, body=result.model_dump(mode="json"))
        return result

    await require_funds(db, principal, idem)
    caps = telephony_catalog.capabilities(body.country_code, body.number_type)
    if caps is None:
        raise await cache_failure(
            idem,
            unprocessable(
                f"we don't offer a {body.number_type} number in "
                f"{body.country_code} yet",
                loc=["body", "number_type"],
            ),
        )

    requested_caps = [c for c in ("voice", "sms") if caps[c]]
    if not requested_caps:
        # A catalog row with neither voice nor sms is schema-invalid, but the
        # runtime load doesn't schema-validate; an empty filter would let the
        # provider purchase an arbitrary number.
        raise await cache_failure(
            idem,
            unprocessable(
                f"the {body.number_type} number in {body.country_code} has no "
                "usable capabilities",
                loc=["body", "number_type"],
            ),
        )

    price = telephony_catalog.price_usd_per_month(body.country_code, body.number_type)
    if price is None or not price.is_finite() or price <= 0:
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=503, detail="number price unavailable; try again later"
            ),
        )
    amount_cents = int((price * 100).quantize(1, rounding=ROUND_HALF_UP))
    if amount_cents <= 0:
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=503, detail="number price unavailable; try again later"
            ),
        )
    billed = principal.auth_kind != "shared"
    if billed:
        # Serialize purchases and monthly debits for this organization.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": str(principal.organization_id)},
        )
        if await get_balance_cents(db, principal.organization_id) < amount_cents:
            raise await cache_failure(
                idem,
                HTTPException(
                    status_code=402,
                    detail=f"insufficient credits; this number costs ${price:.2f} per month; "
                    f"top up at {BILLING_URL}",
                ),
            )

    try:
        acquired = await provider.acquire_number(
            country_code=body.country_code,
            number_type=body.number_type,
            capabilities=requested_caps,
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
    except NumberNotProvisionable as exc:
        # Deterministic: this country/number-type can't be provisioned without
        # regulatory setup (a bundle/address) we don't have — a retry fails
        # identically, so cache the 422 rather than releasing the key. The raw
        # carrier reason is logged, not leaked to the caller.
        logger.warning(
            "number not provisionable (%s %s): %s",
            body.country_code,
            body.number_type,
            exc.detail,
        )
        raise await cache_failure(
            idem,
            unprocessable(
                f"we can't provision a {body.number_type} number in "
                f"{body.country_code} yet — it needs regulatory verification "
                "we don't support",
                loc=["body", "number_type"],
            ),
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
    acquired_at = datetime.now(timezone.utc)
    number.acquired_at = acquired_at
    db.add(number)
    try:
        await db.flush()
        if billed:
            db.add(
                AccountCredit(
                    organization_id=principal.organization_id,
                    kind="debit",
                    channel="voice" if "voice" in acquired.capabilities else "sms",
                    amount_cents=-amount_cents,
                    qty=1,
                    ref=f"monthly_fee:{principal.organization_id}:{number.id}:dedicated_number:{acquired_at:%Y-%m}",
                    source="monthly_fee",
                )
            )
        await db.commit()
    except Exception:
        await db.rollback()
        try:
            await provider.release_number(acquired.provider_resource_id)
        except Exception:
            logger.exception(
                "failed to release number after failed purchase commit: %s",
                acquired.provider_resource_id,
            )
        raise
    # No refresh: `id` (the PK) is populated via the INSERT's implicit RETURNING
    # and expire_on_commit=False keeps it live; PhoneNumberResponse reads no
    # other server-generated column.

    response.headers["Location"] = (
        f"{request_mount_prefix(request)}/numbers/{number.id}"
    )
    number_response = PhoneNumberResponse.model_validate(number)
    if idem is not None:
        await idem.store(
            status_code=http_status.HTTP_201_CREATED,
            body=number_response.model_dump(mode="json"),
        )
    return number_response


def _reject_if_released(number: PhoneNumber) -> None:
    """422 on a released row. A released row is a tombstone: its PN is
    deleted at Twilio, so any provisioning call against it would 404 into
    an opaque 500."""
    if number.provisioning_state != "active":
        raise unprocessable(
            f"this number is {number.provisioning_state}; wait for provisioning or acquire a new number",
            loc=["path", "number_id"],
        )


async def release_org_number(
    db: AsyncSession, provider: VoiceProvider, number: PhoneNumber
) -> PhoneNumber:
    """Release a dedicated number at the carrier and mark the row released.

    Idempotent: an already-released row is returned unchanged, and the
    provider tolerates a number that is already gone at the carrier.
    Shared by DELETE /numbers/{id} and the internal dunning release
    (routes/internal/numbers.py) so both paths stay identical.
    """
    # Serialize against enable_sms: it re-checks provisioning_state under the
    # org-keyed advisory lock and then talks to Twilio, so a release that does
    # not contend for the same lock could delete the PN between that re-check
    # and attach_number (an opaque Twilio 404 → 500, and a Messaging Service
    # SID committed onto a tombstone). Transaction-scoped: released at the
    # commit below (or at rollback).
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": str(number.organization_id)},
    )
    # Re-read under the lock: a concurrent release may have already
    # tombstoned the row after our caller loaded it.
    await db.refresh(number, ["provisioning_state", "released_at"])
    if number.provisioning_state == "released":
        return number
    if number.provisioning_state == "failed":
        number.provisioning_state = "released"
        # Never activated: no release-month rental charge is owed.
        number.released_at = None
        await db.commit()
        return number
    if number.provisioning_state == "pending":
        raise HTTPException(
            status_code=409,
            detail="Number order is still pending; refresh its status before releasing",
        )
    if number.provider == "telnyx":
        await TelnyxClient().release_number(number.provider_resource_id)
    elif number.provider == "twilio":
        await provider.release_number(number.provider_resource_id)
    else:
        raise HTTPException(status_code=409, detail="Unsupported number carrier")
    number.provisioning_state = "released"
    number.released_at = datetime.now(timezone.utc)
    try:
        await db.commit()
    except Exception:
        # The carrier release already happened; until a retry converges the
        # row, the monthly-fee rater keeps billing a number that is gone at
        # Twilio. Loud on purpose.
        logger.error(
            "number %s released at carrier but the DB commit failed; "
            "retry the release to stop billing",
            number.id,
        )
        raise
    return number


@router.delete(
    "/{number_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
async def release_number(
    number_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[VoiceProvider, Depends(get_voice_provider)],
) -> None:
    """Release a dedicated number. The monthly fee stops accruing after the
    release month; months already accrued stay owed (the rater bills late,
    never forgives)."""
    number = await _get_org_number_or_404(db, number_id, principal.organization_id)
    # Best-effort pre-check so an idempotent re-DELETE doesn't append a
    # second audit entry (audit is a safety net, not a correctness gate).
    was_released = number.provisioning_state == "released"
    await release_org_number(db, provider, number)
    if not was_released:
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="number.release",
            resource_type="phone_number",
            resource_id=number.id,
            payload={"e164": number.e164},
        )


@router.get(
    "/{number_id}",
    response_model=PhoneNumberResponse,
)
async def get_number(
    number_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> PhoneNumberResponse:
    """Fetch one dedicated number by id, including its capabilities and state.

    Org-scoped: returns 404 for a number belonging to a different
    organization.
    """
    number = await _get_org_number_or_404(db, number_id, principal.organization_id)
    if (
        number.provisioning_state == "pending"
        and "offer" in number.provisioning_metadata
    ):
        try:
            await reconcile_order(db, number)
        except Exception:
            # A carrier outage must not turn a status read into a 500. The
            # sweeper keeps retrying; report the last committed state.
            await db.rollback()
            logger.warning(
                "Carrier order status unavailable: number=%s", number.id, exc_info=True
            )
            number = await _get_org_number_or_404(
                db, number_id, principal.organization_id
            )
    return PhoneNumberResponse.model_validate(number)


@router.get(
    "",
    response_model=PhoneNumberListResponse,
)
async def list_numbers(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
) -> PhoneNumberListResponse:
    """List dedicated numbers owned by the caller's organization.

    Cursor-paginated, newest first. Only org-owned numbers are listed —
    shared pool numbers used for outbound calls never appear here.
    """
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


@router.post(
    "/{number_id}/enable-sms",
    response_model=PhoneNumberResponse,
)
async def enable_sms(
    number_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SmsProvider, Depends(get_sms_provider)],
) -> PhoneNumberResponse:
    """Attach a dedicated number to the org's shared SMS Messaging Service.

    Required once per number before it can send/receive SMS; the number
    must already have been acquired with sms capability. Idempotent —
    calling this again on an already-enabled number just returns its
    current state. Fails with 422 for a released number or one that lacks
    sms capability.
    """
    number = await _get_org_number_or_404(db, number_id, principal.organization_id)

    _reject_if_released(number)

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

    # Serialize concurrent enable-sms within an org. Provisioning the org's
    # shared Messaging Service is a get-or-create: two parallel enables would
    # otherwise both observe no existing service and each create one (leaving
    # orphaned duplicates). A transaction-scoped advisory lock keyed on the org
    # (auto-released at commit/rollback) makes any waiter see the first
    # request's committed result. release_org_number takes the same org-keyed
    # lock on purpose — that is what makes the released re-check below
    # authoritative rather than a race window; no other code path takes
    # advisory locks.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": str(principal.organization_id)},
    )
    # Re-read under the lock: a concurrent enable of THIS number may have just
    # attached it (its SID was NULL when the row was first loaded), and a
    # concurrent release may have tombstoned it (its PN is gone at Twilio).
    await db.refresh(number, ["messaging_service_sid", "provisioning_state"])
    _reject_if_released(number)
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
                PhoneNumber.provider == number.provider,
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    provider = sms_route(number.provider, provider)
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


__all__ = ["get_voice_provider", "release_org_number", "router"]


class NumberQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country_code: str = Field(
        pattern=r"^[A-Z]{2}$",
        description="Uppercase ISO alpha-2 country code to search.",
    )
    number_type: NumberType | None = Field(
        default=None,
        description="Restrict number type; omit to compare all supported types.",
    )
    capabilities: list[Literal["voice", "sms"]] = Field(
        min_length=1,
        max_length=2,
        description="Required channels; every returned offer must support all requested capabilities.",
    )
    provider: Literal["auto", "twilio", "telnyx"] = Field(
        default="auto",
        description="Carrier preference; auto compares readiness, remaining verification effort, and rental/setup costs. Twilio wins equivalent ties.",
    )


class NumberQuotesResponse(BaseModel):
    offers: list[CarrierOffer] = Field(
        description="Live carrier offers ordered by readiness, remaining verification effort, monthly price, setup price, and Twilio tie-break."
    )
    recommended_quote_id: UUID | None = Field(
        description="Recommended ready offer matching the requested carrier preference, or null if none qualifies."
    )
    unavailable_providers: list[str] = Field(
        description="Carriers whose inventory, price, or regulatory lookup failed; comparison may be incomplete."
    )
    expires_at: datetime = Field(
        description="UTC expiry of these persisted quotes; request fresh offers afterward."
    )


@router.post("/quotes", response_model=NumberQuotesResponse)
async def quote_numbers(
    body: NumberQuoteRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
):
    """Compare live, org-specific offers. Prices include setup + monthly rent.

    Auto recommends a ready offer with the lowest monthly rent, then setup
    cost, preferring Twilio on equivalent ties. Blocked offers sort by verification effort. SMS capability does not waive messaging registration requirements.
    """

    batches = await asyncio.gather(
        *(
            discover_offers(
                principal.organization_id, body.country_code, kind, body.capabilities
            )
            for kind in (
                [body.number_type]
                if body.number_type
                else ["local", "mobile", "national", "toll_free"]
            )
        )
    )
    offers = rank_offers([offer for batch, _ in batches for offer in batch])
    unavailable = sorted({p for _, failures in batches for p in failures})
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    for offer in offers:
        row = NumberOffer(
            organization_id=principal.organization_id,
            offer=offer.model_dump(mode="json"),
            expires_at=expires,
        )
        db.add(row)
        await db.flush()
        offer.quote_id = row.id
    await db.commit()
    ranked = rank_offers(offers, body.provider)
    recommended = next((o for o in ranked if o.readiness == "ready"), None)
    return {
        "offers": offers,
        "recommended_quote_id": recommended.quote_id if recommended else None,
        "unavailable_providers": unavailable,
        "expires_at": expires,
    }
