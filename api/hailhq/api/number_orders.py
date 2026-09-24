"""Durable, credit-reserved carrier orders. An ambiguous POST is never retried."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from hailhq.api.deps import Principal
from hailhq.api.errors import unprocessable
from hailhq.api.funds import BILLING_URL, require_funds
from hailhq.core import telephony_catalog
from hailhq.core.billing import get_balance_cents, monthly_fee_ref
from hailhq.core.db import session_scope
from hailhq.core.models import AccountCredit, NumberOffer, PhoneNumber
from hailhq.core.number_offers import CarrierOffer, discover_offers
from hailhq.core.providers.voice import (
    CarrierRequestError,
    NumberNotProvisionable,
    VoiceProvider,
)
from hailhq.core.providers.voice.telnyx import (
    place_number_order,
    telnyx_order_outcome,
)
from hailhq.core.providers.voice.twilio import (
    find_ordered_number,
    purchase_ordered_number,
)
from hailhq.core.schemas import NumberAcquireRequest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# An order the carrier still reports as pending, or has no record of, after
# this long is abnormal: an offer must be "ready" (regulatory requirements met)
# before it can be bought.
# The reconciler then marks it failed and refunds the hold, so the number can be
# released or re-ordered. If the carrier completes it later, an operator must
# release the number at the carrier (the log line names the order).
PENDING_ORDER_TIMEOUT = timedelta(hours=2)

ORDER_POLL_INTERVAL = timedelta(seconds=15)

# Quotes that expired unused are deleted after this long. Consumed quotes stay:
# they answer replays of the purchase that used them.
QUOTE_RETENTION = timedelta(hours=1)

NUMBER_TAKEN_DETAIL = "This number is already held or has a pending order"


class RetryableError(HTTPException):
    """A 503 raised before any charge. The route does not cache it under the
    idempotency key, so a same-key retry can succeed."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=503, detail=detail)


def catalog_capabilities(country: str, kind: str) -> dict[str, Any]:
    """422 unless the telephony catalog lists this country and number type."""
    caps = telephony_catalog.capabilities(country, kind)
    if caps is None:
        raise unprocessable(
            f"we don't offer a {kind} number in {country} yet",
            loc=["body", "number_type"],
        )
    return caps


async def org_lock(db: AsyncSession, org: UUID) -> None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": str(org)},
    )


def credit(number: PhoneNumber, amount: int, ref: str, source: str) -> AccountCredit:
    return AccountCredit(
        organization_id=number.organization_id,
        kind="credit" if amount > 0 else "debit",
        channel="voice" if "voice" in number.capabilities else "sms",
        amount_cents=amount,
        qty=1,
        ref=ref,
        source=source,
    )


async def finish_order(
    db: AsyncSession,
    number: PhoneNumber,
    *,
    resource_id: str | None,
    failed: bool = False,
    reason: str | None = None,
):
    """Caller holds org lock. Move reservation to month fee, or refund once.

    ``reason`` is stored for a failed order and shown to the buyer; it must
    never contain carrier payloads."""
    if number.provisioning_state != "pending":
        return
    meta = dict(number.provisioning_metadata)
    offer = CarrierOffer.model_validate(meta["offer"])
    now = datetime.now(timezone.utc)
    if meta["billed"]:
        db.add(
            credit(
                number,
                offer.monthly_cents + offer.setup_cents,
                f"number_reservation_return:{number.id}",
                "number_reservation",
            )
        )
        if not failed:
            db.add(
                credit(
                    number,
                    -offer.monthly_cents,
                    monthly_fee_ref(number.organization_id, number.id, now),
                    "monthly_fee",
                )
            )
            if offer.setup_cents:
                db.add(
                    credit(
                        number,
                        -offer.setup_cents,
                        f"number_setup:{number.id}",
                        "number_setup",
                    )
                )
    number.provisioning_state = "failed" if failed else "active"
    if not failed:
        number.provider_resource_id = resource_id
        number.acquired_at = now
    meta.pop("needs_review", None)
    meta["order_state"] = "failed" if failed else "complete"
    if failed:
        meta["failure_reason"] = reason or "the carrier did not complete the order"
    number.provisioning_metadata = meta
    await db.commit()


