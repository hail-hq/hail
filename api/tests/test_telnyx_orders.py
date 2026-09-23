from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from hailhq.api.number_orders import acquire_offer, reconcile_order
from hailhq.core.billing import get_balance_cents
from hailhq.core.models import AccountCredit, NumberOffer, PhoneNumber
from hailhq.core.number_offers import CarrierOffer
from sqlalchemy import select


async def seed_quote(db, org, monthly=100, setup=50, readiness="ready"):
    offer = CarrierOffer(
        provider="telnyx",
        e164="+351211234567",
        country_code="PT",
        number_type="local",
        capabilities=["voice"],
        monthly_cents=monthly,
        setup_cents=setup,
        readiness=readiness,
    )
    row = NumberOffer(
        organization_id=org,
        offer=offer.model_dump(mode="json"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(row)
    await db.commit()
    return row, offer


async def buy(db, org, quote):
    return await acquire_offer(
        db, org, quote.id, country="PT", kind="local", provider="auto", billed=True
    )


@pytest.mark.parametrize("monthly,setup", [(100001, 0), (99999, 2)])
async def test_full_purchase_balance_before_carrier(
    async_session, org_and_key, monkeypatch, monthly, setup
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org, monthly, setup)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    wire = AsyncMock()
    monkeypatch.setattr("hailhq.api.number_orders.TelnyxClient.request", wire)
    with pytest.raises(HTTPException) as exc:
        await buy(async_session, org, row)
    assert exc.value.status_code == 402
    wire.assert_not_awaited()


async def test_pending_order_reserved_once_and_reconciles_to_owned_id(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    order_id, resource_id, order_phone_id = str(uuid4()), str(uuid4()), str(uuid4())
    wire = AsyncMock(
        side_effect=[
            {"data": {"id": order_id, "status": "pending"}},
            {"data": {"id": order_id, "status": "pending"}},
        ]
    )
    monkeypatch.setattr("hailhq.core.config.settings.telnyx_api_key", "test")
    monkeypatch.setattr("hailhq.api.number_orders.TelnyxClient.request", wire)
    number = await buy(async_session, org, row)
    assert number.provisioning_state == "pending"
    assert await get_balance_cents(async_session, org) == 99850
    # Different HTTP idempotency key, same consumed quote: still no new POST.
    again = await buy(async_session, org, row)
    assert again.id == number.id
    assert wire.await_count == 2
    wire.side_effect = [
        {
            "data": {
                "id": order_id,
                "status": "success",
                "requirements_met": True,
                "phone_numbers": [{"id": order_phone_id, "phone_number": number.e164}],
            }
        },
        {
            "data": [
                {"id": resource_id, "phone_number": number.e164, "status": "active"}
            ]
        },
    ]
    await reconcile_order(async_session, number)
    assert number.provisioning_state == "active"
    assert number.provider_resource_id == resource_id
    assert number.provider_resource_id != order_phone_id
    assert await get_balance_cents(async_session, org) == 99850
    debits = (
        (
            await async_session.execute(
                select(AccountCredit).where(
                    AccountCredit.organization_id == org,
                    AccountCredit.source == "monthly_fee",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(debits) == 1 and debits[0].amount_cents == -100
    await reconcile_order(async_session, number)
    assert wire.await_count == 4


async def test_order_timeout_is_not_retried_or_refunded_without_evidence(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    monkeypatch.setattr("hailhq.core.config.settings.telnyx_api_key", "test")
    wire = AsyncMock(side_effect=httpx.ReadTimeout("lost response"))
    monkeypatch.setattr("hailhq.api.number_orders.TelnyxClient.request", wire)
    number = await buy(async_session, org, row)
    assert number.provisioning_state == "pending"
    assert await get_balance_cents(async_session, org) == 99850
    await async_session.refresh(row)
    await buy(async_session, org, row)
    assert wire.await_count == 1


async def test_failed_order_refunds_once(async_session, org_and_key, monkeypatch):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    monkeypatch.setattr("hailhq.core.config.settings.telnyx_api_key", "test")
    order_id = str(uuid4())
    wire = AsyncMock(
        side_effect=[{"data": {"id": order_id}}, {"data": {"status": "failure"}}]
    )
    monkeypatch.setattr("hailhq.api.number_orders.TelnyxClient.request", wire)
    number = await buy(async_session, org, row)
    assert number.provisioning_state == "failed"
    assert await get_balance_cents(async_session, org) == 100000
    await reconcile_order(async_session, number)
    await buy(async_session, org, row)
    assert await get_balance_cents(async_session, org) == 100000
    assert wire.await_count == 2


async def test_quote_is_org_bound_and_expiring(async_session, org_and_key, monkeypatch):
    org, _, _ = org_and_key
    row, _ = await seed_quote(async_session, org)
    with pytest.raises(HTTPException) as exc:
        await buy(async_session, uuid4(), row)
    assert exc.value.status_code == 404
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await async_session.commit()
    with pytest.raises(HTTPException) as exc:
        await buy(async_session, org, row)
    assert exc.value.status_code == 409


async def test_recheck_price_and_verification_before_charging(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    offer.readiness = "verification_required"
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    with pytest.raises(HTTPException) as exc:
        await buy(async_session, org, row)
    assert exc.value.status_code == 409
    assert await get_balance_cents(async_session, org) == 100000
    assert (await async_session.execute(select(PhoneNumber))).scalars().all() == []


@pytest.mark.parametrize(
    "preference,ready", [(None, True), (None, False), ("auto", True), ("auto", False)]
)
async def test_quote_api_returns_live_recommendation_and_persists_org_scope(
    client, async_session, org_and_key, monkeypatch, preference, ready
):
    org, _, key = org_and_key
    _, offer = await seed_quote(async_session, org)
    blocked = offer.model_copy(
        update={
            "provider": "twilio",
            "monthly_cents": 200,
            "readiness": "ready" if ready else "verification_required",
        }
    )
    monkeypatch.setattr(
        "hailhq.api.routes.numbers.discover_offers",
        AsyncMock(return_value=([blocked, offer], [])),
    )
    response = await client.post(
        "/numbers/quotes",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "country_code": "PT",
            "number_type": "local",
            "capabilities": ["voice"],
            **({"provider": preference} if preference else {}),
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    if preference is None and not ready:
        assert result["recommended_quote_id"] is None
        return
    recommended = next(
        o for o in result["offers"] if o["quote_id"] == result["recommended_quote_id"]
    )
    assert recommended["provider"] == ("twilio" if preference is None else "telnyx")

    row = await async_session.get(NumberOffer, UUID(recommended["quote_id"]))
    assert row.organization_id == org


async def test_status_lookup_rejection_does_not_refund_accepted_order(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    monkeypatch.setattr("hailhq.core.config.settings.telnyx_api_key", "test")
    order_id = str(uuid4())
    request = httpx.Request(
        "GET", f"https://api.telnyx.com/v2/number_orders/{order_id}"
    )
    wire = AsyncMock(
        side_effect=[
            {"data": {"id": order_id}},
            httpx.HTTPStatusError(
                "not visible yet",
                request=request,
                response=httpx.Response(404, request=request),
            ),
        ]
    )
    monkeypatch.setattr("hailhq.api.number_orders.TelnyxClient.request", wire)
    number = await buy(async_session, org, row)
    assert number.provisioning_state == "pending"
    assert number.provisioning_metadata["order_id"] == order_id
    assert await get_balance_cents(async_session, org) == 99850
    await async_session.refresh(row)
    await buy(async_session, org, row)
    assert wire.await_count == 2
