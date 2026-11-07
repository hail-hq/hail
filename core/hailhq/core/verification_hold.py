"""Credit set aside while a number verification is checked.

A billed number verification debits setup plus the first month
(``verification_hold:<id>``) when it starts. The amount comes back
(``verification_hold_return:<id>``) when the verification is rejected or
cancelled, when a number is bought with it (the purchase then charges the
number the usual way), or when it stays approved and unused too long.

The ledger has no unique index on ``ref``, so a release locks the hold row
first: two releases of one verification run one after the other, and the
second sees the first one's return row. The caller commits.
"""

from __future__ import annotations

from uuid import UUID

from hailhq.core.models import AccountCredit
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

HOLD_SOURCE = "verification_hold"
RELEASE_SOURCE = "verification_hold_return"

__all__ = [
    "HOLD_SOURCE",
    "RELEASE_SOURCE",
    "hold_ref",
    "open_hold_cents",
    "open_holds",
    "place_hold",
    "release_hold",
    "release_ref",
]


def hold_ref(verification_id: UUID) -> str:
    return f"{HOLD_SOURCE}:{verification_id}"


def release_ref(verification_id: UUID) -> str:
    return f"{RELEASE_SOURCE}:{verification_id}"


async def place_hold(
    db: AsyncSession,
    org: UUID,
    verification_id: UUID,
    amount_cents: int,
    *,
    channel: str,
) -> None:
    """Add the debit row. ``channel`` is 'voice' or 'sms', the same rule as a
    number order's hold, so the website files both under numbers. The
    caller checks the balance under ``org_lock`` and commits."""
    db.add(
        AccountCredit(
            organization_id=org,
            kind="debit",
            channel=channel,
            amount_cents=-amount_cents,
            qty=1,
            ref=hold_ref(verification_id),
            source=HOLD_SOURCE,
        )
    )


async def release_hold(db: AsyncSession, org: UUID, verification_id: UUID) -> int:
    """Add the return row for an open hold. Returns the cents released: 0
    when there is no hold (a verification made before holds existed, a
    shared key) or it was already released. Locks the hold row until the
    caller's transaction ends."""
    hold = (
        await db.execute(
            select(AccountCredit)
            .where(
                AccountCredit.organization_id == org,
                AccountCredit.ref == hold_ref(verification_id),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if hold is None:
        return 0
    released = (
        await db.execute(
            select(AccountCredit.id).where(
                AccountCredit.organization_id == org,
                AccountCredit.ref == release_ref(verification_id),
            )
        )
    ).first()
    if released is not None:
        return 0
    amount = -int(hold.amount_cents)
    db.add(
        AccountCredit(
            organization_id=org,
            kind="credit",
            channel=hold.channel,
            amount_cents=amount,
            qty=1,
            ref=release_ref(verification_id),
            source=RELEASE_SOURCE,
        )
    )
    await db.flush()
    return amount


async def open_hold_cents(db: AsyncSession, org: UUID, verification_id: UUID) -> int:
    return (await open_holds(db, org, [verification_id]))[verification_id]


async def open_holds(
    db: AsyncSession, org: UUID, verification_ids: list[UUID]
) -> dict[UUID, int]:
    """The open hold of each verification, in cents (0 when none)."""
    if not verification_ids:
        return {}
    by_ref = {hold_ref(v): v for v in verification_ids}
    by_ref.update({release_ref(v): v for v in verification_ids})
    rows = await db.execute(
        select(AccountCredit.ref, func.sum(AccountCredit.amount_cents))
        .where(
            AccountCredit.organization_id == org,
            AccountCredit.ref.in_(list(by_ref)),
        )
        .group_by(AccountCredit.ref)
    )
    out = dict.fromkeys(verification_ids, 0)
    for ref, total in rows:
        out[by_ref[ref]] -= int(total)
    return out