async def carrier_outcome(
    number: PhoneNumber,
) -> tuple[Literal["active", "failed", "pending", "missing"], str | None, str | None]:
    """Ask the carrier what happened to this order. Holds no DB lock.

    Returns (state, owned resource id, Telnyx order id). ``missing`` means the
    carrier has no record of the order; it is never treated as permission to
    submit another paid purchase.
    """
    if number.provider == "telnyx":
        return await telnyx_order_outcome(
            number.e164, number.id, number.provisioning_metadata.get("order_id")
        )
    sid = await find_ordered_number(number.e164, number.id)
    return ("active", sid, None) if sid else ("missing", None, None)


async def reconcile_order(
    db: AsyncSession, number: PhoneNumber, *, force: bool = False
):
    if number.provisioning_state != "pending":
        return
    org = number.organization_id
    # Claim a poll under the org lock, then release it before carrier I/O.
    await org_lock(db, org)
    await db.refresh(number)
    if number.provisioning_state != "pending":
        await db.commit()
        return
    now = datetime.now(timezone.utc)
    last_check = number.provisioning_metadata.get("last_checked_at")
    if (
        not force
        and last_check
        and now - datetime.fromisoformat(last_check) < ORDER_POLL_INTERVAL
    ):
        await db.commit()
        return
    number.provisioning_metadata = {
        **number.provisioning_metadata,
        "last_checked_at": now.isoformat(),
    }
    await db.commit()
    lookup_error: Exception | None = None
    try:
        state, resource_id, order_id = await carrier_outcome(number)
    except Exception as exc:
        # Keep retrying until the timeout; after it, fail the order below.
        lookup_error = exc
        state, resource_id, order_id = "pending", None, None
    await org_lock(db, org)
    await db.refresh(number)
    if number.provisioning_state != "pending":
        await db.commit()
        return
    if lookup_error is not None and (
        datetime.now(timezone.utc) - number.created_at <= PENDING_ORDER_TIMEOUT
    ):
        await db.commit()
        raise lookup_error
    if order_id and number.provisioning_metadata.get("order_id") != order_id:
        number.provisioning_metadata = {
            **number.provisioning_metadata,
            "order_id": order_id,
        }
    unfound = (
        state == "missing"
        and datetime.now(timezone.utc) - number.created_at > PENDING_ORDER_TIMEOUT
    )
    if state == "active":
        await finish_order(db, number, resource_id=resource_id)
    elif state == "failed":
        await finish_order(
            db,
            number,
            resource_id=None,
            failed=True,
            reason="the carrier reported the order as failed",
        )
    elif (
        state == "pending"
        and datetime.now(timezone.utc) - number.created_at > PENDING_ORDER_TIMEOUT
    ):
        if lookup_error is not None:
            logger.error(
                "Carrier order lookups keep failing after timeout; failing it and "
                "refunding. Flagged for operator review (release the number at the "
                "carrier if the order later completed): number=%s carrier_order=%s",
                number.id,
                number.provisioning_metadata.get("order_id"),
                exc_info=lookup_error,
            )
        else:
            logger.warning(
                "Carrier order still pending after timeout; failing it and refunding: "
                "number=%s carrier_order=%s",
                number.id,
                number.provisioning_metadata.get("order_id"),
            )
        await finish_order(
            db,
            number,
            resource_id=None,
            failed=True,
            reason="the carrier did not confirm the order in time",
        )
    elif unfound:
        logger.error(
            "Carrier has no record of the order after timeout; failing it and "
            "refunding. Flagged for operator review (release the number at the "
            "carrier if it was bought): number=%s carrier_order=%s",
            number.id,
            number.provisioning_metadata.get("order_id"),
        )
        await finish_order(
            db,
            number,
            resource_id=None,
            failed=True,
            reason="the carrier has no record of the order",
        )
    await db.commit()


