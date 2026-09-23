"""Durable, credit-reserved carrier orders. An ambiguous POST is never retried."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from hailhq.core.billing import get_balance_cents
from hailhq.core.config import settings
from hailhq.core.models import AccountCredit, NumberOffer, PhoneNumber
from hailhq.core.number_offers import CarrierOffer, discover_offers
from hailhq.core.providers.telnyx import TelnyxClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client as TwilioClient

logger = logging.getLogger(__name__)


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
    meta["order_state"] = "failed" if failed else "complete"
    number.provisioning_metadata = meta
    await db.commit()


async def reconcile_order(db: AsyncSession, number: PhoneNumber):
    if number.provisioning_state != "pending":
        return
    await org_lock(db, number.organization_id)
    await db.refresh(number)
    if number.provisioning_state != "pending":
        return
    meta = dict(number.provisioning_metadata)
    if number.provider == "telnyx":
        api = TelnyxClient()
        order_id = meta.get("order_id")
        if order_id:
            order = (await api.request("GET", f"/number_orders/{UUID(order_id)}"))[
                "data"
            ]
        else:
            # A crash/timeout after POST may lose the response. Recover by our
            # durable reference; no match does NOT authorize another paid POST.
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
            if len(matches) != 1:
                return
            order = matches[0]
            meta["order_id"] = order["id"]
            number.provisioning_metadata = meta
        if order["status"] == "failure":
            await finish_order(db, number, resource_id=None, failed=True)
            return
        if order["status"] != "success" or not order.get("requirements_met"):
            await db.commit()
            return
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
        if match:
            # Order-phone IDs and owned-phone IDs are distinct Telnyx resources.
            await finish_order(db, number, resource_id=match["id"])
        else:
            await db.commit()
    elif number.provider == "twilio":

        def lookup():
            api = TwilioClient(
                settings.twilio_account_sid,
                settings.twilio_auth_token,
                http_client=TwilioHttpClient(timeout=10, max_retries=0),
            )
            return api.incoming_phone_numbers.list(phone_number=number.e164, limit=10)

        found = await asyncio.to_thread(lookup)
        match = next(
            (
                n
                for n in found
                if n.phone_number == number.e164
                and n.friendly_name == f"hail-order-{number.id}"
            ),
            None,
        )
        if match:
            await finish_order(db, number, resource_id=match.sid)


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
    row = (
        await db.execute(
            select(NumberOffer)
            .where(NumberOffer.id == quote_id, NumberOffer.organization_id == org)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Number quote not found")
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
    total = offer.monthly_cents + offer.setup_cents
    if billed and await get_balance_cents(db, org) < total:
        raise HTTPException(
            status_code=402,
            detail="Insufficient credits for setup and the first month; top up at https://hail.so/console/billing#topup",
        )
    existing = (
        await db.execute(
            select(PhoneNumber).where(
                PhoneNumber.e164 == offer.e164,
                PhoneNumber.provisioning_state != "released",
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409, detail="This number is already held or has a pending order"
        )
    number = PhoneNumber(
        id=uuid4(),
        organization_id=org,
        e164=offer.e164,
        country_code=country,
        number_type=kind,
        capabilities=offer.capabilities,
        provider=offer.provider,
        provider_resource_id="",
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
    await db.commit()
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
            await reconcile_order(db, number)
        else:

            def purchase():
                api = TwilioClient(
                    settings.twilio_account_sid,
                    settings.twilio_auth_token,
                    http_client=TwilioHttpClient(timeout=10, max_retries=0),
                )
                kwargs = {
                    "phone_number": offer.e164,
                    "friendly_name": f"hail-order-{number.id}",
                }
                if offer.verification_id:
                    kwargs["bundle_sid"] = offer.verification_id
                return api.incoming_phone_numbers.create(**kwargs)

            bought = await asyncio.to_thread(purchase)
            await finish_order(db, number, resource_id=bought.sid)
    except (httpx.HTTPStatusError, TwilioRestException) as exc:
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
    return number


async def reconcile_pending_orders():
    """Fresh session per number: one carrier/DB failure cannot stall the batch."""
    from hailhq.core.db import session_scope

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
