"""The credit a number verification sets aside (core/verification_hold.py)."""

from __future__ import annotations

import uuid

from hailhq.core import verification_hold
from hailhq.core.billing import get_balance_cents
from hailhq.core.models import AccountCredit
from sqlalchemy import select

from .conftest import insert_org_and_key


async def _rows(session, org, ref):
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


async def test_refs_name_the_verification() -> None:
    vid = uuid.uuid4()
    assert verification_hold.hold_ref(vid) == f"verification_hold:{vid}"
    assert verification_hold.release_ref(vid) == f"verification_hold_return:{vid}"


async def test_place_then_open_equals_amount(async_session) -> None:
    org, _, _ = await insert_org_and_key(async_session)
    vid = uuid.uuid4()
    await verification_hold.place_hold(async_session, org, vid, 2300, channel="voice")
    await async_session.commit()
    assert await verification_hold.open_hold_cents(async_session, org, vid) == 2300
    (row,) = await _rows(async_session, org, verification_hold.hold_ref(vid))
    assert (row.kind, row.channel, int(row.amount_cents), row.source) == (
        "debit",
        "voice",
        -2300,
        "verification_hold",
    )


async def test_release_returns_amount_once(async_session) -> None:
    org, _, _ = await insert_org_and_key(async_session)
    vid = uuid.uuid4()
    await verification_hold.place_hold(async_session, org, vid, 2300, channel="sms")
    await async_session.commit()
    assert await verification_hold.release_hold(async_session, org, vid) == 2300
    await async_session.commit()
    assert await verification_hold.release_hold(async_session, org, vid) == 0
    await async_session.commit()
    (row,) = await _rows(async_session, org, verification_hold.release_ref(vid))
    assert (row.kind, row.channel, int(row.amount_cents), row.source) == (
        "credit",
        "sms",
        2300,
        "verification_hold_return",
    )
    assert await verification_hold.open_hold_cents(async_session, org, vid) == 0


async def test_release_without_hold_is_zero(async_session) -> None:
    org, _, _ = await insert_org_and_key(async_session)
    vid = uuid.uuid4()
    assert await verification_hold.release_hold(async_session, org, vid) == 0
    await async_session.commit()
    assert await _rows(async_session, org, verification_hold.release_ref(vid)) == []


async def test_balance_drops_by_hold(async_session) -> None:
    org, _, _ = await insert_org_and_key(async_session, initial_credit_cents=5000)
    vid = uuid.uuid4()
    await verification_hold.place_hold(async_session, org, vid, 2300, channel="voice")
    await async_session.commit()
    assert await get_balance_cents(async_session, org) == 2700
    await verification_hold.release_hold(async_session, org, vid)
    await async_session.commit()
    assert await get_balance_cents(async_session, org) == 5000


async def test_open_holds_per_verification(async_session) -> None:
    org, _, _ = await insert_org_and_key(async_session)
    other_org, _, _ = await insert_org_and_key(async_session, org_slug="other")
    held, released, none = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await verification_hold.place_hold(async_session, org, held, 100, channel="voice")
    await verification_hold.place_hold(
        async_session, org, released, 200, channel="voice"
    )
    await async_session.commit()
    await verification_hold.release_hold(async_session, org, released)
    await async_session.commit()
    assert await verification_hold.open_holds(
        async_session, org, [held, released, none]
    ) == {held: 100, released: 0, none: 0}
    # Another organization's ledger never counts.
    assert await verification_hold.open_holds(async_session, other_org, [held]) == {
        held: 0
    }
    assert await verification_hold.open_holds(async_session, org, []) == {}