async def load_quote(db: AsyncSession, org: UUID, quote_id: UUID) -> NumberOffer:
    """Caller holds org lock. Locks the quote row and re-reads it from the DB."""
    row = (
        await db.execute(
            select(NumberOffer)
            .where(NumberOffer.id == quote_id, NumberOffer.organization_id == org)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Number quote not found")
    return row


async def acquire_offer(
    db: AsyncSession,
    org: UUID,
    quote_id: UUID,
    *,
    country: str,
    kind: str | None,
    provider: str,
    billed: bool,
) -> PhoneNumber:
    """Buy the quoted number. ``kind`` is the number type the client sent, or
    None to take it from the quote; a different explicit type is a 422."""
    await org_lock(db, org)
    row = await load_quote(db, org, quote_id)
    offer = CarrierOffer.model_validate(row.offer)
    if (
        offer.country_code != country
        or (kind is not None and offer.number_type != kind)
        or provider not in ("auto", offer.provider)
    ):
        raise unprocessable(
            "Quote does not match the selected country, type or provider",
            loc=["body", "quote_id"],
        )
    kind = offer.number_type
    if row.number_id:
        return await db.get(PhoneNumber, row.number_id)
    catalog_capabilities(country, kind)
    if row.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=409, detail="Quote expired; refresh number offers"
        )
    if offer.readiness != "ready":
        raise unprocessable(
            "Complete this organization's regulatory verification first",
            loc=["body", "quote_id"],
        )
    # Carrier discovery is slow: release the org lock and the quote row lock
    # first so other billing and number writes for this organization proceed.
    await db.commit()
    # Check both carrier price and regulatory readiness again before committing
    # money. The client cannot submit price, bundle ids, or a different number.
    live, unavailable = await discover_offers(
        org,
        country,
        kind,
        row.offer.get("requested_capabilities") or offer.capabilities,
        e164=offer.e164,
    )
    fresh = next(
        (o for o in live if o.provider == offer.provider and o.e164 == offer.e164), None
    )
    if fresh is None and offer.provider in unavailable:
        # The carrier lookup failed; that says nothing about the quote. The
        # route does not cache this response, so a retry can succeed.
        raise RetryableError("Carrier lookup unavailable; try again shortly")
    if (
        fresh is None
        or fresh.readiness != "ready"
        or (fresh.monthly_cents, fresh.setup_cents, fresh.verification_id)
        != (offer.monthly_cents, offer.setup_cents, offer.verification_id)
    ):
        raise HTTPException(
            status_code=409,
            detail="Price, inventory or verification changed; refresh offers",
        )
    await org_lock(db, org)
    row = await load_quote(db, org, quote_id)
    if row.number_id:
        # A concurrent request consumed this quote while we were checking.
        return await db.get(PhoneNumber, row.number_id)
    total = offer.monthly_cents + offer.setup_cents
    if billed and await get_balance_cents(db, org) < total:
        raise HTTPException(
            status_code=402,
            detail=f"insufficient credits; setup and the first month cost ${total / 100:.2f}; top up at {BILLING_URL}",
        )
    existing = (
        await db.execute(
            select(PhoneNumber.id)
            .where(
                PhoneNumber.e164 == offer.e164,
                PhoneNumber.provisioning_state.not_in(("released", "failed")),
            )
            .limit(1)
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail=NUMBER_TAKEN_DETAIL)
    number = PhoneNumber(
        id=uuid4(),
        organization_id=org,
        e164=offer.e164,
        country_code=country,
        number_type=kind,
        capabilities=offer.capabilities,
        provider=offer.provider,
        provisioning_state="pending",
        is_pool=False,
        provisioning_metadata={
            "offer": offer.model_dump(mode="json"),
            "billed": billed,
            "monthly_cents": offer.monthly_cents,
            "currency": "USD",
            "order_state": "submitting",
        },
    )
    db.add(number)
    row.number_id = number.id
    if billed:
        db.add(
            credit(
                number, -total, f"number_reservation:{number.id}", "number_reservation"
            )
        )
    # Persist BEFORE the non-transactional carrier operation. A retry sees this
    # consumed quote and pending number even if the process dies mid-request.
    try:
        await db.commit()
    except IntegrityError:
        # The unique index is global; the org lock is not. Another organization
        # won the same number. Nothing was charged or ordered.
        await db.rollback()
        raise HTTPException(status_code=409, detail=NUMBER_TAKEN_DETAIL) from None
    # Held through the carrier call on purpose: finish_order and the metadata
    # write below act on this in-memory row and must not interleave with
    # reconcile_order. The cost is that same-organization writes wait for the
    # carrier (timeouts: Telnyx 20s, Twilio 10s).
    await org_lock(db, org)
    try:
        if offer.provider == "telnyx":
            order_id = await place_number_order(
                number.id, offer.e164, offer.verification_id, offer.capabilities
            )
            number.provisioning_metadata = {
                **number.provisioning_metadata,
                "order_id": order_id,
                "order_state": "pending",
            }
            await db.commit()
        else:
            resource_id = await purchase_ordered_number(
                offer.e164, number.id, offer.verification_id
            )
            await finish_order(db, number, resource_id=resource_id)
    except (httpx.HTTPStatusError, CarrierRequestError) as exc:
        status = (
            exc.response.status_code
            if isinstance(exc, httpx.HTTPStatusError)
            else exc.status
        )
        if 400 <= status < 500 and status not in (408, 409):
            await finish_order(
                db,
                number,
                resource_id=None,
                failed=True,
                reason=f"the carrier rejected the order (HTTP {status})",
            )
        else:
            # Unknown outcome: keep the reservation for reconciliation.
            await db.commit()
            logger.warning(
                "Carrier order outcome unknown: number=%s status=%s", number.id, status
            )
    except Exception:
        # Do not expose provider payloads or retry this order on another carrier.
        await db.rollback()
        await db.refresh(number)
        logger.warning(
            "Carrier order requires reconciliation: number=%s",
            number.id,
            exc_info=True,
        )
    # Status reads can fail after a successful POST. They must never enter the
    # submission rejection/refund handler above.
    if (
        number.provider == "telnyx"
        and number.provisioning_state == "pending"
        and number.provisioning_metadata.get("order_id")
    ):
        try:
            await reconcile_order(db, number)
        except Exception:
            await db.rollback()
            await db.refresh(number)
            logger.warning(
                "Carrier order status unavailable: number=%s", number.id, exc_info=True
            )
    return number


async def acquire_from_catalog(
    db: AsyncSession,
    org: UUID,
    *,
    country: str,
    kind: str,
    billed: bool,
    voice_provider: VoiceProvider,
) -> PhoneNumber:
    """Legacy Twilio purchase: the carrier picks the number, so its E.164 is
    unknown until the purchase returns. No hold can be posted first; the
    monthly fee is debited in the same commit that records the number."""
    caps = catalog_capabilities(country, kind)
    requested_caps = [c for c in ("voice", "sms") if caps[c]]
    if not requested_caps:
        # A catalog row with neither voice nor sms is schema-invalid, but the
        # runtime load doesn't schema-validate; an empty filter would let the
        # provider purchase an arbitrary number.
        raise unprocessable(
            f"the {kind} number in {country} has no usable capabilities",
            loc=["body", "number_type"],
        )
    price = telephony_catalog.price_usd_per_month(country, kind)
    amount_cents = (
        int((price * 100).quantize(1, rounding=ROUND_HALF_UP))
        if price is not None and price.is_finite() and price > 0
        else 0
    )
    if amount_cents <= 0:
        raise HTTPException(
            status_code=503, detail="number price unavailable; try again later"
        )
    if billed:
        # Serialize purchases and monthly debits for this organization.
        await org_lock(db, org)
        if await get_balance_cents(db, org) < amount_cents:
            raise HTTPException(
                status_code=402,
                detail=f"insufficient credits; this number costs ${price:.2f} per month; "
                f"top up at {BILLING_URL}",
            )
    try:
        acquired = await voice_provider.acquire_number(
            country_code=country, number_type=kind, capabilities=requested_caps
        )
    except LookupError as exc:
        # The carrier has no matching inventory right now.
        raise RetryableError(str(exc)) from exc
    except NumberNotProvisionable as exc:
        # Deterministic: it needs regulatory setup we don't have, so a retry
        # fails identically. The raw carrier reason is logged, not returned.
        logger.warning(
            "number not provisionable (%s %s): %s", country, kind, exc.detail
        )
        raise unprocessable(
            f"we can't provision a {kind} number in {country} yet — it needs "
            "regulatory verification we don't support",
            loc=["body", "number_type"],
        ) from exc

    number = PhoneNumber(
        organization_id=org,
        e164=acquired.e164,
        country_code=acquired.country_code,
        number_type=acquired.number_type,
        capabilities=acquired.capabilities,
        provider_resource_id=acquired.provider_resource_id,
        provisioning_state="active",
        is_pool=False,
    )
    number.acquired_at = datetime.now(timezone.utc)
    db.add(number)
    try:
        await db.flush()
        if billed:
            db.add(
                credit(
                    number,
                    -amount_cents,
                    monthly_fee_ref(org, number.id, number.acquired_at),
                    "monthly_fee",
                )
            )
        await db.commit()
    except Exception:
        await db.rollback()
        try:
            await voice_provider.release_number(acquired.provider_resource_id)
        except Exception:
            logger.exception(
                "failed to release number after failed purchase commit: %s",
                acquired.provider_resource_id,
            )
        raise
    return number


async def purchase_number(
    db: AsyncSession,
    principal: Principal,
    body: NumberAcquireRequest,
    voice_provider: VoiceProvider,
) -> PhoneNumber:
    """The one purchase entry point for POST /numbers.

    Raises HTTPException for every rejection; the caller caches it under the
    idempotency key unless it is a RetryableError.
    """
    org = principal.organization_id
    billed = principal.auth_kind != "shared"
    provider = body.provider or "auto"
    if body.quote_id is not None:
        number = await acquire_offer(
            db,
            org,
            body.quote_id,
            country=body.country_code,
            # An omitted type comes from the quote; only an explicit one can conflict.
            kind=body.number_type if "number_type" in body.model_fields_set else None,
            provider=provider,
            billed=billed,
        )
    elif body.provider in ("auto", "telnyx"):
        # Omitted provider without a quote is the legacy Twilio contract.
        raise unprocessable(
            "Request a live quote from POST /numbers/quotes, then pass its quote_id",
            loc=["body", "quote_id"],
        )
    else:
        # Same balance gate as the other paid create routes; runs before the
        # carrier purchase.
        await require_funds(db, principal)
        number = await acquire_from_catalog(
            db,
            org,
            country=body.country_code,
            kind=body.number_type,
            billed=billed,
            voice_provider=voice_provider,
        )
    if number.provisioning_state == "failed":
        # The hold was refunded; nothing is owed. Say so instead of a 201.
        reason = number.provisioning_metadata.get(
            "failure_reason", "the carrier did not complete the order"
        )
        raise HTTPException(
            status_code=409,
            detail=f"Number order failed and credits were refunded: {reason}",
        )
    if number.provisioning_state == "released":
        # A replayed quote whose number was released since. It is not a new purchase.
        raise HTTPException(
            status_code=409,
            detail="The number bought with this quote was released; request a new quote",
        )
    return number


async def reconcile_pending_orders():
    """Fresh session per number: one carrier/DB failure cannot stall the batch."""

    async with session_scope() as db:
        ids = (
            (
                await db.execute(
                    select(PhoneNumber.id)
                    .where(
                        PhoneNumber.provisioning_state == "pending",
                        PhoneNumber.provisioning_metadata.has_key("offer"),
                    )
                    .order_by(PhoneNumber.updated_at)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    for number_id in ids:
        try:
            async with session_scope() as db:
                number = await db.get(PhoneNumber, number_id)
                if number:
                    await reconcile_order(db, number)
                    number.updated_at = datetime.now(timezone.utc)
                    await db.commit()
        except Exception:
            logger.warning(
                "Number order reconciliation failed: number=%s; will retry",
                number_id,
                exc_info=True,
            )


async def purge_expired_quotes() -> int:
    """Delete quotes that expired without being used. Returns how many."""
    async with session_scope() as db:
        result = await db.execute(
            delete(NumberOffer).where(
                NumberOffer.number_id.is_(None),
                NumberOffer.expires_at < datetime.now(timezone.utc) - QUOTE_RETENTION,
            )
        )
        await db.commit()
        return result.rowcount or 0
