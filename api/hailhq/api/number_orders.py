"""Durable, credit-reserved carrier orders. An ambiguous POST is never retried."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from hailhq.api.audit import write_audit_log
from hailhq.api.deps import Principal
from hailhq.api.errors import unprocessable
from hailhq.api.funds import BILLING_URL
from hailhq.core import sms_setup, telephony_catalog
from hailhq.core.billing import get_balance_cents, monthly_fee_ref
from hailhq.core.carrier_routing import Outcome, carrier
from hailhq.core.db import org_lock, session_scope
from hailhq.core.models import (
    AccountCredit,
    CarrierVerification,
    NumberOffer,
    PhoneNumber,
)
from hailhq.core.number_offers import CarrierOffer, discover_offers
from hailhq.core.providers.voice import (
    CarrierPreOrderError,
    CarrierRequestError,
)
from hailhq.core.schemas import NumberAcquireRequest
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# A running carrier check blocks other checks of the same order for this
# long. Longer than the slowest carrier check (4 calls of up to 40 s each),
# so two runs never both file a registration for one number.
ORDER_CHECK_LEASE = timedelta(minutes=5)

# Quotes that expired unused are deleted after this long.
QUOTE_RETENTION = timedelta(hours=1)
# Consumed quotes answer replays of the purchase that used them (idempotency
# keys live 24h) and are deleted after this long.
CONSUMED_QUOTE_RETENTION = timedelta(days=7)

NUMBER_TAKEN_DETAIL = "This number is already held or has a pending order"


class RetryableError(HTTPException):
    """A 503 raised before any charge. The route does not cache it under the
    idempotency key, so a same-key retry can succeed."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=503, detail=detail)


def catalog_capabilities(
    country: str, kind: str, provider: str = "auto"
) -> dict[str, Any]:
    """422 unless the carrier's catalog (any carrier for 'auto') lists this
    country and number type."""
    caps = telephony_catalog.capabilities(country, kind, provider)
    if caps is None:
        raise unprocessable(
            f"we don't offer a {kind} number in {country} yet",
            loc=["body", "number_type"],
        )
    return caps


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
    keep_setup: bool = False,
    reason: str | None = None,
):
    """Caller holds org lock. Move reservation to month fee, or refund once.

    ``reason`` is stored for a failed order and shown to the buyer; it must
    never contain carrier payloads beyond the carrier's own human-readable
    rejection wording. ``keep_setup`` charges the setup fee even on failure:
    the carrier already billed it and does not refund (the number was
    bought, then its registration was rejected or never cleared)."""
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
        if offer.setup_cents and (not failed or keep_setup):
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
    meta["order_state"] = "failed" if failed else "complete"
    if failed:
        meta["failure_reason"] = reason or "the carrier did not complete the order"
    number.provisioning_metadata = meta
    await db.commit()
    if not failed and "sms" in number.capabilities:
        # The number is bought and billed; SMS setup is a separate, retried
        # step (assigning a texts agent or POST /enable-sms runs it again).
        # The commit above released the transaction-scoped org lock. Take it
        # again: two purchases must not both see "no service" and each
        # create one.
        try:
            await org_lock(db, number.organization_id)
            await db.refresh(number)
            await sms_setup.ensure_sms(db, number)
            await db.commit()
        except sms_setup.SmsSetupError as exc:
            await db.rollback()
            logger.warning(
                "SMS setup deferred for %s after purchase: %s", number.e164, exc.stage
            )


async def give_back(number: PhoneNumber, resource_id: str) -> None:
    """Release a bought number the order will not keep. Never raises. When
    the carrier call fails, the resource id is kept on the row as
    ``unreleased_resource_id``: a failed order stores no resource id, and
    the number renews at the carrier until ``retry_unreleased_numbers`` gets
    it released. Saved by the caller's ``finish_order``."""
    try:
        await carrier(number.provider).release(resource_id)
    except Exception:
        logger.exception(
            "Could not release carrier number; will retry: number=%s resource=%s",
            number.id,
            resource_id,
        )
        number.provisioning_metadata = {
            **number.provisioning_metadata,
            "unreleased_resource_id": resource_id,
        }


