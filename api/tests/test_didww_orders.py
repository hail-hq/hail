"""DIDWW purchases through ``acquire_offer`` and ``reconcile_order``. The
carrier functions are mocked; their HTTP is covered in core tests."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from hailhq.api.number_orders import (
    acquire_offer,
    reconcile_order,
    retry_unreleased_numbers,
)
from hailhq.core import telephony_catalog
from hailhq.core.billing import get_balance_cents
from hailhq.core.carrier_offer import Approval
from hailhq.core.models import (
    AccountCredit,
    AuditLog,
    CarrierVerification,
    NumberOffer,
)
from hailhq.core.number_offers import CarrierOffer
from hailhq.core.providers.voice import (
    CarrierPreOrderError,
    CarrierRequestError,
)
from sqlalchemy import select, text

ORDER = "o0000000-0000-0000-0000-000000000001"
DID = "d0000000-0000-0000-0000-000000000002"


async def seed_quote(
    db,
    org,
    *,
    address_id="addr-1",
    monthly=350,
    setup=350,
    kind="national",
    verification_row_id=None,
):
    """A ready DIDWW quote. With ``address_id``, the org's approved person
    verification (``verification_row_id``, or a made-up id) is on it."""
    approvals = (
        [
            Approval(
                row_id=verification_row_id or uuid4(),
                subject_type="person",
                verification_id=address_id,
                address_id=address_id,
            )
        ]
        if address_id
        else []
    )
    offer = CarrierOffer(
        provider="didww",
        e164="+351300000001",
        country_code="PT",
        number_type=kind,
        capabilities=["voice"],
        monthly_cents=monthly,
        setup_cents=setup,
        readiness="ready",
        subject_types=["person"] if address_id else [],
        approvals=approvals,
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
    return await acquire_offer(db, org, quote.id, country="PT", kind=kind, billed=True)


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
    monkeypatch.setattr("hailhq.core.providers.voice.didww.place_didww_order", place)
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome",
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
    monkeypatch.setattr("hailhq.core.providers.voice.didww.place_didww_order", place)
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome",
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
    monkeypatch.setattr("hailhq.core.providers.voice.didww.place_didww_order", place)
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
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome", outcome
    )
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
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome", outcome
    )
    terminate = AsyncMock()
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number", terminate
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.revoke_registration",
        AsyncMock(return_value=None),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.return_value = ("rejected_registration", DID, ORDER)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    assert "registration" in number.provisioning_metadata["failure_reason"]
    terminate.assert_awaited_once_with(DID)
    assert "unreleased_resource_id" not in number.provisioning_metadata
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


async def _seed_approved_verification(
    db, org, *, number_type="national", subject_type="person", address_id="addr-1"
):
    row = CarrierVerification(
        organization_id=org,
        provider="didww",
        country_code="PT",
        number_type=number_type,
        subject_type=subject_type,
        state="approved",
        provider_refs={"address_id": address_id},
        requirements_version="v1",
    )
    db.add(row)
    await db.commit()
    return row


NEUTRAL_REJECTION = (
    "the carrier rejected the end-user registration; "
    "start a new verification with corrected details"
)


async def test_didww_registration_rejected_revokes_approval(
    async_session, org_and_key, monkeypatch, caplog
):
    org, _, _ = org_and_key
    verification = await _seed_approved_verification(async_session, org)
    other = await _seed_approved_verification(async_session, org, number_type="local")
    row, offer = await seed_quote(
        async_session, org, verification_row_id=verification.id
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome", outcome
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number", AsyncMock()
    )
    revoke = AsyncMock(return_value="Document is blurry")
    monkeypatch.setattr("hailhq.core.providers.voice.didww.revoke_registration", revoke)
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.return_value = ("rejected_registration", DID, ORDER)
    with caplog.at_level("WARNING", logger="hailhq.api.number_orders"):
        await reconcile_order(async_session, number, force=True)
    revoke.assert_awaited_once_with("addr-1", org, "PT", "national")
    # The buyer reads Hail's wording; the carrier's text stays server-side.
    assert number.provisioning_metadata["failure_reason"] == NEUTRAL_REJECTION
    assert number.provisioning_metadata["carrier_reason"] == "Document is blurry"
    messages = [r.getMessage() for r in caplog.records]
    # The reviewer's note can quote personal data: kept in the DB, not logged.
    assert not any("Document is blurry" in m for m in messages)
    logged = [m for m in messages if "rejected the registration" in m]
    assert len(logged) == 1
    assert str(org) in logged[0] and str(number.id) in logged[0]
    assert str(verification.id) in logged[0]
    await async_session.refresh(verification)
    assert verification.state == "rejected"
    assert verification.rejection_reason == NEUTRAL_REJECTION
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
    assert audit.payload["reason"] == NEUTRAL_REJECTION
    assert audit.payload["carrier_reason"] == "Document is blurry"


async def test_didww_registration_rejected_refunds_even_if_revoke_fails(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    verification = await _seed_approved_verification(async_session, org)
    row, offer = await seed_quote(
        async_session, org, verification_row_id=verification.id
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome", outcome
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number", AsyncMock()
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.revoke_registration",
        AsyncMock(side_effect=RuntimeError("500")),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.return_value = ("rejected_registration", DID, ORDER)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    assert number.provisioning_metadata["failure_reason"] == NEUTRAL_REJECTION
    assert "carrier_reason" not in number.provisioning_metadata
    await async_session.refresh(verification)
    assert verification.state == "rejected"
    assert await get_balance_cents(async_session, org) == 100000 - 350


async def _reject_bought_number(async_session, org, monkeypatch, quote, offer):
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome", outcome
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number", AsyncMock()
    )
    revoke = AsyncMock(return_value=None)
    monkeypatch.setattr("hailhq.core.providers.voice.didww.revoke_registration", revoke)
    number = await buy(async_session, org, quote, monkeypatch, offer)
    outcome.return_value = ("rejected_registration", DID, ORDER)
    await reconcile_order(async_session, number, force=True)
    return number, revoke


async def test_rejected_registration_rejects_only_the_verification_used(
    async_session, org_and_key, monkeypatch
):
    """A person and a business verification are both approved. The number
    was bought with the business one: only that one is rejected, and the
    carrier takes back that one's address."""
    org, _, _ = org_and_key
    person = await _seed_approved_verification(async_session, org)
    business = await _seed_approved_verification(
        async_session, org, subject_type="business", address_id="addr-2"
    )
    quote, offer = await seed_quote(async_session, org, address_id=None)
    offer = offer.model_copy(
        update={
            "subject_types": ["business", "person"],
            "approvals": [
                Approval(
                    row_id=person.id,
                    subject_type="person",
                    verification_id="addr-1",
                    address_id="addr-1",
                ),
                Approval(
                    row_id=business.id,
                    subject_type="business",
                    verification_id="addr-2",
                    address_id="addr-2",
                ),
            ],
        }
    )
    quote.offer = offer.model_dump(mode="json")
    await async_session.commit()
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    number = await acquire_offer(
        async_session,
        org,
        quote.id,
        country="PT",
        kind="national",
        billed=True,
        subject_type="business",
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome",
        AsyncMock(return_value=("rejected_registration", DID, ORDER)),
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number", AsyncMock()
    )
    revoke = AsyncMock(return_value=None)
    monkeypatch.setattr("hailhq.core.providers.voice.didww.revoke_registration", revoke)
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    revoke.assert_awaited_once_with("addr-2", org, "PT", "national")
    await async_session.refresh(person)
    await async_session.refresh(business)
    assert (person.state, business.state) == ("approved", "rejected")


