"""A number verification needs setup plus the first month in credit. The
amount is set aside when it starts and comes back when it is rejected,
cancelled or left unused (core/verification_hold.py)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from hailhq.api.main import app
from hailhq.api.routes import verifications as verifications_routes
from hailhq.api.routes.verifications import get_verification_registry
from hailhq.core import verification_hold
from hailhq.core.billing import get_balance_cents
from hailhq.core.config import SELF_HOSTED_ORG_ID, settings
from hailhq.core.db import session_scope
from hailhq.core.models import AccountCredit, CarrierVerification, NumberOffer
from sqlalchemy import delete, select

from .conftest import insert_org_and_key
from .test_verifications_api import (
    FakeCarrier,
    _auth,
    _passport,
)

SETUP, MONTHLY = 1500, 800
NEEDED = SETUP + MONTHLY


async def _quote(session, org, *, by_request=False) -> str:
    quote = NumberOffer(
        organization_id=org,
        offer={
            "provider": "fake",
            "e164": None if by_request else "+447700900001",
            "country_code": "GB",
            "number_type": "mobile",
            "capabilities": ["voice", "sms"],
            "monthly_cents": MONTHLY,
            "setup_cents": SETUP,
            "by_request": by_request,
        },
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(quote)
    await session.commit()
    return str(quote.id)


@pytest.fixture()
def carrier():
    fake = FakeCarrier()
    app.dependency_overrides[get_verification_registry] = lambda: (
        lambda name, purpose="number": (
            fake if name == "fake" and purpose == "number" else None
        )
    )
    verifications_routes._last_polled.clear()
    yield fake
    app.dependency_overrides.pop(get_verification_registry, None)


async def _org(session, balance: int):
    org, _, key = await insert_org_and_key(session, initial_credit_cents=balance)
    return org, key


async def _create(client, key, quote_id, first="Ada", last="Lovelace"):
    import json

    return await client.post(
        "/verifications",
        data={
            "quote_id": quote_id,
            "country_code": "GB",
            "number_type": "mobile",
            "subject_type": "person",
            "fields": json.dumps({"first_name": first, "last_name": last}),
            "documents": json.dumps({"proof_of_identity": {"option": "passport"}}),
        },
        files=_passport(),
        headers=_auth(key),
    )


async def _ledger(session, org, ref) -> list[AccountCredit]:
    session.expire_all()
    return (
        (
            await session.execute(
                select(AccountCredit).where(
                    AccountCredit.organization_id == org, AccountCredit.ref == ref
                )
            )
        )
        .scalars()
        .all()
    )


async def _rows(session, org) -> list[CarrierVerification]:
    session.expire_all()
    return (
        (
            await session.execute(
                select(CarrierVerification).where(
                    CarrierVerification.organization_id == org
                )
            )
        )
        .scalars()
        .all()
    )


# -- the gate and the hold ------------------------------------------------


async def test_number_verification_402_below_needed(
    client, async_session, carrier
) -> None:
    org, key = await _org(async_session, NEEDED - 1)
    resp = await _create(client, key, await _quote(async_session, org))
    assert resp.status_code == 402, resp.text
    assert resp.json()["detail"] == (
        "not enough credit to start verification; setup and the first month "
        "cost $23.00; add credit first"
    )
    assert carrier.drafts == []
    assert await _rows(async_session, org) == []


async def test_number_verification_holds_needed(client, async_session, carrier) -> None:
    org, key = await _org(async_session, NEEDED)
    resp = await _create(client, key, await _quote(async_session, org))
    assert resp.status_code == 201, resp.text
    vid = uuid.UUID(resp.json()["id"])
    (hold,) = await _ledger(async_session, org, verification_hold.hold_ref(vid))
    assert (int(hold.amount_cents), hold.kind, hold.source, hold.channel) == (
        -NEEDED,
        "debit",
        "verification_hold",
        "voice",
    )
    assert await get_balance_cents(async_session, org) == 0


async def test_shared_key_no_hold(client, async_session, carrier, monkeypatch) -> None:
    monkeypatch.setattr(settings, "hail_api_key", "hail-test-shared-secret")
    # Below the amount: the shared key is not billed.
    await async_session.execute(
        delete(AccountCredit).where(AccountCredit.organization_id == SELF_HOSTED_ORG_ID)
    )
    await async_session.commit()
    quote_id = await _quote(async_session, SELF_HOSTED_ORG_ID)
    resp = await _create(client, "hail-test-shared-secret", quote_id)
    assert resp.status_code == 201, resp.text
    vid = uuid.UUID(resp.json()["id"])
    assert (
        await _ledger(
            async_session, SELF_HOSTED_ORG_ID, verification_hold.hold_ref(vid)
        )
        == []
    )


async def test_by_request_offer_no_hold(client, async_session, carrier) -> None:
    org, key = await _org(async_session, 0)
    resp = await _create(client, key, await _quote(async_session, org, by_request=True))
    assert resp.status_code == 201, resp.text
    vid = uuid.UUID(resp.json()["id"])
    assert await _ledger(async_session, org, verification_hold.hold_ref(vid)) == []


class _SpendingCarrier(FakeCarrier):
    """Spends the credit while the carrier builds the draft: the balance
    read before the draft passes, the one at save time does not."""

    def __init__(self, org) -> None:
        super().__init__()
        self.org = org

    async def create_draft(self, **kwargs):
        async with session_scope() as other:
            other.add(
                AccountCredit(
                    organization_id=self.org,
                    kind="debit",
                    channel="voice",
                    amount_cents=-100,
                    qty=1,
                    ref=f"test-spend:{uuid.uuid4()}",
                    source="test",
                )
            )
            await other.commit()
        return await super().create_draft(**kwargs)


async def test_late_402_discards_draft(client, async_session) -> None:
    org, key = await _org(async_session, NEEDED)
    fake = _SpendingCarrier(org)
    app.dependency_overrides[get_verification_registry] = lambda: (
        lambda name, purpose="number": fake if name == "fake" else None
    )
    try:
        resp = await _create(client, key, await _quote(async_session, org))
    finally:
        app.dependency_overrides.pop(get_verification_registry, None)
    assert resp.status_code == 402, resp.text
    assert len(fake.drafts) == 1
    assert fake.discarded == [{"bundle_sid": "B1"}]
    assert await _rows(async_session, org) == []
    holds = (
        (
            await async_session.execute(
                select(AccountCredit).where(
                    AccountCredit.organization_id == org,
                    AccountCredit.source == "verification_hold",
                )
            )
        )
        .scalars()
        .all()
    )
    assert holds == []


async def test_retry_one_hold(client, async_session, carrier) -> None:
    org, key = await _org(async_session, 3 * NEEDED)
    quote_id = await _quote(async_session, org)
    first = await _create(client, key, quote_id)
    again = await _create(client, key, quote_id)
    assert first.status_code == 201, first.text
    assert again.status_code == 409, again.text
    holds = (
        (
            await async_session.execute(
                select(AccountCredit).where(
                    AccountCredit.organization_id == org,
                    AccountCredit.source == "verification_hold",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(holds) == 1
    assert await get_balance_cents(async_session, org) == 2 * NEEDED


# -- the hold comes back ----------------------------------------------------


async def _held(client, async_session, carrier, balance=NEEDED):
    """An org whose verification set NEEDED aside: (org, key, vid)."""
    org, key = await _org(async_session, balance)
    resp = await _create(client, key, await _quote(async_session, org))
    assert resp.status_code == 201, resp.text
    return org, key, uuid.UUID(resp.json()["id"])


async def _returned(async_session, org, vid) -> int:
    rows = await _ledger(async_session, org, verification_hold.release_ref(vid))
    assert len(rows) <= 1
    return int(rows[0].amount_cents) if rows else 0


async def _set(async_session, vid, **values) -> None:
    from sqlalchemy import update

    await async_session.execute(
        update(CarrierVerification)
        .where(CarrierVerification.id == vid)
        .values(**values)
    )
    await async_session.commit()


def _superadmin() -> None:
    from types import SimpleNamespace

    from hailhq.api.superadmin import require_superadmin

    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )


@pytest.fixture(autouse=True)
def _no_superadmin():
    yield
    from hailhq.api.superadmin import require_superadmin

    app.dependency_overrides.pop(require_superadmin, None)


async def test_release_on_carrier_reject(client, async_session, carrier) -> None:
    from hailhq.core.providers.verification import ProviderStatus

    org, key, vid = await _held(client, async_session, carrier)
    carrier.remote_status = ProviderStatus(state="rejected", reason="ID unreadable")
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "rejected"
    assert await _returned(async_session, org, vid) == NEEDED
    assert await get_balance_cents(async_session, org) == NEEDED
    # Dismissing it afterwards gives nothing back twice.
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "cancelled"
    assert await _returned(async_session, org, vid) == NEEDED
    assert await get_balance_cents(async_session, org) == NEEDED


async def test_release_on_submit_refusal(client, async_session, carrier) -> None:
    from hailhq.core.providers.verification import Problem, SubmitRefused

    async def refuse(refs):
        raise SubmitRefused([Problem(field="", message="not accepted")])

    carrier.submit = refuse
    org, _, vid = await _held(client, async_session, carrier)
    (row,) = await _rows(async_session, org)
    assert row.state == "rejected"
    assert await _returned(async_session, org, vid) == NEEDED


async def test_release_on_admin_reject(client, async_session, carrier) -> None:
    org, key, vid = await _held(client, async_session, carrier)
    await _set(async_session, vid, state="awaiting_review")
    _superadmin()
    resp = await client.post(
        f"/admin/verifications/{vid}/reject",
        json={"reason": "ID unreadable"},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    assert await _returned(async_session, org, vid) == NEEDED


async def test_release_on_cancel(client, async_session, carrier) -> None:
    org, key, vid = await _held(client, async_session, carrier)
    await _set(async_session, vid, state="awaiting_review")
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert await _returned(async_session, org, vid) == NEEDED
    assert await get_balance_cents(async_session, org) == NEEDED


async def _sweep() -> None:
    async with session_scope() as db:
        await verifications_routes.sweep_verifications(
            db, lambda name, purpose="number": None
        )


async def test_release_30_days_after_approval_without_number(
    client, async_session, carrier
) -> None:
    org, _, vid = await _held(client, async_session, carrier)
    approved = datetime.now(timezone.utc) - timedelta(days=31)
    await _set(
        async_session, vid, state="approved", approved_at=approved, updated_at=approved
    )
    await _sweep()
    assert await _returned(async_session, org, vid) == NEEDED
    await _sweep()
    assert await _returned(async_session, org, vid) == NEEDED
    assert await get_balance_cents(async_session, org) == NEEDED


async def test_no_release_before_30_days(client, async_session, carrier) -> None:
    org, _, vid = await _held(client, async_session, carrier)
    approved = datetime.now(timezone.utc) - timedelta(days=29)
    await _set(
        async_session, vid, state="approved", approved_at=approved, updated_at=approved
    )
    await _sweep()
    assert await _returned(async_session, org, vid) == 0
    assert await get_balance_cents(async_session, org) == 0


async def test_reject_then_resubmit_passes(client, async_session, carrier) -> None:
    from hailhq.core.providers.verification import ProviderStatus

    org, key, vid = await _held(client, async_session, carrier)
    carrier.remote_status = ProviderStatus(state="rejected", reason="ID unreadable")
    await client.get(f"/verifications/{vid}", headers=_auth(key))
    carrier.remote_status = ProviderStatus(state="pending")
    resp = await _create(client, key, await _quote(async_session, org))
    assert resp.status_code == 201, resp.text
    assert await get_balance_cents(async_session, org) == 0
