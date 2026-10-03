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
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi import status as http_status
from hailhq.api.audit import actor_of, write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.funds import FUNDS_RESPONSES
from hailhq.api.idempotency import (
    IdempotencyContext,
    cache_failure,
    idempotency_dep,
    replay_cached,
)
from hailhq.api.number_orders import (
    RetryableError,
    catalog_capabilities,
    purchase_number,
)
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.api.route_prefixes import request_mount_prefix
from hailhq.api.routes.calls import get_livekit, get_livekit_optional
from hailhq.core import inbound_routing, telephony_catalog
from hailhq.core.carrier_routing import CARRIERS, carrier, sms_route
from hailhq.core.db import get_session, org_lock
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import Agent, NumberOffer, PhoneNumber
from hailhq.core.number_offers import (
    PROVIDERS,
    CarrierOffer,
    discover_offers,
    rank_offers,
)
from hailhq.core.providers.voice import CarrierNotConfigured
from hailhq.core.schemas import (
    NumberAcquireRequest,
    NumberQuoteRequest,
    PhoneNumberListResponse,
    PhoneNumberResponse,
    PhoneNumberRoutingUpdate,
)
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/numbers", tags=["numbers"], responses=GENERAL_RATE_LIMITED_RESPONSES
)

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200


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
        404: {"description": "The quote does not exist for this organization."},
        422: {
            "description": (
                "quote_id is missing (request one from POST /numbers/quotes) or "
                "the quote does not match the country, type or provider."
            ),
        },
        402: {
            "description": (
                FUNDS_RESPONSES[402]["description"]
                + ". Also returned when the balance doesn't cover this "
                "number's full monthly price."
            ),
        },
        409: {
            "description": (
                "The quote expired, the number is taken, the price changed, or the "
                "carrier rejected or failed the order. In the last case the credit "
                "hold is refunded and the detail gives the reason. Also returned "
                "when the quote's number was released since it was bought."
            ),
        },
        503: {
            "description": (
                "The carrier lookup or number price is unavailable, or no "
                "inventory matched. Nothing was charged; retry shortly."
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
    idem: Annotated[IdempotencyContext | None, Depends(idempotency_dep)] = None,
) -> PhoneNumberResponse:
    """Buy a dedicated phone number for the caller's organization.

    This purchases a real number at the carrier and starts a recurring
    monthly fee immediately — it is not a reservation. quote_id is required:
    request live offers from POST /numbers/quotes first, then buy one. The
    number is usable for voice, SMS, or both, as the quote lists.
    """
    if idem is not None and idem.is_replay:
        _cached_id, cached = replay_cached(
            idem, response, request, resource_prefix="/numbers"
        )
        return PhoneNumberResponse.model_validate(cached)

    try:
        number = await purchase_number(db, principal, body)
    except RetryableError:
        # Transient (no charge was made): release the in-flight sentinel
        # instead of caching, so a same-key retry can succeed once the carrier
        # recovers.
        if idem is not None:
            await idem.release()
        raise
    except HTTPException as exc:
        raise await cache_failure(idem, exc)

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
    db: AsyncSession, number: PhoneNumber, lk: LiveKitClient | None = None
) -> PhoneNumber:
    """Release a dedicated number at the carrier and mark the row released.

    Idempotent: an already-released row is returned unchanged, and the
    provider tolerates a number that is already gone at the carrier.
    Shared by DELETE /numbers/{id} and the internal dunning release
    (routes/internal/numbers.py) so both paths stay identical.

    A number registered for inbound is taken off the LiveKit trunk first
    (best effort: the carrier release below removes the number either way,
    and a stale trunk entry only wastes a lookup on LiveKit's side).
    """
    # Serialize against enable_sms: it re-checks provisioning_state under the
    # org-keyed advisory lock and then talks to Twilio, so a release that does
    # not contend for the same lock could delete the PN between that re-check
    # and attach_number (an opaque Twilio 404 → 500, and a Messaging Service
    # SID committed onto a tombstone). Transaction-scoped: released at the
    # commit below (or at rollback).
    await org_lock(db, number.organization_id)
    # Re-read under the lock: a concurrent release may have already
    # tombstoned the row after our caller loaded it, or the order reconciler
    # may have just activated a pending order (which sets provider_resource_id).
    await db.refresh(number)
    if number.provisioning_state == "released":
        return number
    if number.provisioning_state == "failed":
        # Dismiss: the hold was already refunded and no carrier number exists,
        # so there is nothing to release. Mark it released; no carrier call.
        if number.released_at is None:
            number.released_at = datetime.now(timezone.utc)
            await db.commit()
        return number
    if number.provisioning_state == "pending":
        raise HTTPException(
            status_code=409,
            detail="Number order is still pending; refresh its status before releasing",
        )
    if number.provider not in CARRIERS:
        raise HTTPException(
            status_code=409,
            detail=f"{number.provider} numbers cannot be released through the API yet",
        )
    if lk is not None and number.inbound_registered_at is not None:
        try:
            await inbound_routing.unregister(db, lk, number)
        except inbound_routing.InboundRoutingError:
            logger.warning(
                "number %s: inbound unregister failed before release (%s)",
                number.id,
                number.e164,
                exc_info=True,
            )
            number.inbound_registered_at = None
    number.voice_agent_id = None
    number.sms_agent_id = None
    try:
        await carrier(number.provider).release(number.provider_resource_id)
    except CarrierNotConfigured as exc:
        # Carrier not configured: an operator problem, not a server fault.
        # The customer reads this: never the carrier's name or an env var.
        logger.error("%s release failed: %s", number.provider, exc)
        raise HTTPException(
            status_code=503, detail="the carrier is not configured"
        ) from exc
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
    responses={
        404: {"description": "The number does not exist for this organization."},
        409: {
            "description": (
                "The order is still pending (refresh its status first) or the "
                "number's carrier is unsupported. A failed order is dismissed "
                "with 204 and no carrier call."
            ),
        },
        503: {"description": "The number's carrier is not configured on this server."},
    },
)
async def release_number(
    number_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)],
) -> None:
    """Release a dedicated number. The monthly fee stops accruing after the
    release month; months already accrued stay owed (the rater bills late,
    never forgives). A number that answered calls is taken off the inbound
    trunk first."""
    number = await _get_org_number_or_404(db, number_id, principal.organization_id)
    # Best-effort pre-check so an idempotent re-DELETE doesn't append a
    # second audit entry (audit is a safety net, not a correctness gate).
    was_released = (
        number.provisioning_state == "released" or number.released_at is not None
    )
    await release_org_number(db, number, lk)
    if not was_released:
        actor_user_id, actor_kind = actor_of(principal)
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="number.release",
            resource_type="phone_number",
            resource_id=number.id,
            payload={"e164": number.e164},
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
        )


