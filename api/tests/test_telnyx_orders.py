from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from hailhq.api.number_orders import (
    PENDING_ORDER_TIMEOUT,
    QUOTE_RETENTION,
    UNFOUND_ORDER_REVIEW_AFTER,
    acquire_offer,
    purge_expired_quotes,
    reconcile_order,
)
from hailhq.core.billing import get_balance_cents, monthly_fee_ref
from hailhq.core.models import AccountCredit, NumberOffer, PhoneNumber
from hailhq.core.number_offers import CarrierOffer
from hailhq.core.providers.voice import CarrierRequestError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from .conftest import insert_org_and_key


async def seed_quote(
    db,
    org,
    monthly=100,
    setup=50,
    readiness="ready",
    provider="telnyx",
    kind="local",
):
    offer = CarrierOffer(
        provider=provider,
        e164="+351211234567",
        country_code="PT",
        number_type=kind,
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
    await reconcile_order(async_session, number, force=True)
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
    assert debits[0].ref == monthly_fee_ref(org, number.id, number.acquired_at)
    await reconcile_order(async_session, number, force=True)
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
    await reconcile_order(async_session, number, force=True)
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
    "preference,ready",
    [(None, True), (None, False), ("twilio", True), ("twilio", False)],
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
    if preference == "twilio" and not ready:
        assert result["recommended_quote_id"] is None
        return
    recommended = next(
        o for o in result["offers"] if o["quote_id"] == result["recommended_quote_id"]
    )
    assert recommended["provider"] == ("twilio" if preference == "twilio" else "telnyx")

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


async def age(db, number):
    number.created_at = datetime.now(timezone.utc) - UNFOUND_ORDER_REVIEW_AFTER * 2
    await db.commit()


async def test_unfound_telnyx_order_requires_review_without_refund(
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
    assert await get_balance_cents(async_session, org) == 99850
    wire.side_effect = None
    wire.return_value = {"data": []}
    # No carrier record yet, but too early to conclude the POST was lost.
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "pending"
    assert await get_balance_cents(async_session, org) == 99850
    await age(async_session, number)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "pending"
    assert number.provisioning_metadata["needs_review"] is True
    assert await get_balance_cents(async_session, org) == 99850
    await reconcile_order(async_session, number, force=True)
    assert await get_balance_cents(async_session, org) == 99850

    # Later authoritative activation is still reconciled and billed once.
    recovered = AsyncMock(return_value=("active", str(uuid4()), str(uuid4())))
    monkeypatch.setattr("hailhq.api.number_orders.carrier_outcome", recovered)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "active"
    assert "needs_review" not in number.provisioning_metadata
    assert await get_balance_cents(async_session, org) == 99850


async def test_unfound_twilio_order_requires_review_without_refund(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org, provider="twilio")
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.purchase_ordered_number",
        AsyncMock(side_effect=CarrierRequestError(503)),
    )
    find = AsyncMock(return_value=None)
    monkeypatch.setattr("hailhq.api.number_orders.find_ordered_number", find)
    number = await buy(async_session, org, row)
    assert number.provisioning_state == "pending"
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "pending"
    assert await get_balance_cents(async_session, org) == 99850
    await age(async_session, number)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "pending"
    assert number.provisioning_metadata["needs_review"] is True
    assert await get_balance_cents(async_session, org) == 99850


async def test_twilio_order_found_at_carrier_is_activated(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org, provider="twilio")
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.purchase_ordered_number",
        AsyncMock(side_effect=CarrierRequestError(503)),
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.find_ordered_number",
        AsyncMock(return_value="PN_found"),
    )
    number = await buy(async_session, org, row)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "active"
    assert number.provider_resource_id == "PN_found"