async def rejected_registration(
    db: AsyncSession, number: PhoneNumber, resource_id: str | None
) -> str:
    """The carrier refused the end-user registration of a bought number.
    Releases the number, takes the approval back and marks the Hail
    verification rejected. Returns the reason shown to the buyer.

    Caller holds the org lock and fails the order. The carrier calls run
    under that lock on purpose: two concurrent reconciles must never both
    see "pending" and both release the same number."""
    org = number.organization_id
    if resource_id:
        await give_back(number, resource_id)
    # Take the approval back, so the next quote asks for new papers
    # instead of filing the same rejected ones again.
    reason = None
    revoke = carrier(number.provider).revoke_registration
    if revoke is not None:
        try:
            reason = await revoke(
                CarrierOffer.model_validate(number.provisioning_metadata["offer"]), org
            )
        except Exception:
            logger.exception(
                "Could not revoke rejected carrier registration; take the "
                "approval back by hand: number=%s",
                number.id,
            )
    reason = reason or "the carrier rejected the end-user registration"
    verification = (
        await db.execute(
            select(CarrierVerification).where(
                CarrierVerification.organization_id == org,
                CarrierVerification.provider == number.provider,
                CarrierVerification.country_code == number.country_code,
                CarrierVerification.number_type == number.number_type,
                CarrierVerification.state == "approved",
            )
        )
    ).scalar_one_or_none()
    if verification is not None:
        verification.state = "rejected"
        verification.rejection_reason = reason
        verification.updated_at = datetime.now(timezone.utc)
        # Same audit trail as every other system-driven rejection
        # (see _reject_row in routes/verifications.py).
        await write_audit_log(
            org,
            None,
            "verification.reject",
            "carrier_verification",
            verification.id,
            {"rejected_by": None, "reason": reason},
            actor_user_id=None,
            actor_kind="system",
        )
    return reason


async def carrier_outcome(number: PhoneNumber) -> Outcome:
    """Ask the carrier what happened to this order. Holds no DB lock.

    Returns (state, owned resource id, carrier order id). ``missing`` means
    the carrier has no record of the order; it is never treated as
    permission to submit another paid purchase. ``rejected_registration``
    means the number exists but the end-user registration failed.
    """
    meta = number.provisioning_metadata
    return await carrier(number.provider).order_outcome(
        number.e164,
        number.id,
        meta.get("order_id"),
        CarrierOffer.model_validate(meta["offer"]),
    )


