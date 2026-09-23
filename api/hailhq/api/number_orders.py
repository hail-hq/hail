"""Durable, credit-reserved carrier orders. An ambiguous POST is never retried."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from hailhq.api.funds import BILLING_URL
from hailhq.core.billing import get_balance_cents
from hailhq.core.config import settings
from hailhq.core.db import session_scope
from hailhq.core.models import AccountCredit, NumberOffer, PhoneNumber
from hailhq.core.number_offers import CarrierOffer, discover_offers
from hailhq.core.providers.telnyx import TelnyxClient
from hailhq.core.providers.voice import CarrierRequestError
from hailhq.core.providers.voice.twilio import (
    find_ordered_number,
    purchase_ordered_number,
)
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Missing inventory/order lookups cannot prove a paid POST was rejected.
# Escalate old ambiguous orders while retaining the reservation and number claim.
UNFOUND_ORDER_REVIEW_AFTER = timedelta(hours=1)

ORDER_POLL_INTERVAL = timedelta(seconds=15)

NUMBER_TAKEN_DETAIL = "This number is already held or has a pending order"


async def org_lock(db, org):
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": str(org)},
    )


def credit(number, amount, ref, source):
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
):
    """Caller holds org lock. Move reservation to month fee, or refund once."""
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
                    f"monthly_fee:{number.organization_id}:{number.id}:dedicated_number:{now:%Y-%m}",
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
        api = TelnyxClient()
        order_id = number.provisioning_metadata.get("order_id")
        if order_id:
            order = (await api.request("GET", f"/number_orders/{UUID(order_id)}"))[
                "data"
            ]
        else:
            # A crash/timeout after POST may lose the response. Recover by our
            # durable reference.
            result = await api.request(
                "GET",
                "/number_orders",
                params={
                    "filter[customer_reference]": str(number.id),
                    "page[size]": 100,
                },
            )
            matches = [
                o
                for o in result["data"]
                if o.get("customer_reference") == str(number.id)
            ]
            if not matches:
                return "missing", None, None
            if len(matches) > 1:
                return "pending", None, None
            order = matches[0]
        order_id = order.get("id") or order_id
        if order["status"] == "failure":
            return "failed", None, order_id
        if order["status"] != "success" or not order.get("requirements_met"):
            return "pending", None, order_id
        owned = (
            await api.request(
                "GET",
                "/phone_numbers",
                params={
                    "filter[phone_number]": number.e164.lstrip("+"),
                    "page[size]": 100,
                },
            )
        )["data"]
        match = next(
            (
                n
                for n in owned
                if n["phone_number"] == number.e164 and n.get("status") == "active"
            ),
            None,
        )
        # Order-phone IDs and owned-phone IDs are distinct Telnyx resources.
        return (
            ("active", match["id"], order["id"])
            if match
            else (
                "pending",
                None,
                order["id"],
            )
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
    state, resource_id, order_id = await carrier_outcome(number)
    await org_lock(db, org)
    await db.refresh(number)
    if number.provisioning_state != "pending":
        await db.commit()
        return
    if order_id and number.provisioning_metadata.get("order_id") != order_id:
        number.provisioning_metadata = {
            **number.provisioning_metadata,
            "order_id": order_id,
        }
    unfound = (
        state == "missing"
        and datetime.now(timezone.utc) - number.created_at > UNFOUND_ORDER_REVIEW_AFTER
    )
    if state == "active":
        await finish_order(db, number, resource_id=resource_id)
    elif state == "failed":
        await finish_order(db, number, resource_id=None, failed=True)
    elif unfound:
        number.provisioning_metadata = {
            **number.provisioning_metadata,
            "needs_review": True,
        }
        logger.warning(
            "Carrier order remains unconfirmed; review required: number=%s", number.id
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
    kind: str,
    provider: str,
    billed: bool,
) -> PhoneNumber:
    await org_lock(db, org)
    row = await load_quote(db, org, quote_id)
    offer = CarrierOffer.model_validate(row.offer)
    if (
        offer.country_code != country
        or offer.number_type != kind
        or provider not in ("auto", offer.provider)
    ):
        raise HTTPException(
            status_code=422,
            detail="Quote does not match the selected country, type or provider",
        )
    if row.number_id:
        return await db.get(PhoneNumber, row.number_id)
    if row.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=409, detail="Quote expired; refresh number offers"
        )
    if offer.readiness != "ready":
        raise HTTPException(
            status_code=422,
            detail="Complete this organization's regulatory verification first",
        )
    # Carrier discovery is slow: release the org lock and the quote row lock
    # first so other billing and number writes for this organization proceed.
    await db.commit()
    # Check both carrier price and regulatory readiness again before committing
    # money. The client cannot submit price, bundle ids, or a different number.
    live, _ = await discover_offers(
        org, country, kind, offer.capabilities, e164=offer.e164
    )
    fresh = next(
        (o for o in live if o.provider == offer.provider and o.e164 == offer.e164), None
    )
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
    await org_lock(db, org)
    try:
        if offer.provider == "telnyx":
            payload = {
                "phone_numbers": [{"phone_number": offer.e164}],
                "customer_reference": str(number.id),
            }
            if "voice" in offer.capabilities:
                payload["connection_id"] = settings.telnyx_connection_id
            if offer.verification_id:
                payload["phone_numbers"][0][
                    "requirement_group_id"
                ] = offer.verification_id
            order = (
                await TelnyxClient().request("POST", "/number_orders", json=payload)
            )["data"]
            number.provisioning_metadata = {
                **number.provisioning_metadata,
                "order_id": order["id"],
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
            await finish_order(db, number, resource_id=None, failed=True)
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
        logger.warning("Carrier order requires reconciliation: number=%s", number.id)
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
            logger.warning("Carrier order status unavailable: number=%s", number.id)
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
                "Number order reconciliation failed: number=%s; will retry", number_id
            )
