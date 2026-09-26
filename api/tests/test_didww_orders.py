"""DIDWW purchases through ``acquire_offer`` and ``reconcile_order``. The
carrier functions are mocked; their HTTP is covered in core tests."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from hailhq.api.number_orders import acquire_offer, reconcile_order
from hailhq.core.billing import get_balance_cents
from hailhq.core.models import AccountCredit, NumberOffer
from hailhq.core.number_offers import CarrierOffer
from hailhq.core.providers.voice import CarrierRequestError
from sqlalchemy import select, text

ORDER = "o0000000-0000-0000-0000-000000000001"
DID = "d0000000-0000-0000-0000-000000000002"


async def seed_quote(db, org, *, address_id="addr-1", monthly=350, setup=350):
    offer = CarrierOffer(
        provider="didww",
        e164="+351300000001",
        country_code="PT",
        number_type="national",
        capabilities=["voice"],
        monthly_cents=monthly,
        setup_cents=setup,
        readiness="ready",
        verification_id=address_id,
        address_id=address_id,
    )
    row = NumberOffer(
        organization_id=org,
        offer=offer.model_dump(mode="json"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(row)
    await db.commit()
    return row, offer


async def buy(db, org, quote, monkeypatch, offer):
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    return await acquire_offer(
        db, org, quote.id, country="PT", kind="national", provider="auto", billed=True
    )


async def _age(db, number, delta):
    await db.execute(
        text("UPDATE phone_numbers SET created_at = :t WHERE id = :id"),
        {"t": datetime.now(timezone.utc) - delta, "id": number.id},
    )
    await db.commit()
    await db.refresh(number)


async def test_didww_purchase_places_order_and_waits(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    place = AsyncMock(return_value=ORDER)
    monkeypatch.setattr("hailhq.api.number_orders.place_didww_order", place)
    monkeypatch.setattr(
        "hailhq.api.number_orders.didww_order_outcome",
        AsyncMock(return_value=("pending", None, ORDER)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    assert number.provisioning_state == "pending"
    assert number.provider == "didww"
    assert number.provisioning_metadata["order_id"] == ORDER
    place.assert_awaited_once_with(number.id, "+351300000001", "addr-1")
    assert await get_balance_cents(async_session, org) == 100000 - 700


async def test_didww_purchase_is_not_catalog_gated(
    async_session, org_and_key, monkeypatch
):
    """PT/national is not in the test catalog; DIDWW carries its own price."""
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.didww_order_outcome",
        AsyncMock(return_value=("pending", None, ORDER)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    assert number.provisioning_state == "pending"


async def test_didww_registration_approved_activates(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr("hailhq.api.number_orders.didww_order_outcome", outcome)
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.return_value = ("active", DID, ORDER)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "active"
    assert number.provider_resource_id == DID
    outcome.assert_awaited_with("+351300000001", number.id, ORDER, "addr-1")
    assert await get_balance_cents(async_session, org) == 100000 - 700


async def test_didww_registration_rejected_refunds_monthly_only(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr("hailhq.api.number_orders.didww_order_outcome", outcome)
    terminate = AsyncMock()
    monkeypatch.setattr("hailhq.api.number_orders.terminate_did", terminate)
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.return_value = ("rejected_registration", DID, ORDER)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    assert "registration" in number.provisioning_metadata["failure_reason"]
    terminate.assert_awaited_once_with(DID)
    assert await get_balance_cents(async_session, org) == 100000 - 350
    setup = (
        await async_session.execute(
            select(AccountCredit).where(
                AccountCredit.ref == f"number_setup:{number.id}"
            )
        )
    ).scalar_one()
    assert setup.amount_cents == -350
    # A second pass changes nothing.
    await reconcile_order(async_session, number, force=True)
    assert await get_balance_cents(async_session, org) == 100000 - 350


async def test_didww_pending_survives_three_days(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.didww_order_outcome",
        AsyncMock(return_value=("pending", None, ORDER)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    await _age(async_session, number, timedelta(days=3))
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "pending"
    await _age(async_session, number, timedelta(days=7, minutes=1))
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    assert await get_balance_cents(async_session, org) == 100000


async def test_lookup_error_keeps_pending_before_timeout(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr("hailhq.api.number_orders.didww_order_outcome", outcome)
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.side_effect = RuntimeError("429")
    with pytest.raises(RuntimeError):
        await reconcile_order(async_session, number, force=True)
    await async_session.refresh(number)
    assert number.provisioning_state == "pending"


async def test_didww_order_rejected_at_carrier_refunds_all(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order",
        AsyncMock(side_effect=CarrierRequestError(422)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    assert number.provisioning_state == "failed"
    assert await get_balance_cents(async_session, org) == 100000
