"""DIDWW purchases through ``acquire_offer`` and ``reconcile_order``. The
carrier functions are mocked; their HTTP is covered in core tests."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from hailhq.api.number_orders import (
    acquire_offer,
    reconcile_order,
    retry_unterminated_dids,
)
from hailhq.api.routes import numbers as numbers_routes
from hailhq.core import telephony_catalog
from hailhq.core.billing import get_balance_cents
from hailhq.core.models import (
    AccountCredit,
    AuditLog,
    CarrierVerification,
    NumberOffer,
    PhoneNumber,
)
from hailhq.core.number_offers import CarrierOffer
from hailhq.core.providers.voice import (
    CarrierNotConfigured,
    CarrierPreOrderError,
    CarrierRequestError,
)
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


@pytest.fixture(autouse=True)
def catalogs(tmp_path, monkeypatch):
    """One catalog per carrier: Twilio lists PT local, DIDWW lists PT national,
    Telnyx lists nothing for PT."""

    def row(kind):
        return {
            "country_code": "PT",
            "number_type": kind,
            "usd_per_month": "3.50",
            "voice": True,
            "sms": False,
            "mms": False,
        }

    (tmp_path / "twilio.json").write_text(
        json.dumps({"numbers": [row("local")], "a2p_10dlc": []})
    )
    (tmp_path / "telnyx.json").write_text(json.dumps({"numbers": []}))
    (tmp_path / "didww.json").write_text(json.dumps({"numbers": [row("national")]}))
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_DIR", str(tmp_path))
    telephony_catalog._load.cache_clear()
    yield
    telephony_catalog._load.cache_clear()


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


async def test_didww_purchase_checks_the_didww_catalog(
    async_session, org_and_key, monkeypatch
):
    """PT national is in the DIDWW catalog and in no other: the purchase must
    check the quoted carrier's own catalog."""
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
    place.assert_awaited_once_with(number.id, offer.e164, "addr-1")


async def test_didww_purchase_outside_its_catalog_is_a_422(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org, kind="toll_free")
    place = AsyncMock(return_value=ORDER)
    monkeypatch.setattr("hailhq.api.number_orders.place_didww_order", place)
    with pytest.raises(HTTPException) as exc:
        await buy(async_session, org, row, monkeypatch, offer, kind="toll_free")
    assert exc.value.status_code == 422
    place.assert_not_awaited()
    assert await get_balance_cents(async_session, org) == 100000


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
    assert "unterminated_did_id" not in number.provisioning_metadata
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
    audit = (
        await async_session.execute(
            select(AuditLog).where(
                AuditLog.action == "verification.reject",
                AuditLog.resource_id == verification.id,
            )
        )
    ).scalar_one()
    assert audit.actor_kind == "system"
    assert audit.payload["reason"] == "Document is blurry"


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
    # The DID still renews at the carrier: its id is kept on the row.
    await async_session.refresh(number)
    assert number.provisioning_metadata["unterminated_did_id"] == DID