async def test_rejected_registration_of_an_older_number_with_two_rows(
    async_session, org_and_key, monkeypatch
):
    """A number bought before purchases stored the verification id: it is
    matched by carrier, country and type, and two approved rows are no
    error."""
    org, _, _ = org_and_key
    first = await _seed_approved_verification(async_session, org)
    second = await _seed_approved_verification(
        async_session, org, subject_type="business"
    )
    quote, offer = await seed_quote(async_session, org)
    number, _ = await _reject_bought_number(
        async_session, org, monkeypatch, quote, offer
    )
    # Bought before verification_row_id existed.
    meta = dict(number.provisioning_metadata)
    assert number.provisioning_state == "failed"
    meta["offer"] = {**meta["offer"], "verification_row_id": None}
    number.provisioning_metadata = meta
    number.provisioning_state = "pending"
    await async_session.commit()
    from hailhq.api.number_orders import rejected_registration

    await rejected_registration(async_session, number, None)
    # The caller commits (reconcile_order); refresh would drop the change.
    await async_session.commit()
    states = set()
    for row in (first, second):
        await async_session.refresh(row)
        states.add(row.state)
    assert "rejected" in states


async def test_didww_pending_survives_three_days(
    async_session, org_and_key, monkeypatch
):
    """The DID exists but its registration never clears: after 7 days the
    DID is terminated and the setup fee stays."""
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome",
        AsyncMock(return_value=("pending", DID, ORDER)),
    )
    terminate = AsyncMock()
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number", terminate
    )
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
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome",
        AsyncMock(return_value=("pending", None, ORDER)),
    )
    terminate = AsyncMock()
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number", terminate
    )
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
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome",
        AsyncMock(return_value=("pending", DID, ORDER)),
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number",
        AsyncMock(side_effect=RuntimeError("500")),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    await _age(async_session, number, timedelta(days=7, minutes=1))
    await reconcile_order(async_session, number, force=True)
    assert number.provisioning_state == "failed"
    assert await get_balance_cents(async_session, org) == 100000 - 350
    # The DID still renews at the carrier: its id is kept on the row.
    await async_session.refresh(number)
    assert number.provisioning_metadata["unreleased_resource_id"] == DID