_ROUTING_RESPONSES: dict = {
    404: {
        "description": "The number or the agent does not exist for this organization."
    },
    422: {"description": "The number lacks the capability the agent would answer on."},
    502: {
        "description": "The carrier or LiveKit refused to register the number for inbound."
    },
    503: {
        "description": "This server is not configured for inbound calls on the number's carrier."
    },
}


async def _load_org_agent_or_404(
    db: AsyncSession, agent_id: UUID, organization_id: UUID
) -> Agent:
    agent = (
        await db.execute(
            select(Agent).where(
                Agent.id == agent_id, Agent.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="agent not found"
        )
    return agent


def _routing_http_error(exc: inbound_routing.InboundRoutingError) -> HTTPException:
    # The customer reads this: never the carrier's name or an env var. ``exc``
    # names both (it is logged by the caller).
    if exc.config:
        return HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="inbound calls are not configured for this carrier",
        )
    return HTTPException(
        status_code=http_status.HTTP_502_BAD_GATEWAY,
        detail=f"could not register the number for inbound calls (stage: {exc.stage})",
    )


@router.patch(
    "/{number_id}",
    response_model=PhoneNumberResponse,
    responses=_ROUTING_RESPONSES,
)
async def route_number(
    number_id: UUID,
    body: PhoneNumberRoutingUpdate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient, Depends(get_livekit)],
) -> PhoneNumberResponse:
    """Choose which agent answers this number.

    Setting ``voice_agent_id`` registers the number for inbound calls at the
    carrier and on Hail's LiveKit inbound trunk; ``null`` unregisters it, and
    calls ring out again. ``sms_agent_id`` only needs the sms capability;
    inbound texts already reach Hail. A field left out keeps its value.
    """
    # Same org lock release_org_number takes: a PATCH racing a release (or
    # another PATCH) must not commit an agent onto a released row or leave a
    # registered number with no agent.
    await org_lock(db, principal.organization_id)
    number = await _get_org_number_or_404(db, number_id, principal.organization_id)
    await db.refresh(number)
    _reject_if_released(number)
    fields = body.model_fields_set
    if "voice_agent_id" in fields and body.voice_agent_id is not None:
        if "voice" not in number.capabilities:
            raise unprocessable(
                "this number has no voice capability", loc=["body", "voice_agent_id"]
            )
        await _load_org_agent_or_404(db, body.voice_agent_id, principal.organization_id)
    if "sms_agent_id" in fields and body.sms_agent_id is not None:
        if "sms" not in number.capabilities:
            raise unprocessable(
                "this number has no sms capability", loc=["body", "sms_agent_id"]
            )
        await _load_org_agent_or_404(db, body.sms_agent_id, principal.organization_id)

    try:
        if "voice_agent_id" in fields:
            if body.voice_agent_id is None:
                await inbound_routing.unregister(db, lk, number)
            else:
                await inbound_routing.register(db, lk, number)
            number.voice_agent_id = body.voice_agent_id
        if "sms_agent_id" in fields:
            number.sms_agent_id = body.sms_agent_id
    except inbound_routing.InboundRoutingError as exc:
        # Rollback expires the row; log the path id, not the ORM attribute.
        await db.rollback()
        logger.warning(
            "number %s inbound routing failed stage=%s",
            number_id,
            exc.stage,
            exc_info=True,
        )
        raise _routing_http_error(exc) from exc
    number.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(number)
    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="number.route",
        resource_type="phone_number",
        resource_id=number.id,
        payload={
            "e164": number.e164,
            "voice_agent_id": (
                str(number.voice_agent_id) if number.voice_agent_id else None
            ),
            "sms_agent_id": str(number.sms_agent_id) if number.sms_agent_id else None,
        },
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return PhoneNumberResponse.model_validate(number)


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
    organization. Read-only: a pending order is advanced by the background
    reconciler, never by this request.
    """
    number = await _get_org_number_or_404(db, number_id, principal.organization_id)
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
    shared pool numbers used for outbound calls never appear here. Failed
    orders that were dismissed (DELETE) are not listed.
    """
    stmt = select(PhoneNumber).where(
        PhoneNumber.organization_id == principal.organization_id,
        PhoneNumber.is_pool.is_(False),
        # A dismissed failed order is hidden; normally released numbers stay
        # listed as tombstones.
        or_(
            PhoneNumber.provisioning_state != "failed",
            PhoneNumber.released_at.is_(None),
        ),
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
    responses={
        404: {"description": "The number does not exist for this organization."},
        503: {
            "description": "SMS is not configured for this number's carrier on this server."
        },
    },
)
async def enable_sms(
    number_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
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
    # authoritative rather than a race window. Purchases, order reconciliation
    # and the monthly-fee rater take the same lock (see ``org_lock``).
    await org_lock(db, principal.organization_id)
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

    try:
        provider = sms_route(number.provider)
    except ValueError as exc:
        # Carrier not configured for SMS: an operator problem, not a server fault.
        # The customer reads this: never the carrier's name or an env var.
        logger.error("%s SMS route unavailable: %s", number.provider, exc)
        raise HTTPException(
            status_code=503, detail="SMS is not available on this number's carrier"
        ) from exc
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


class NumberQuotesResponse(BaseModel):
    offers: list[CarrierOffer] = Field(
        description="Live carrier offers ordered by readiness, remaining verification effort, monthly price and setup price. On a tie the first carrier listed wins."
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
    cost. On a tie the first carrier listed wins. Blocked offers sort by verification effort. SMS capability does not waive messaging registration requirements.
    """

    # Only number types the requested carrier's catalog lists (any carrier
    # for 'auto') can be bought, so only those are searched.
    if body.number_type:
        catalog_capabilities(body.country_code, body.number_type, body.provider)
        kinds = [body.number_type]
    else:
        kinds = [
            k
            for k in ("local", "mobile", "national", "toll_free")
            if telephony_catalog.capabilities(body.country_code, k, body.provider)
            is not None
        ]
        if not kinds:
            raise unprocessable(
                f"we don't offer numbers in {body.country_code} yet",
                loc=["body", "country_code"],
            )
    # Carrier discovery takes seconds. End the transaction the auth lookup
    # opened so this request does not hold a pooled connection while it waits.
    await db.commit()
    providers = PROVIDERS if body.provider == "auto" else [body.provider]
    batches = await asyncio.gather(
        *(
            discover_offers(
                principal.organization_id,
                body.country_code,
                kind,
                body.capabilities,
                # Ask only the carriers whose own catalog lists this kind:
                # the purchase checks the quoted carrier's catalog, and a
                # quote must never show an offer the purchase would refuse.
                providers=[
                    p
                    for p in providers
                    if telephony_catalog.capabilities(body.country_code, kind, p)
                    is not None
                ],
            )
            for kind in kinds
        )
    )
    # An explicit carrier restricts the offers themselves, not only the
    # recommendation: a client that buys "the cheapest offer" must never be
    # handed a quote its own provider setting then rejects with a 422.
    offers = rank_offers(
        [offer for batch, _ in batches for offer in batch], body.provider
    )
    unavailable = sorted({p for _, failures in batches for p in failures})
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    for offer in offers:
        row = NumberOffer(
            id=uuid4(),
            organization_id=principal.organization_id,
            # The purchase re-check must search with what was asked for, not
            # with every channel the number happens to support.
            offer={
                **offer.model_dump(mode="json"),
                "requested_capabilities": body.capabilities,
            },
            expires_at=expires,
        )
        db.add(row)
        offer.quote_id = row.id
    await db.commit()
    recommended = next((o for o in offers if o.readiness == "ready"), None)
    return {
        "offers": offers,
        "recommended_quote_id": recommended.quote_id if recommended else None,
        "unavailable_providers": unavailable,
        "expires_at": expires,
    }


__all__ = ["release_org_number", "router"]