async def test_didww_rejected_terminate_failure_keeps_the_did_id(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr("hailhq.api.number_orders.didww_order_outcome", outcome)
    monkeypatch.setattr(
        "hailhq.api.number_orders.terminate_did",
        AsyncMock(side_effect=RuntimeError("500")),
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.revoke_registration", AsyncMock(return_value=None)
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.return_value = ("rejected_registration", DID, ORDER)
    await reconcile_order(async_session, number, force=True)
    await async_session.refresh(number)
    assert number.provisioning_state == "failed"
    assert number.provisioning_metadata["unterminated_did_id"] == DID
    assert await get_balance_cents(async_session, org) == 100000 - 350


async def _mark_check(db, number, *, started: timedelta):
    """As if another run claimed a check ``started`` ago and has not answered."""
    then = (datetime.now(timezone.utc) - started).isoformat()
    number.provisioning_metadata = {
        **number.provisioning_metadata,
        "last_checked_at": then,
        "check_started_at": then,
    }
    await db.commit()


async def test_reconcile_skips_while_another_check_is_running(
    async_session, org_and_key, monkeypatch
):
    """One DIDWW check can take longer than the poll interval. A second run
    must not start meanwhile: both would file a registration for the DID."""
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    outcome = AsyncMock(return_value=("pending", DID, ORDER))
    monkeypatch.setattr("hailhq.api.number_orders.didww_order_outcome", outcome)
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.reset_mock()
    await _mark_check(async_session, number, started=timedelta(seconds=30))
    await reconcile_order(async_session, number)
    outcome.assert_not_awaited()


async def test_reconcile_takes_over_a_check_that_never_answered(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    outcome = AsyncMock(return_value=("pending", DID, ORDER))
    monkeypatch.setattr("hailhq.api.number_orders.didww_order_outcome", outcome)
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.reset_mock()
    await _mark_check(async_session, number, started=timedelta(minutes=6))
    await reconcile_order(async_session, number)
    outcome.assert_awaited_once()
    await async_session.refresh(number)
    assert "check_started_at" not in number.provisioning_metadata


async def test_reconcile_clears_its_claim_when_the_lookup_fails(
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
    await async_session.refresh(number)
    assert "check_started_at" not in number.provisioning_metadata
    outcome.side_effect = RuntimeError("500")
    with pytest.raises(RuntimeError):
        await reconcile_order(async_session, number, force=True)
    await async_session.refresh(number)
    assert "check_started_at" not in number.provisioning_metadata


async def _rejected_with_failed_terminate(db, org, monkeypatch):
    """A failed DIDWW order whose DID could not be terminated."""
    row, offer = await seed_quote(db, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order", AsyncMock(return_value=ORDER)
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr("hailhq.api.number_orders.didww_order_outcome", outcome)
    monkeypatch.setattr(
        "hailhq.api.number_orders.terminate_did",
        AsyncMock(side_effect=RuntimeError("500")),
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.revoke_registration", AsyncMock(return_value=None)
    )
    number = await buy(db, org, row, monkeypatch, offer)
    outcome.return_value = ("rejected_registration", DID, ORDER)
    await reconcile_order(db, number, force=True)
    await db.refresh(number)
    assert number.provisioning_metadata["unterminated_did_id"] == DID
    return number


async def test_retry_terminates_a_did_left_behind(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    number = await _rejected_with_failed_terminate(async_session, org, monkeypatch)
    release = AsyncMock()
    monkeypatch.setattr("hailhq.api.number_orders.release_didww_number", release)
    await retry_unterminated_dids()
    release.assert_awaited_once_with(DID)
    await async_session.refresh(number)
    assert "unterminated_did_id" not in number.provisioning_metadata
    assert number.provisioning_state == "failed"
    assert await get_balance_cents(async_session, org) == 100000 - 350
    # Nothing is left to retry.
    await retry_unterminated_dids()
    release.assert_awaited_once()


async def test_retry_keeps_the_did_id_when_the_carrier_fails_again(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    number = await _rejected_with_failed_terminate(async_session, org, monkeypatch)
    release = AsyncMock(side_effect=RuntimeError("500"))
    monkeypatch.setattr("hailhq.api.number_orders.release_didww_number", release)
    await retry_unterminated_dids()  # never raises
    release.assert_awaited_once_with(DID)
    await async_session.refresh(number)
    assert number.provisioning_metadata["unterminated_did_id"] == DID


async def test_retry_skips_failed_orders_with_nothing_left_behind(
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
    release = AsyncMock()
    monkeypatch.setattr("hailhq.api.number_orders.release_didww_number", release)
    await retry_unterminated_dids()
    release.assert_not_awaited()


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


async def test_didww_number_gone_before_order_fails_immediately(
    async_session, org_and_key, monkeypatch
):
    """place_didww_order's local inventory search (_find_available) raises a
    410 when the number vanished before any order was POSTed. No order was
    ever placed at the carrier, so this must fail and refund immediately
    instead of sitting 'pending' for up to DIDWW's 7-day reconciliation
    timeout (unlike Telnyx's ambiguous 408/409 post-POST timeouts)."""
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order",
        AsyncMock(side_effect=CarrierPreOrderError(410)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    assert number.provisioning_state == "failed"
    assert (
        number.provisioning_metadata["failure_reason"]
        == "the number is no longer available"
    )
    assert await get_balance_cents(async_session, org) == 100000


@pytest.mark.parametrize("status", [409, 500, 502])
async def test_didww_inventory_failure_before_order_refunds_at_once(
    async_session, org_and_key, monkeypatch, status
):
    """The inventory search failed and no order was sent. The hold must not
    wait 7 days for a reconciliation that can never find an order."""
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order",
        AsyncMock(side_effect=CarrierPreOrderError(status)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    assert number.provisioning_state == "failed"
    assert (
        number.provisioning_metadata["failure_reason"]
        == "the carrier could not be reached; nothing was ordered"
    )
    assert await get_balance_cents(async_session, org) == 100000


async def test_didww_order_post_failure_keeps_the_hold(
    async_session, org_and_key, monkeypatch
):
    """POST /orders answered 500: the order may exist, so the hold stays."""
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.api.number_orders.place_didww_order",
        AsyncMock(side_effect=CarrierRequestError(500)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    assert number.provisioning_state == "pending"
    assert await get_balance_cents(async_session, org) == 100000 - 700


async def test_quotes_ask_each_kind_only_at_carriers_that_list_it(
    client, org_and_key, monkeypatch
):
    """A quote must never show an offer the purchase would refuse: the
    purchase checks the quoted carrier's own catalog."""
    _, _, key = org_and_key
    discover = AsyncMock(return_value=([], []))
    monkeypatch.setattr("hailhq.api.routes.numbers.discover_offers", discover)

    resp = await client.post(
        "/numbers/quotes",
        json={"country_code": "PT", "capabilities": ["voice"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    searched = {
        call.args[2]: call.kwargs["providers"] for call in discover.await_args_list
    }
    assert searched == {"local": ["twilio"], "national": ["didww"]}


async def test_quotes_for_one_carrier_search_only_its_kinds(
    client, org_and_key, monkeypatch
):
    _, _, key = org_and_key
    discover = AsyncMock(return_value=([], []))
    monkeypatch.setattr("hailhq.api.routes.numbers.discover_offers", discover)

    resp = await client.post(
        "/numbers/quotes",
        json={"country_code": "PT", "capabilities": ["voice"], "provider": "didww"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    searched = {
        call.args[2]: call.kwargs["providers"] for call in discover.await_args_list
    }
    assert searched == {"national": ["didww"]}


async def test_release_without_carrier_key_does_not_name_the_carrier(monkeypatch):
    monkeypatch.setattr(
        numbers_routes,
        "release_didww_number",
        AsyncMock(
            side_effect=CarrierNotConfigured("DIDWW is not configured (DIDWW_API_KEY)")
        ),
    )
    number = PhoneNumber(provider="didww", provider_resource_id=DID)
    with pytest.raises(HTTPException) as exc:
        await numbers_routes._release_didww(number, None)
    assert exc.value.status_code == 503
    assert exc.value.detail == "the carrier is not configured"