async def test_didww_rejected_terminate_failure_keeps_the_did_id(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome", outcome
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number",
        AsyncMock(side_effect=RuntimeError("500")),
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.revoke_registration",
        AsyncMock(return_value=None),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.return_value = ("rejected_registration", DID, ORDER)
    await reconcile_order(async_session, number, force=True)
    await async_session.refresh(number)
    assert number.provisioning_state == "failed"
    assert number.provisioning_metadata["unreleased_resource_id"] == DID
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


async def _mark_checked(db, number, *, ago: timedelta):
    """As if a finished check answered ``ago``."""
    number.provisioning_metadata = {
        **number.provisioning_metadata,
        "last_checked_at": (datetime.now(timezone.utc) - ago).isoformat(),
    }
    await db.commit()


async def test_reconcile_asks_didww_every_fifteen_minutes(
    async_session, org_and_key, monkeypatch
):
    """A registration waits days at DIDWW. Every 15 s would be thousands of
    calls per number that all answer "pending"."""
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    outcome = AsyncMock(return_value=("pending", DID, ORDER))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome", outcome
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.reset_mock()
    await _mark_checked(async_session, number, ago=timedelta(minutes=14))
    await reconcile_order(async_session, number)
    outcome.assert_not_awaited()
    await _mark_checked(async_session, number, ago=timedelta(minutes=16))
    await reconcile_order(async_session, number)
    outcome.assert_awaited_once()


async def test_reconcile_skips_while_another_check_is_running(
    async_session, org_and_key, monkeypatch
):
    """One DIDWW check can take longer than the poll interval. A second run
    must not start meanwhile: both would file a registration for the DID."""
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    outcome = AsyncMock(return_value=("pending", DID, ORDER))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome", outcome
    )
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
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    outcome = AsyncMock(return_value=("pending", DID, ORDER))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome", outcome
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    outcome.reset_mock()
    # Past the check lease and past DIDWW's 15-minute poll interval.
    await _mark_check(async_session, number, started=timedelta(minutes=16))
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
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome", outcome
    )
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
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome", outcome
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number",
        AsyncMock(side_effect=RuntimeError("500")),
    )
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.revoke_registration",
        AsyncMock(return_value=None),
    )
    number = await buy(db, org, row, monkeypatch, offer)
    outcome.return_value = ("rejected_registration", DID, ORDER)
    await reconcile_order(db, number, force=True)
    await db.refresh(number)
    assert number.provisioning_metadata["unreleased_resource_id"] == DID
    return number


async def test_retry_terminates_a_did_left_behind(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    number = await _rejected_with_failed_terminate(async_session, org, monkeypatch)
    release = AsyncMock()
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number", release
    )
    await retry_unreleased_numbers()
    release.assert_awaited_once_with(DID)
    await async_session.refresh(number)
    assert "unreleased_resource_id" not in number.provisioning_metadata
    assert number.provisioning_state == "failed"
    assert await get_balance_cents(async_session, org) == 100000 - 350
    # Nothing is left to retry.
    await retry_unreleased_numbers()
    release.assert_awaited_once()