async def reconcile_order(
    db: AsyncSession, number: PhoneNumber, *, force: bool = False
):
    if number.provisioning_state != "pending":
        return
    org = number.organization_id
    timeout = carrier(number.provider).pending_timeout
    poll_interval = carrier(number.provider).poll_interval
    # Claim a poll under the org lock, then release it before carrier I/O.
    await org_lock(db, org)
    await db.refresh(number)
    if number.provisioning_state != "pending":
        await db.commit()
        return
    now = datetime.now(timezone.utc)
    last_check = number.provisioning_metadata.get("last_checked_at")
    running = number.provisioning_metadata.get("check_started_at")
    if not force and (
        (last_check and now - datetime.fromisoformat(last_check) < poll_interval)
        or (running and now - datetime.fromisoformat(running) < ORDER_CHECK_LEASE)
    ):
        await db.commit()
        return
    number.provisioning_metadata = {
        **number.provisioning_metadata,
        "last_checked_at": now.isoformat(),
        "check_started_at": now.isoformat(),
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
    # The check answered: the next run may start.
    number.provisioning_metadata = {
        k: v for k, v in number.provisioning_metadata.items() if k != "check_started_at"
    }
    if lookup_error is not None and (
        datetime.now(timezone.utc) - number.created_at <= timeout
    ):
        await db.commit()
        raise lookup_error
    if order_id and number.provisioning_metadata.get("order_id") != order_id:
        number.provisioning_metadata = {
            **number.provisioning_metadata,
            "order_id": order_id,
        }
    unfound = (
        state == "missing" and datetime.now(timezone.utc) - number.created_at > timeout
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
    elif state == "rejected_registration":
        # The number exists at the carrier but the end-user registration was
        # refused. Stop its renewal and give the monthly fee back; the setup
        # fee stays (the carrier billed it and does not refund).
        # The carrier calls in here run under the org lock on purpose (see
        # rejected_registration).
        reason = await rejected_registration(db, number, resource_id)
        await finish_order(
            db,
            number,
            resource_id=None,
            failed=True,
            keep_setup=True,
            reason=reason,
        )
    elif (
        state == "pending" and datetime.now(timezone.utc) - number.created_at > timeout
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
        if resource_id:
            # The number was bought but its registration never cleared.
            # Release it; the setup fee stays (the carrier billed it and
            # does not refund).
            await give_back(number, resource_id)
            await finish_order(
                db,
                number,
                resource_id=None,
                failed=True,
                keep_setup=True,
                reason="the carrier did not approve the registration in time",
            )
        else:
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
    catalog_capabilities(country, kind, offer.provider)
    if row.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=409, detail="Quote expired; refresh number offers"
        )
    if offer.readiness != "ready":
        raise unprocessable(
            "Complete this organization's regulatory verification first",
            loc=["body", "quote_id"],
        )
    total = offer.monthly_cents + offer.setup_cents
    no_funds = HTTPException(
        status_code=402,
        detail=f"insufficient credits; setup and the first month cost ${total / 100:.2f}; top up at {BILLING_URL}",
    )
    # Cheap check first so an empty balance never costs a carrier round-trip.
    # Re-checked under the lock below, after the slow discovery.
    if billed and await get_balance_cents(db, org) < total:
        raise no_funds
    # Carrier discovery is slow: release the org lock and the quote row lock
    # first so other billing and number writes for this organization proceed.
    await db.commit()
    # Check both carrier price and regulatory readiness again before committing
    # money. The client cannot submit price, bundle ids, or a different number.
    # Only the quoted carrier can match, so only it is asked.
    live, unavailable = await discover_offers(
        org,
        country,
        kind,
        row.offer.get("requested_capabilities") or offer.capabilities,
        e164=offer.e164,
        providers=[offer.provider],
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
    if billed and await get_balance_cents(db, org) < total:
        raise no_funds
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
    # carrier (its HTTP timeout, 10 to 20 s).
    await org_lock(db, org)
    try:
        placed = await carrier(offer.provider).place_order(number.id, offer)
        if carrier(offer.provider).async_orders:
            number.provisioning_metadata = {
                **number.provisioning_metadata,
                "order_id": placed,
                "order_state": "pending",
            }
            await db.commit()
        else:
            await finish_order(db, number, resource_id=placed)
    except CarrierPreOrderError as exc:
        # No order was sent to the carrier: nothing to reconcile.
        await finish_order(
            db,
            number,
            resource_id=None,
            failed=True,
            reason=(
                "the number is no longer available"
                if exc.status == 410
                else "the carrier could not be reached; nothing was ordered"
            ),
        )
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
        carrier(number.provider).async_orders
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


async def purchase_number(
    db: AsyncSession, principal: Principal, body: NumberAcquireRequest
) -> PhoneNumber:
    """The one purchase entry point for POST /numbers: it buys a quote.

    Raises HTTPException for every rejection; the caller caches it under the
    idempotency key unless it is a RetryableError.
    """
    number = await acquire_offer(
        db,
        principal.organization_id,
        body.quote_id,
        country=body.country_code,
        # An omitted type comes from the quote; only an explicit one can conflict.
        kind=body.number_type if "number_type" in body.model_fields_set else None,
        provider=body.provider,
        billed=principal.auth_kind != "shared",
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


async def retry_release(db: AsyncSession, number: PhoneNumber) -> None:
    """Release a number whose release failed when its order was failed.
    Carrier errors propagate and the id stays for the next try. The carrier
    call runs under the org lock, like the first release, so two runs
    never act on the same row."""
    await org_lock(db, number.organization_id)
    await db.refresh(number)
    resource_id = number.provisioning_metadata.get("unreleased_resource_id")
    if resource_id:
        await carrier(number.provider).release(resource_id)
        number.provisioning_metadata = {
            k: v
            for k, v in number.provisioning_metadata.items()
            if k != "unreleased_resource_id"
        }
    await db.commit()


async def retry_unreleased_numbers():
    """Fresh session per number, like ``reconcile_pending_orders``. Oldest
    ``updated_at`` first; a row that failed again goes to the back."""
    async with session_scope() as db:
        ids = (
            (
                await db.execute(
                    select(PhoneNumber.id)
                    .where(
                        PhoneNumber.provisioning_metadata.has_key(
                            "unreleased_resource_id"
                        )
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
                    await retry_release(db, number)
        except Exception:
            logger.warning(
                "Carrier release retry failed: number=%s; will retry",
                number_id,
                exc_info=True,
            )
            # Move the row to the back, so rows beyond the limit get a turn.
            async with session_scope() as db:
                await db.execute(
                    update(PhoneNumber)
                    .where(PhoneNumber.id == number_id)
                    .values(updated_at=datetime.now(timezone.utc))
                )
                await db.commit()


async def purge_expired_quotes() -> int:
    """Delete stale quotes: unused ones after QUOTE_RETENTION, consumed ones
    after CONSUMED_QUOTE_RETENTION. Returns how many."""
    now = datetime.now(timezone.utc)
    async with session_scope() as db:
        result = await db.execute(
            delete(NumberOffer).where(
                or_(
                    and_(
                        NumberOffer.number_id.is_(None),
                        NumberOffer.expires_at < now - QUOTE_RETENTION,
                    ),
                    and_(
                        NumberOffer.number_id.is_not(None),
                        NumberOffer.expires_at < now - CONSUMED_QUOTE_RETENTION,
                    ),
                )
            )
        )
        await db.commit()
        return result.rowcount or 0


_sms_services_refreshed = False


async def sync_sms_setup() -> None:
    """Keep SMS set up without anyone clicking: retry numbers whose setup
    failed (no messaging service yet), and once per process set every
    existing service's own settings again, so a service made before its
    inbound webhook existed, or after HAIL_API_URL changed, still delivers.
    A fresh session and the org lock per number: one carrier failure cannot
    stall the rest."""
    global _sms_services_refreshed
    async with session_scope() as db:
        pending = (
            (
                await db.execute(
                    select(PhoneNumber.id)
                    .where(
                        PhoneNumber.provisioning_state == "active",
                        PhoneNumber.messaging_service_sid.is_(None),
                        PhoneNumber.capabilities.any("sms"),
                    )
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        refresh: list = []
        if not _sms_services_refreshed:
            refresh = list(
                (
                    await db.execute(
                        select(PhoneNumber.id)
                        .where(
                            PhoneNumber.provisioning_state == "active",
                            PhoneNumber.messaging_service_sid.is_not(None),
                        )
                        .distinct(PhoneNumber.messaging_service_sid)
                        .order_by(PhoneNumber.messaging_service_sid)
                    )
                )
                .scalars()
                .all()
            )
    all_ok = True
    for number_id in [*pending, *refresh]:
        try:
            async with session_scope() as db:
                number = await db.get(PhoneNumber, number_id)
                if number is None:
                    continue
                await org_lock(db, number.organization_id)
                await db.refresh(number)
                if number.provisioning_state != "active":
                    continue
                await sms_setup.ensure_sms(db, number)
                await db.commit()
        except sms_setup.SmsSetupError as exc:
            all_ok = False
            logger.warning("SMS setup retry failed (%s): %s", number_id, exc.stage)
        except Exception:
            all_ok = False
            logger.exception("SMS setup retry crashed for %s", number_id)
    if refresh and all_ok:
        _sms_services_refreshed = True