async def test_failed_order_frees_the_number_for_a_new_order(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    monkeypatch.setattr("hailhq.core.config.settings.telnyx_api_key", "test")
    wire = AsyncMock(
        side_effect=[{"data": {"id": str(uuid4())}}, {"data": {"status": "failure"}}]
    )
    monkeypatch.setattr("hailhq.api.number_orders.TelnyxClient.request", wire)
    failed = await buy(async_session, org, row)
    assert failed.provisioning_state == "failed"
    assert failed.provider_resource_id is None
    again, _ = await seed_quote(async_session, org)
    wire.side_effect = [{"data": {"id": str(uuid4()), "status": "pending"}}] * 2
    retry = await buy(async_session, org, again)
    assert retry.id != failed.id
    assert retry.provisioning_state == "pending"


async def test_number_held_by_another_org_is_a_409(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    rival = (await insert_org_and_key(async_session, org_slug="rival"))[0]
    async_session.add(
        PhoneNumber(
            organization_id=rival,
            e164=offer.e164,
            country_code="PT",
            number_type="local",
            provisioning_state="pending",
        )
    )
    await async_session.commit()
    wire = AsyncMock()
    monkeypatch.setattr("hailhq.api.number_orders.TelnyxClient.request", wire)
    with pytest.raises(HTTPException) as exc:
        await buy(async_session, org, row)
    assert exc.value.status_code == 409
    wire.assert_not_awaited()


async def test_losing_a_cross_org_insert_race_is_a_409_and_charges_nothing(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    wire = AsyncMock()
    monkeypatch.setattr("hailhq.api.number_orders.TelnyxClient.request", wire)
    real_commit, calls = async_session.commit, []

    async def commit():
        calls.append(1)
        # 1st commit ends the pre-discovery step; 2nd inserts the number.
        if len(calls) == 2:
            raise IntegrityError(
                "INSERT", {}, Exception("phone_numbers_e164_live_uniq")
            )
        await real_commit()

    monkeypatch.setattr(async_session, "commit", commit)
    with pytest.raises(HTTPException) as exc:
        await buy(async_session, org, row)
    assert exc.value.status_code == 409
    wire.assert_not_awaited()
    assert await get_balance_cents(async_session, org) == 100000
    await async_session.refresh(row)
    assert row.number_id is None


async def test_discovery_runs_without_the_org_lock_or_a_transaction(
    async_session, org_and_key, monkeypatch, session_factory
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr("hailhq.core.config.settings.telnyx_api_key", "test")
    monkeypatch.setattr(
        "hailhq.api.number_orders.TelnyxClient.request",
        AsyncMock(return_value={"data": {"id": str(uuid4()), "status": "pending"}}),
    )
    seen = {}

    async def discover(*args, **kwargs):
        async with session_factory() as other:
            seen["lock_free"] = (
                await other.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                    {"k": str(org)},
                )
            ).scalar_one()
            seen["row_free"] = (
                await other.execute(
                    select(NumberOffer.id)
                    .where(NumberOffer.id == row.id)
                    .with_for_update(nowait=True)
                )
            ).scalar_one()
        return [offer], []

    monkeypatch.setattr("hailhq.api.number_orders.discover_offers", discover)
    await buy(async_session, org, row)
    assert seen == {"lock_free": True, "row_free": row.id}


async def test_get_number_survives_a_carrier_outage_while_pending(
    client, async_session, org_and_key, monkeypatch
):
    org, _, key = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    monkeypatch.setattr("hailhq.core.config.settings.telnyx_api_key", "test")
    wire = AsyncMock(side_effect=httpx.ReadTimeout("lost response"))
    monkeypatch.setattr("hailhq.api.number_orders.TelnyxClient.request", wire)
    number = await buy(async_session, org, row)
    wire.side_effect = httpx.ConnectError("carrier down")
    response = await client.get(
        f"/numbers/{number.id}", headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["provisioning_state"] == "pending"


async def test_reconciler_polls_share_a_persisted_interval(
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
    lookup = AsyncMock(return_value=("pending", None, None))
    monkeypatch.setattr("hailhq.api.number_orders.carrier_outcome", lookup)
    for _ in range(3):
        await reconcile_order(async_session, number)
    assert lookup.await_count == 1
    await async_session.refresh(number)
    number.provisioning_metadata = {
        **number.provisioning_metadata,
        "last_checked_at": (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat(),
    }
    await async_session.commit()
    await reconcile_order(async_session, number)
    assert lookup.await_count == 2


async def test_get_number_never_reconciles_or_writes_money_state(
    client, async_session, org_and_key, monkeypatch
):
    org, _, key = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    monkeypatch.setattr("hailhq.core.config.settings.telnyx_api_key", "test")
    wire = AsyncMock(side_effect=httpx.ReadTimeout("lost response"))
    monkeypatch.setattr("hailhq.api.number_orders.TelnyxClient.request", wire)
    number = await buy(async_session, org, row)
    lookup = AsyncMock(return_value=("active", "resource", None))
    monkeypatch.setattr("hailhq.api.number_orders.carrier_outcome", lookup)
    balance = await get_balance_cents(async_session, org)
    response = await client.get(
        f"/numbers/{number.id}", headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 200
    assert response.json()["provisioning_state"] == "pending"
    lookup.assert_not_awaited()
    assert await get_balance_cents(async_session, org) == balance


async def test_carrier_lookup_outage_at_recheck_is_a_retryable_503_not_a_409(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, _ = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([], ["telnyx"])),
    )
    with pytest.raises(HTTPException) as exc:
        await buy(async_session, org, row)
    assert exc.value.status_code == 503
    assert await get_balance_cents(async_session, org) == 100000
    await async_session.refresh(row)
    assert row.number_id is None


async def test_quote_mismatch_is_a_validation_shaped_422(async_session, org_and_key):
    org, _, _ = org_and_key
    row, _ = await seed_quote(async_session, org)
    with pytest.raises(HTTPException) as exc:
        await acquire_offer(
            async_session,
            org,
            row.id,
            country="PT",
            kind="mobile",
            provider="auto",
            billed=True,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail[0]["loc"] == ["body", "quote_id"]


async def test_only_stale_unused_quotes_are_purged(async_session, org_and_key):
    org, _, _ = org_and_key
    now = datetime.now(timezone.utc)
    stale = now - QUOTE_RETENTION - timedelta(minutes=1)
    stale_unused, _ = await seed_quote(async_session, org)
    stale_consumed, _ = await seed_quote(async_session, org)
    recent_unused, _ = await seed_quote(async_session, org)
    live, _ = await seed_quote(async_session, org)
    stale_unused.expires_at = stale
    stale_consumed.expires_at = stale
    stale_consumed.number_id = uuid4()
    recent_unused.expires_at = now - timedelta(minutes=5)
    await async_session.commit()
    assert await purge_expired_quotes() == 1
    kept = set((await async_session.execute(select(NumberOffer.id))).scalars())
    assert kept == {stale_consumed.id, recent_unused.id, live.id}


async def test_transient_recheck_failure_is_not_cached_under_the_idempotency_key(
    client, async_session, org_and_key, monkeypatch, voice_provider_mock
):
    org, _, key = org_and_key
    row, _ = await seed_quote(async_session, org)
    discover = AsyncMock(return_value=([], ["telnyx"]))
    monkeypatch.setattr("hailhq.api.number_orders.discover_offers", discover)
    for _ in range(2):
        response = await client.post(
            "/numbers",
            headers={"Authorization": f"Bearer {key}", "Idempotency-Key": "retry-1"},
            json={
                "country_code": "PT",
                "number_type": "local",
                "quote_id": str(row.id),
            },
        )
        assert response.status_code == 503, response.text
        assert "Idempotency-Replay" not in response.headers
    assert discover.await_count == 2


async def test_recheck_searches_with_the_capabilities_that_were_requested(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    offer.capabilities = ["sms", "voice"]
    row.offer = {**offer.model_dump(mode="json"), "requested_capabilities": ["voice"]}
    await async_session.commit()
    discover = AsyncMock(return_value=([offer], []))
    monkeypatch.setattr("hailhq.api.number_orders.discover_offers", discover)
    monkeypatch.setattr("hailhq.core.config.settings.telnyx_api_key", "test")
    monkeypatch.setattr(
        "hailhq.api.number_orders.TelnyxClient.request",
        AsyncMock(return_value={"data": {"id": str(uuid4()), "status": "pending"}}),
    )
    await buy(async_session, org, row)
    assert discover.await_args.args[3] == ["voice"]


async def test_quote_route_stores_the_requested_capabilities(
    client, async_session, org_and_key, monkeypatch
):
    org, _, key = org_and_key
    _, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.routes.numbers.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    response = await client.post(
        "/numbers/quotes",
        headers={"Authorization": f"Bearer {key}"},
        json={"country_code": "PT", "number_type": "local", "capabilities": ["voice"]},
    )
    assert response.status_code == 200, response.text
    quote_id = response.json()["offers"][0]["quote_id"]
    row = await async_session.get(NumberOffer, UUID(quote_id))
    assert row.offer["requested_capabilities"] == ["voice"]


async def _stub_order(monkeypatch, offer, *responses):
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    monkeypatch.setattr("hailhq.core.config.settings.telnyx_api_key", "test")
    wire = AsyncMock(side_effect=list(responses))
    monkeypatch.setattr("hailhq.api.number_orders.TelnyxClient.request", wire)
    return wire


async def test_pending_order_past_timeout_is_failed_and_refunded_once(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    await _stub_order(monkeypatch, offer, {"data": {"id": str(uuid4())}})
    number = await buy(async_session, org, row)
    assert number.provisioning_state == "pending"
    assert await get_balance_cents(async_session, org) == 100000 - 150
    monkeypatch.setattr(
        "hailhq.api.number_orders.carrier_outcome",
        AsyncMock(return_value=("pending", None, None)),
    )
    # Younger than the timeout: stays pending, hold kept.
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "pending"
    await async_session.execute(
        text("UPDATE phone_numbers SET created_at = :t WHERE id = :id"),
        {
            "t": datetime.now(timezone.utc)
            - PENDING_ORDER_TIMEOUT
            - timedelta(minutes=1),
            "id": number.id,
        },
    )
    await async_session.commit()
    await async_session.refresh(number)
    await reconcile_order(async_session, number, force=True)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    assert "in time" in number.provisioning_metadata["failure_reason"]
    assert await get_balance_cents(async_session, org) == 100000
    returns = (
        (
            await async_session.execute(
                select(AccountCredit).where(
                    AccountCredit.ref == f"number_reservation_return:{number.id}"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(returns) == 1


async def test_post_numbers_returns_409_for_a_refunded_failed_order(
    client, async_session, org_and_key, monkeypatch
):
    org, _, key = org_and_key
    row, offer = await seed_quote(async_session, org)
    await _stub_order(
        monkeypatch,
        offer,
        {"data": {"id": str(uuid4())}},
        {"data": {"status": "failure"}},
    )
    headers = {"Authorization": f"Bearer {key}", "Idempotency-Key": "failed-order"}
    body = {"country_code": "PT", "quote_id": str(row.id)}
    first = await client.post("/numbers", json=body, headers=headers)
    assert first.status_code == 409, first.text
    assert "refunded" in first.json()["detail"]
    assert "carrier reported the order as failed" in first.json()["detail"]
    assert await get_balance_cents(async_session, org) == 100000
    second = await client.post("/numbers", json=body, headers=headers)
    assert second.status_code == 409
    # A different key replays the consumed quote: still the same 409.
    third = await client.post(
        "/numbers",
        json=body,
        headers={"Authorization": f"Bearer {key}", "Idempotency-Key": "other"},
    )
    assert third.status_code == 409


async def test_replayed_quote_of_a_released_number_is_a_409_not_a_201(
    client, async_session, org_and_key, monkeypatch
):
    org, _, key = org_and_key
    row, offer = await seed_quote(async_session, org)
    await _stub_order(monkeypatch, offer, {"data": {"id": str(uuid4())}})
    body = {"country_code": "PT", "quote_id": str(row.id)}
    first = await client.post(
        "/numbers",
        json=body,
        headers={"Authorization": f"Bearer {key}", "Idempotency-Key": "first"},
    )
    assert first.status_code == 201, first.text
    number = await async_session.get(PhoneNumber, UUID(first.json()["id"]))
    number.provisioning_state = "released"
    number.released_at = datetime.now(timezone.utc)
    await async_session.commit()
    replay = await client.post(
        "/numbers",
        json=body,
        headers={"Authorization": f"Bearer {key}", "Idempotency-Key": "second"},
    )
    assert replay.status_code == 409, replay.text
    assert "released" in replay.json()["detail"]


async def test_quote_number_type_is_taken_from_the_quote(
    client, async_session, org_and_key, monkeypatch
):
    org, _, key = org_and_key
    row, offer = await seed_quote(async_session, org, kind="mobile")
    await _stub_order(monkeypatch, offer, {"data": {"id": str(uuid4())}})
    headers = {"Authorization": f"Bearer {key}"}
    conflict = await client.post(
        "/numbers",
        json={"country_code": "PT", "quote_id": str(row.id), "number_type": "local"},
        headers=headers,
    )
    assert conflict.status_code == 422
    ok = await client.post(
        "/numbers",
        json={"country_code": "pt", "quote_id": str(row.id)},
        headers=headers,
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["number_type"] == "mobile"


async def test_quote_purchase_of_a_type_missing_from_the_catalog_is_422(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org, kind="toll_free")
    discover = AsyncMock(return_value=([offer], []))
    monkeypatch.setattr("hailhq.api.number_orders.discover_offers", discover)
    with pytest.raises(HTTPException) as exc:
        await acquire_offer(
            async_session,
            org,
            row.id,
            country="PT",
            kind=None,
            provider="auto",
            billed=True,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail[0]["loc"] == ["body", "number_type"]
    discover.assert_not_awaited()
    assert await get_balance_cents(async_session, org) == 100000


async def test_quote_route_rejects_unlisted_type_and_accepts_lowercase_country(
    client, org_and_key, monkeypatch
):
    _, _, key = org_and_key
    discover = AsyncMock(return_value=([], []))
    monkeypatch.setattr("hailhq.api.routes.numbers.discover_offers", discover)
    headers = {"Authorization": f"Bearer {key}"}
    unlisted = await client.post(
        "/numbers/quotes",
        json={
            "country_code": "PT",
            "number_type": "toll_free",
            "capabilities": ["voice"],
        },
        headers=headers,
    )
    assert unlisted.status_code == 422
    discover.assert_not_awaited()
    lower = await client.post(
        "/numbers/quotes",
        json={"country_code": "pt", "capabilities": ["voice"]},
        headers=headers,
    )
    assert lower.status_code == 200, lower.text
    assert {call.args[1] for call in discover.await_args_list} == {"PT"}
    # Only the types the catalog lists for PT are searched.
    assert {call.args[2] for call in discover.await_args_list} == {"local", "mobile"}