async def test_retry_keeps_the_did_id_when_the_carrier_fails_again(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    number = await _rejected_with_failed_terminate(async_session, org, monkeypatch)
    release = AsyncMock(side_effect=RuntimeError("500"))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number", release
    )
    await retry_unreleased_numbers()  # never raises
    release.assert_awaited_once_with(DID)
    await async_session.refresh(number)
    assert number.provisioning_metadata["unreleased_resource_id"] == DID


async def test_retry_moves_a_failed_row_to_the_back(
    async_session, org_and_key, monkeypatch
):
    """Rows are picked oldest first, 100 per run. A row that failed again
    must not stay in front, or rows beyond the first 100 never get a turn."""
    org, _, _ = org_and_key
    number = await _rejected_with_failed_terminate(async_session, org, monkeypatch)
    await async_session.execute(
        text("UPDATE phone_numbers SET updated_at = :t WHERE id = :id"),
        {"t": datetime.now(timezone.utc) - timedelta(days=1), "id": number.id},
    )
    await async_session.commit()
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number",
        AsyncMock(side_effect=RuntimeError("500")),
    )
    await retry_unreleased_numbers()
    await async_session.refresh(number)
    assert datetime.now(timezone.utc) - number.updated_at < timedelta(minutes=1)
    assert number.provisioning_metadata["unreleased_resource_id"] == DID


async def test_retry_skips_failed_orders_with_nothing_left_behind(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(side_effect=CarrierRequestError(422)),
    )
    number = await buy(async_session, org, row, monkeypatch, offer)
    assert number.provisioning_state == "failed"
    release = AsyncMock()
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.release_didww_number", release
    )
    await retry_unreleased_numbers()
    release.assert_not_awaited()


async def test_lookup_error_keeps_pending_before_timeout(
    async_session, org_and_key, monkeypatch
):
    org, _, _ = org_and_key
    row, offer = await seed_quote(async_session, org)
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.place_didww_order",
        AsyncMock(return_value=ORDER),
    )
    outcome = AsyncMock(return_value=("pending", None, ORDER))
    monkeypatch.setattr(
        "hailhq.core.providers.voice.didww.didww_order_outcome", outcome
    )
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
        "hailhq.core.providers.voice.didww.place_didww_order",
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
        "hailhq.core.providers.voice.didww.place_didww_order",
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
        "hailhq.core.providers.voice.didww.place_didww_order",
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
        "hailhq.core.providers.voice.didww.place_didww_order",
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


