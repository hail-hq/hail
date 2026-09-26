"""DIDWW purchases through ``acquire_offer`` and ``reconcile_order``. The
carrier functions are mocked; their HTTP is covered in core tests."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from hailhq.api.number_orders import acquire_offer, reconcile_order
from hailhq.core import telephony_catalog
from hailhq.core.billing import get_balance_cents
from hailhq.core.config import settings
from hailhq.core.models import AccountCredit, CarrierVerification, NumberOffer
from hailhq.core.number_offers import CarrierOffer
from hailhq.core.providers.voice import CarrierRequestError
from sqlalchemy import select, text

ORDER = "o0000000-0000-0000-0000-000000000001"
DID = "d0000000-0000-0000-0000-000000000002"


async def seed_quote(
    db, org, *, address_id="addr-1", monthly=350, setup=350, kind="national"
):
    offer = CarrierOffer(
        provider="didww",
        e164="+351300000001",
        country_code="PT",
        number_type=kind,
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


async def buy(db, org, quote, monkeypatch, offer, *, kind="national"):
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    return await acquire_offer(
        db, org, quote.id, country="PT", kind=kind, provider="auto", billed=True
    )


def _non_catalog_kind(country="PT"):
    """A number type the live telephony catalog does not list for
    ``country``: proves ``acquire_offer``'s catalog gate is actually skipped
    for DIDWW, not merely inert because the catalog happens to cover
    everything asked for."""
    for kind in ("local", "mobile", "national", "toll_free"):
        if telephony_catalog.capabilities(country, kind) is None:
            return kind
    raise AssertionError(f"{country} lists every kind; pick another test country")


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
    """A DIDWW purchase for a kind the live catalog does not list for PT must
    still go through: DIDWW carries its own live price, so acquire_offer must
    not run catalog_capabilities() for it. Uses a kind the catalog actually
    lacks (not just one this test assumes is absent) so the assertion below
    fails if the ``offer.provider != DIDWW`` skip is ever removed."""
    kind = _non_catalog_kind()
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org, kind=kind)
    place = AsyncMock(return_value=ORDER)
    monkeypatch.setattr("hailhq.api.number_orders.place_didww_order", place)
    monkeypatch.setattr(
        "hailhq.api.number_orders.didww_order_outcome",
        AsyncMock(return_value=("pending", None, ORDER)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer, kind=kind)
    assert number.provisioning_state == "pending"
    place.assert_awaited_once_with(number.id, offer.e164, "addr-1")


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
    monkeypatch.setattr(
        "hailhq.api.number_orders.revoke_registration", AsyncMock(return_value=None)
    )
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


async def _seed_approved_verification(db, org, *, number_type="national"):
    row = CarrierVerification(
        organization_id=org,
        provider="didww",
        country_code="PT",
        number_type=number_type,
        subject_type="person",
        state="approved",
        provider_refs={"address_id": "addr-1"},
        requirements_version="v1",
    )
    db.add(row)
    await db.commit()
    return row


async def test_didww_registration_rejected_revokes_approval(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    verification = await _seed_approved_verification(async_session, org)
    other = await _seed_approved_verification(async_session, org, number_type="local")
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr("hailhq.api.number_orders.didww_order_outcome", outcome)
    monkeypatch.setattr("hailhq.api.number_orders.terminate_did", AsyncMock())
    revoke = AsyncMock(return_value="Document is blurry")
    monkeypatch.setattr("hailhq.api.number_orders.revoke_registration", revoke)
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.return_value = ("rejected_registration", DID, ORDER)
    await reconcile_order(async_session, number, force=True)
    revoke.assert_awaited_once_with("addr-1", org, "PT", "national")
    assert number.provisioning_metadata["failure_reason"] == "Document is blurry"
    await async_session.refresh(verification)
    assert verification.state == "rejected"
    assert verification.rejection_reason == "Document is blurry"
    await async_session.refresh(other)
    assert other.state == "approved"
    assert await get_balance_cents(async_session, org) == 100000 - 350


async def test_didww_registration_rejected_refunds_even_if_revoke_fails(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    verification = await _seed_approved_verification(async_session, org)
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr("hailhq.api.number_orders.didww_order_outcome", outcome)
    monkeypatch.setattr("hailhq.api.number_orders.terminate_did", AsyncMock())
    monkeypatch.setattr(
        "hailhq.api.number_orders.revoke_registration",
        AsyncMock(side_effect=RuntimeError("500")),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.return_value = ("rejected_registration", DID, ORDER)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    assert (
        number.provisioning_metadata["failure_reason"]
        == "the carrier rejected the end-user registration"
    )
    await async_session.refresh(verification)
    assert verification.state == "rejected"
    assert await get_balance_cents(async_session, org) == 100000 - 350


async def test_didww_pending_survives_three_days(
    async_session, org_and_key, monkeypatch
):
    """The DID exists but its registration never clears: after 7 days the
    DID is terminated and the setup fee stays."""
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.didww_order_outcome",
        AsyncMock(return_value=("pending", DID, ORDER)),
    )
    terminate = AsyncMock()
    monkeypatch.setattr("hailhq.api.number_orders.terminate_did", terminate)
    number = await buy(async_session, org, row, monkeypatch, offer)
    await _age(async_session, number, timedelta(days=3))
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "pending"
    terminate.assert_not_awaited()
    await _age(async_session, number, timedelta(days=7, minutes=1))
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    assert (
        number.provisioning_metadata["failure_reason"]
        == "the carrier did not approve the registration in time"
    )
    terminate.assert_awaited_once_with(DID)
    assert number.provider_resource_id is None
    assert await get_balance_cents(async_session, org) == 100000 - 350


async def test_didww_timeout_without_did_refunds_all(
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
    terminate = AsyncMock()
    monkeypatch.setattr("hailhq.api.number_orders.terminate_did", terminate)
    number = await buy(async_session, org, row, monkeypatch, offer)
    await _age(async_session, number, timedelta(days=7, minutes=1))
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    assert (
        number.provisioning_metadata["failure_reason"]
        == "the carrier did not confirm the order in time"
    )
    terminate.assert_not_awaited()
    assert await get_balance_cents(async_session, org) == 100000


async def test_didww_timeout_terminate_failure_still_finishes(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.didww_order_outcome",
        AsyncMock(return_value=("pending", DID, ORDER)),
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.terminate_did",
        AsyncMock(side_effect=RuntimeError("500")),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    await _age(async_session, number, timedelta(days=7, minutes=1))
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    assert await get_balance_cents(async_session, org) == 100000 - 350


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


async def test_quotes_search_non_catalog_kinds_at_didww_only(
    client, org_and_key, monkeypatch
):
    """A quote must never offer a Twilio/Telnyx number for a kind the
    catalog does not list (acquire_offer's catalog gate would 422 it)."""
    _, _, key = org_and_key
    monkeypatch.setattr(settings, "didww_api_key", "k")
    discover = AsyncMock(return_value=([], []))
    monkeypatch.setattr("hailhq.api.routes.numbers.discover_offers", discover)
    catalog_kinds = {
        kind
        for kind in ("local", "mobile", "national", "toll_free")
        if telephony_catalog.capabilities("PT", kind) is not None
    }
    non_catalog_kinds = {"local", "mobile", "national", "toll_free"} - catalog_kinds
    assert catalog_kinds and non_catalog_kinds, "PT must list some but not all kinds"

    resp = await client.post(
        "/numbers/quotes",
        json={"country_code": "PT", "capabilities": ["voice"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text

    assert discover.await_count == len(catalog_kinds | non_catalog_kinds)
    searched = {
        call.args[2]: call.kwargs["providers"] for call in discover.await_args_list
    }
    assert set(searched) == catalog_kinds | non_catalog_kinds
    for kind in catalog_kinds:
        assert set(searched[kind]) == {"twilio", "telnyx", "didww"}
    for kind in non_catalog_kinds:
        assert searched[kind] == ["didww"]


async def test_quotes_skip_non_catalog_kinds_when_didww_unconfigured(
    client, org_and_key, monkeypatch
):
    _, _, key = org_and_key
    monkeypatch.setattr(settings, "didww_api_key", "")
    discover = AsyncMock(return_value=([], []))
    monkeypatch.setattr("hailhq.api.routes.numbers.discover_offers", discover)
    catalog_kinds = {
        kind
        for kind in ("local", "mobile", "national", "toll_free")
        if telephony_catalog.capabilities("PT", kind) is not None
    }

    resp = await client.post(
        "/numbers/quotes",
        json={"country_code": "PT", "capabilities": ["voice"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    searched = {call.args[2] for call in discover.await_args_list}
    assert searched == catalog_kinds
