"""Shared phone-number pool: claim and release helpers.

Pool numbers (``phone_numbers.is_pool = TRUE``, ``organization_id IS NULL``)
are shared across orgs that have no provisioned number of their own.
``reserved_call_id`` is the single source of truth for "this number is in
use right now" — there is no separate enum or boolean.

Two callers exercise these helpers:

* :mod:`hailhq.api.routes.calls` calls :func:`claim_pool_number` when an
  org has no active number, and calls :func:`release_pool_reservation`
  from its dispatch-failure handler so a failed call doesn't leak a slot.
* :mod:`hailhq.voicebot.agent` calls :func:`release_pool_reservation`
  from its ``finalize_call`` path the moment a call reaches a terminal
  status.

Both can fire safely without coordination: :func:`release_pool_reservation`
is idempotent — the WHERE clause matches at most one row and matches
nothing on a second invocation. A periodic sweeper (separate task) provides
the deterministic backstop using ``Call.max_duration_seconds + grace``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import PhoneNumber

# Metadata key stamped on Call.metadata_ when the call drew from the shared
# pool. Read by tests, the voicebot, and any external consumer that needs
# to distinguish pool-backed calls from org-owned ones.
CALL_META_FROM_POOL = "from_pool"


async def claim_pool_number(session: AsyncSession) -> PhoneNumber | None:
    """Lock one available pool number — returns the row without yet
    binding it to a call.

    Uses ``SELECT … FOR UPDATE SKIP LOCKED`` so concurrent claims never
    fight over the same row — losers skip to the next available number.
    Returns the locked :class:`PhoneNumber` row on success (lock held
    until the caller commits or rolls back), or ``None`` when the pool
    is exhausted.

    ``ORDER BY random()`` distributes traffic across the pool instead of
    hammering whichever row the planner happens to return first; this
    spreads carrier wear and caller-ID reputation risk evenly.

    Why the split: the FK ``phone_numbers.reserved_call_id → calls(id)``
    is not deferrable, so the reservation can only be set after the
    Call row exists. Typical caller flow inside one transaction:

        pool = await claim_pool_number(db)
        if pool is None:
            raise HTTPException(503, "pool exhausted")
        call = Call(from_number_id=pool.id, ...)
        db.add(call); await db.flush()        # ← call.id materializes here
        pool.reserved_call_id = call.id       # ← bind reservation
        await db.commit()                     # ← both persisted atomically

    A rollback unwinds both the Call insert and the reservation; the
    row-level lock is released by the rollback, freeing the number for
    the next claimant.
    """
    stmt = (
        select(PhoneNumber)
        .where(
            PhoneNumber.is_pool.is_(True),
            PhoneNumber.provisioning_state == "active",
            PhoneNumber.reserved_call_id.is_(None),
        )
        .order_by(func.random())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def release_pool_reservation(
    session: AsyncSession, *, call_id: uuid.UUID
) -> bool:
    """Idempotently release the pool reservation held by ``call_id``.

    Returns ``True`` iff a row was actually released; ``False`` means
    either the call never held a pool reservation, or the reservation
    was already cleared. Both outcomes are safe — callers should invoke
    this unconditionally on terminal status transitions.

    The ``reserved_call_id`` predicate is the entire idempotency story:
    a second call after release matches zero rows. The helper deliberately
    does not look at ``provisioning_state`` or ``is_pool`` — it works on
    whatever row currently points at ``call_id``, which by construction
    must be a pool row (only pool numbers ever receive a reservation).
    """
    stmt = (
        update(PhoneNumber)
        .where(PhoneNumber.reserved_call_id == call_id)
        .values(reserved_call_id=None)
    )
    result = await session.execute(stmt)
    return (result.rowcount or 0) > 0


__all__ = ["CALL_META_FROM_POOL", "claim_pool_number", "release_pool_reservation"]