async def test_quotes_pass_the_area_code_to_every_carrier(
    client, org_and_key, monkeypatch
):
    _, _, key = org_and_key
    discover = AsyncMock(return_value=([], []))
    monkeypatch.setattr("hailhq.api.routes.numbers.discover_offers", discover)
    # The test catalogs list PT only; area codes are US/CA only.
    monkeypatch.setattr(
        telephony_catalog, "capabilities", lambda *a: {"voice": True, "sms": False}
    )

    resp = await client.post(
        "/numbers/quotes",
        json={
            "country_code": "US",
            "number_type": "local",
            "capabilities": ["voice"],
            "area_code": "480",
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert discover.await_args.kwargs["area_code"] == "480"


@pytest.mark.parametrize(
    ("country", "typed", "searched"),
    [
        # Typed as dialed at home; carriers match what follows the country code.
        ("GB", "020", "20"),
        ("DE", "030", "30"),
        ("US", "1480", "480"),
        ("US", "480", "480"),
        ("GB", "0131", "131"),
        # Italy keeps its 0 after +39.
        ("IT", "06", "06"),
        # Russia and Kazakhstan dial 8 first, and some area codes start with
        # 8 too: 812 is St Petersburg, not trunk + 12.
        ("RU", "812", "812"),
        ("RU", "8812", "812"),
        ("KZ", "7172", "7172"),
        ("KZ", "87172", "7172"),
        # Both forms are real area codes (8342 Saransk, 342 Perm): the trunk
        # digit wins, because people dial it first.
        ("RU", "8342", "342"),
    ],
)
async def test_quotes_drop_the_trunk_prefix_from_the_area_code(
    client, org_and_key, monkeypatch, country, typed, searched
):
    _, _, key = org_and_key
    discover = AsyncMock(return_value=([], []))
    monkeypatch.setattr("hailhq.api.routes.numbers.discover_offers", discover)
    monkeypatch.setattr(
        telephony_catalog, "capabilities", lambda *a: {"voice": True, "sms": False}
    )
    resp = await client.post(
        "/numbers/quotes",
        json={
            "country_code": country,
            "number_type": "local",
            "capabilities": ["voice"],
            "area_code": typed,
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert discover.await_args.kwargs["area_code"] == searched


async def test_quotes_take_a_us_city_without_a_state(client, org_and_key, monkeypatch):
    """Each offer names its city and state, so the state is optional."""
    _, _, key = org_and_key
    discover = AsyncMock(return_value=([], []))
    monkeypatch.setattr("hailhq.api.routes.numbers.discover_offers", discover)
    monkeypatch.setattr(
        telephony_catalog, "capabilities", lambda *a: {"voice": True, "sms": False}
    )
    resp = await client.post(
        "/numbers/quotes",
        json={
            "country_code": "US",
            "number_type": "local",
            "capabilities": ["voice"],
            "city": "Phoenix",
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert discover.await_args.kwargs["region"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"country_code": "US", "area_code": "4-80"},
        {"country_code": "GB", "area_code": "1234567"},
        {"country_code": "GB", "area_code": ""},
    ],
)
async def test_quotes_reject_a_bad_area_code(client, org_and_key, body):
    _, _, key = org_and_key
    resp = await client.post(
        "/numbers/quotes",
        json={"capabilities": ["voice"], **body},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "area_code"]


async def test_quotes_pass_city_and_region_to_every_carrier(
    client, org_and_key, monkeypatch
):
    _, _, key = org_and_key
    discover = AsyncMock(return_value=([], []))
    monkeypatch.setattr("hailhq.api.routes.numbers.discover_offers", discover)
    monkeypatch.setattr(
        telephony_catalog, "capabilities", lambda *a: {"voice": True, "sms": False}
    )

    resp = await client.post(
        "/numbers/quotes",
        json={
            "country_code": "US",
            "number_type": "local",
            "capabilities": ["voice"],
            "city": " Phoenix ",
            "region": "az",
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert discover.await_args.kwargs["city"] == "Phoenix"
    assert discover.await_args.kwargs["region"] == "AZ"


@pytest.mark.parametrize(
    "body",
    [
        # A state alone is not a search.
        {"country_code": "US", "region": "AZ"},
        {"country_code": "PT", "city": "Lisbon", "region": "LI"},
        {"country_code": "US", "city": "  ", "region": "AZ"},
        {"country_code": "US", "city": "Phoenix", "region": "ARZ"},
        {"country_code": "US", "city": "Phoenix", "region": "ZZ"},
        # A Canadian province is not a US state.
        {"country_code": "US", "city": "Toronto", "region": "ON"},
    ],
)
async def test_quotes_reject_a_bad_city_or_region(client, org_and_key, body):
    _, _, key = org_and_key
    resp = await client.post(
        "/numbers/quotes",
        json={"capabilities": ["voice"], **body},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text


async def test_regions_list_us_states_and_canadian_provinces(client, org_and_key):
    _, _, key = org_and_key
    resp = await client.get(
        "/numbers/regions", headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 200, resp.text
    regions = {
        (r["country_code"], r["code"]): r["name"] for r in resp.json()["regions"]
    }
    assert regions[("US", "AZ")] == "Arizona"
    assert regions[("CA", "ON")] == "Ontario"
    assert regions[("US", "PR")] == "Puerto Rico"
    assert len(regions) == 51 + 5 + 13


async def test_regions_need_an_api_key(client):
    resp = await client.get("/numbers/regions")
    assert resp.status_code == 401


async def test_quotes_take_a_city_alone_outside_us_and_ca(
    client, org_and_key, monkeypatch
):
    _, _, key = org_and_key
    discover = AsyncMock(return_value=([], []))
    monkeypatch.setattr("hailhq.api.routes.numbers.discover_offers", discover)

    resp = await client.post(
        "/numbers/quotes",
        json={"country_code": "PT", "capabilities": ["voice"], "city": "Lisbon"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert discover.await_args.kwargs["city"] == "Lisbon"
    assert discover.await_args.kwargs["region"] is None


async def test_quote_provider_is_ignored(client, org_and_key, monkeypatch):
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
    # ``provider`` is deprecated and ignored: every carrier is searched for
    # the kinds its own catalog lists.
    assert searched == {"local": ["twilio"], "national": ["didww"]}


@pytest.fixture()
def by_request_catalog(tmp_path):
    """PT national: Twilio sells it with a quote, DIDWW lists it by request."""
    row = {
        "country_code": "PT",
        "number_type": "national",
        "usd_per_month": "3.50",
        "voice": True,
        "sms": False,
        "mms": False,
    }
    (tmp_path / "twilio.json").write_text(
        json.dumps({"numbers": [row], "a2p_10dlc": []})
    )
    (tmp_path / "didww.json").write_text(
        json.dumps(
            {"numbers": [{**row, "by_request": True, "verification_required": True}]}
        )
    )
    telephony_catalog._load.cache_clear()


@pytest.fixture()
def didww_verification(monkeypatch):
    """A configured DIDWW verification plug-in (a fake) for quotes and the
    verification routes."""
    from hailhq.api.main import app
    from hailhq.api.routes.verifications import get_verification_registry

    from .test_verifications_api import FakeCarrier

    fake = FakeCarrier()

    def registry(name, purpose="number"):
        return fake if name == "didww" and purpose == "number" else None

    monkeypatch.setattr("hailhq.api.routes.numbers.get_verification_provider", registry)
    app.dependency_overrides[get_verification_registry] = lambda: registry
    yield fake
    app.dependency_overrides.pop(get_verification_registry, None)


async def _quote_pt_national(client, key, monkeypatch, live: list[CarrierOffer]):
    monkeypatch.setattr(
        "hailhq.api.routes.numbers.discover_offers",
        AsyncMock(return_value=(live, [])),
    )
    resp = await client.post(
        "/numbers/quotes",
        json={
            "country_code": "PT",
            "number_type": "national",
            "capabilities": ["voice"],
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.usefixtures("by_request_catalog")
async def test_quotes_list_a_by_request_offer_after_the_offers_to_buy(
    client, async_session, org_and_key, monkeypatch, didww_verification
):
    """Twilio sells PT national with a quote and DIDWW lists it by request:
    both come back. The by-request one has no number, is never recommended,
    and has a stored quote with the DIDWW carrier."""
    _, _, key = org_and_key
    twilio = CarrierOffer(
        provider="twilio",
        e164="+351300000009",
        country_code="PT",
        number_type="national",
        capabilities=["voice"],
        monthly_cents=400,
        setup_cents=0,
        readiness="ready",
    )
    body = await _quote_pt_national(client, key, monkeypatch, [twilio])
    assert [o["by_request"] for o in body["offers"]] == [False, True]
    asked = body["offers"][1]
    assert asked["e164"] is None
    assert asked["readiness"] == "verification_required"
    assert asked["monthly_cents"] == 350
    assert asked["quote_id"]
    assert "provider" not in asked
    assert body["recommended_quote_id"] == body["offers"][0]["quote_id"]
    stored = (
        await async_session.execute(
            select(NumberOffer.offer).where(NumberOffer.id == asked["quote_id"])
        )
    ).scalar_one()
    assert stored["provider"] == "didww" and stored["by_request"] is True


@pytest.mark.usefixtures("by_request_catalog")
async def test_by_request_quote_files_a_verification_with_didww_and_is_not_sold(
    client, async_session, org_and_key, monkeypatch, didww_verification
):
    from .test_verifications_api import _passport, _row_provider, _submission

    _, _, key = org_and_key
    auth = {"Authorization": f"Bearer {key}"}
    body = await _quote_pt_national(client, key, monkeypatch, [])
    (asked,) = body["offers"]
    assert asked["by_request"] is True
    assert body["recommended_quote_id"] is None
    quote_id = asked["quote_id"]
    params = {"country_code": "PT", "number_type": "national", "subject_type": "person"}

    resp = await client.get(
        "/verifications/requirements",
        params={**params, "quote_id": quote_id},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        "/verifications",
        data=_submission(quote_id=quote_id, provider="", **params),
        files=_passport(),
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    assert await _row_provider(async_session, resp.json()["id"]) == "didww"

    # Without the quote_id there is no carrier to pick: 422.
    resp = await client.get("/verifications/requirements", params=params, headers=auth)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["query", "quote_id"]

    # Support orders it by hand: the API never buys it.
    resp = await client.post(
        "/numbers",
        json={"country_code": "PT", "number_type": "national", "quote_id": quote_id},
        headers=auth,
    )
    assert resp.status_code == 422, resp.text
    assert "Ask support" in resp.json()["detail"][0]["msg"]
    assert resp.json()["detail"][0]["loc"] == ["body", "quote_id"]
    assert (
        await async_session.execute(
            select(NumberOffer.number_id).where(NumberOffer.id == quote_id)
        )
    ).scalar_one() is None


@pytest.fixture()
def de_by_request_catalog(tmp_path):
    """DE local: only DIDWW lists it, by request. No carrier sells it."""
    (tmp_path / "twilio.json").write_text(json.dumps({"numbers": [], "a2p_10dlc": []}))
    (tmp_path / "didww.json").write_text(
        json.dumps(
            {
                "numbers": [
                    {
                        "country_code": "DE",
                        "number_type": "local",
                        "usd_per_month": "1.50",
                        "voice": True,
                        "sms": False,
                        "mms": False,
                        "by_request": True,
                        "verification_required": True,
                    }
                ]
            }
        )
    )
    telephony_catalog._load.cache_clear()


@pytest.mark.usefixtures("de_by_request_catalog")
@pytest.mark.parametrize(
    "extra",
    [
        {"area_code": "30", "capabilities": ["voice"]},
        {"city": "Berlin", "capabilities": ["voice"]},
        {"capabilities": ["voice", "sms"]},
    ],
)
async def test_quotes_refuse_a_by_request_kind_when_no_offer_comes_back(
    client, org_and_key, monkeypatch, didww_verification, extra
):
    """A place search or SMS leaves the by-request offer out. No carrier
    sells DE local, so the answer is the old 422, not an empty list."""
    _, _, key = org_and_key
    discover = AsyncMock(return_value=([], []))
    monkeypatch.setattr("hailhq.api.routes.numbers.discover_offers", discover)
    resp = await client.post(
        "/numbers/quotes",
        json={"country_code": "DE", "number_type": "local", **extra},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text
    assert "we don't offer a local number in DE" in resp.json()["detail"][0]["msg"]
    discover.assert_not_awaited()


@pytest.mark.usefixtures("de_by_request_catalog")
async def test_quotes_refuse_a_country_with_only_by_request_kinds_and_no_offer(
    client, org_and_key, monkeypatch, didww_verification
):
    _, _, key = org_and_key
    monkeypatch.setattr(
        "hailhq.api.routes.numbers.discover_offers",
        AsyncMock(return_value=([], [])),
    )
    resp = await client.post(
        "/numbers/quotes",
        json={"country_code": "DE", "area_code": "30", "capabilities": ["voice"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text
    assert "we don't offer numbers in DE" in resp.json()["detail"][0]["msg"]


@pytest.mark.usefixtures("de_by_request_catalog")
async def test_quotes_refuse_a_by_request_kind_when_its_carrier_is_not_configured(
    client, org_and_key, monkeypatch
):
    _, _, key = org_and_key
    monkeypatch.setattr(
        "hailhq.api.routes.numbers.get_verification_provider", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "hailhq.api.routes.numbers.discover_offers",
        AsyncMock(return_value=([], [])),
    )
    resp = await client.post(
        "/numbers/quotes",
        json={"country_code": "DE", "number_type": "local", "capabilities": ["voice"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.usefixtures("de_by_request_catalog")
async def test_quotes_list_a_by_request_kind_no_carrier_sells(
    client, org_and_key, monkeypatch, didww_verification
):
    _, _, key = org_and_key
    monkeypatch.setattr(
        "hailhq.api.routes.numbers.discover_offers",
        AsyncMock(return_value=([], [])),
    )
    resp = await client.post(
        "/numbers/quotes",
        json={"country_code": "DE", "number_type": "local", "capabilities": ["voice"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    (offer,) = resp.json()["offers"]
    assert offer["by_request"] is True and offer["e164"] is None
