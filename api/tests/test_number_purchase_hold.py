"""A purchase counts the credit its verification set aside, and the quote
shows that amount (core/verification_hold.py)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from hailhq.core import verification_hold
from hailhq.core.billing import get_balance_cents
from hailhq.core.carrier_offer import Approval
from hailhq.core.models import AccountCredit, NumberOffer
from hailhq.core.number_offers import CarrierOffer
from hailhq.core.providers.voice import CarrierPreOrderError
from sqlalchemy import select

from .conftest import insert_org_and_key
from .test_verifications_api import (  # noqa: F401 (autouse fixture)
    _approved,
    _auth,
    pinned_catalog,
)


async def _credit(session, org, cents: int) -> None:
    session.add(
        AccountCredit(
            organization_id=org,
            kind="credit" if cents > 0 else "debit",
            channel="credit",
            amount_cents=cents,
            qty=1,
            ref=f"test:{uuid.uuid4()}",
            source="test",
        )
    )
    await session.commit()


async def _held_approval(session, *, hold: int, outside: int):
    """An org with an approved verification that set ``hold`` aside, and
    ``outside`` cents of balance beside it."""
    org, _, key = await insert_org_and_key(session, initial_credit_cents=0)
    vid = await _approved(session, org, "person", "BU_person", "Ada Lovelace")
    await _credit(session, org, hold + outside)
    await verification_hold.place_hold(session, org, vid, hold, channel="voice")
    await session.commit()
    assert await get_balance_cents(session, org) == outside
    return org, key, vid


async def _buy(client, session, monkeypatch, org, key, vid, *, monthly, purchase):
    offer = CarrierOffer(
        provider="twilio",
        e164="+447700900001",
        country_code="GB",
        number_type="mobile",
        capabilities=["voice", "sms"],
        monthly_cents=monthly,
        setup_cents=0,
        readiness="ready",
        subject_types=["person"],
        approvals=[
            Approval(
                row_id=vid,
                subject_type="person",
                verification_id="BU_person",
                subject_name="Ada Lovelace",
            )
        ],
    )
    quote = NumberOffer(
        organization_id=org,
        offer=offer.model_dump(mode="json"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(quote)
    await session.commit()
    monkeypatch.setattr(
        "hailhq.core.providers.voice.twilio.purchase_ordered_number", purchase
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    return await client.post(
        "/numbers",
        json={"country_code": "GB", "quote_id": str(quote.id)},
        headers=_auth(key),
    )


async def _by_ref(session, org, ref) -> list[AccountCredit]:
    return (
        (
            await session.execute(
                select(AccountCredit).where(
                    AccountCredit.organization_id == org,
                    AccountCredit.ref == ref,
                )
            )
        )
        .scalars()
        .all()
    )


async def test_buy_counts_open_hold(client, async_session, monkeypatch) -> None:
    """The verification set the whole price aside, then the rest of the
    balance went on calls: the buy still works."""
    org, key, vid = await _held_approval(async_session, hold=250, outside=0)
    resp = await _buy(
        client,
        async_session,
        monkeypatch,
        org,
        key,
        vid,
        monthly=250,
        purchase=AsyncMock(return_value="PN_gb_1"),
    )
    assert resp.status_code == 201, resp.text
    number_id = resp.json()["id"]
    async_session.expire_all()
    (returned,) = await _by_ref(async_session, org, verification_hold.release_ref(vid))
    (reserved,) = await _by_ref(async_session, org, f"number_reservation:{number_id}")
    assert int(returned.amount_cents) == 250
    # One transaction: Postgres stamps both rows with its start time.
    assert returned.created_at == reserved.created_at
    assert await verification_hold.open_hold_cents(async_session, org, vid) == 0
    # The first month is paid from the amount set aside.
    assert await get_balance_cents(async_session, org) == 0


async def test_buy_needs_extra_when_price_rose(
    client, async_session, monkeypatch
) -> None:
    org, key, vid = await _held_approval(async_session, hold=100, outside=30)
    purchase = AsyncMock(return_value="PN_gb_1")
    resp = await _buy(
        client,
        async_session,
        monkeypatch,
        org,
        key,
        vid,
        monthly=150,
        purchase=purchase,
    )
    assert resp.status_code == 402, resp.text
    purchase.assert_not_awaited()
    assert await verification_hold.open_hold_cents(async_session, org, vid) == 100


async def test_buy_failure_does_not_remake_hold(
    client, async_session, monkeypatch
) -> None:
    org, key, vid = await _held_approval(async_session, hold=250, outside=0)
    resp = await _buy(
        client,
        async_session,
        monkeypatch,
        org,
        key,
        vid,
        monthly=250,
        purchase=AsyncMock(side_effect=CarrierPreOrderError(410)),
    )
    assert resp.status_code == 409, resp.text
    # The order's hold is refunded to the balance; the verification's hold
    # is not made again.
    assert len(await _by_ref(async_session, org, verification_hold.hold_ref(vid))) == 1
    assert await verification_hold.open_hold_cents(async_session, org, vid) == 0
    assert await get_balance_cents(async_session, org) == 250
