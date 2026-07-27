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

from hailhq.core.models import PhoneNumber
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

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


async def sweep_pool_reservations(
    session: AsyncSession, *, grace_seconds: int
) -> list[uuid.UUID]:
    """Force-release stuck pool reservations. Idempotent backstop.

    Three release conditions fold into one UPDATE:

    1. **Missed release** — the call reached a terminal status but
       :func:`release_pool_reservation` never fired (process crash mid-
       transaction, an exception in ``finalize_call`` before the helper
       was reached, etc.). The Call row already says ``completed`` /
       ``failed`` / ``busy`` / ``no_answer`` / ``canceled``; the sweeper
       just unwinds the dangling reservation.

    2. **Hard backstop** — neither the API nor the voicebot ever wrote a
       terminal status. ``now() > requested_at + max_duration_seconds +
       grace_seconds`` means the call cannot legitimately still be
       running: the voicebot enforces ``max_duration_seconds`` server-
       side, plus a configurable grace window absorbs LiveKit/Twilio
       teardown + clock skew.

    3. **Orphan FK** — ``reserved_call_id`` points at a Call row that no
       longer exists. The FK has ``ON DELETE SET NULL`` so this shouldn't
       normally happen, but a manual cleanup or a future cascade race
       could leave one. Belt-and-suspenders.

    Returns the list of ``phone_numbers.id`` values that were released
    so the caller can log them. Force-releases should be rare; surfacing
    them is the signal for operational investigation.
    """
    stmt = text("""
        UPDATE phone_numbers pn
           SET reserved_call_id = NULL
         WHERE pn.is_pool = TRUE
           AND pn.reserved_call_id IS NOT NULL
           AND (
             EXISTS (
               SELECT 1 FROM calls c
                WHERE c.id = pn.reserved_call_id
                  AND c.status IN :terminal_statuses
             )
             OR EXISTS (
               SELECT 1 FROM calls c
                WHERE c.id = pn.reserved_call_id
                  AND c.max_duration_seconds IS NOT NULL
                  AND now() > c.requested_at
                              + make_interval(secs => (
                                  c.max_duration_seconds + :grace_s
                                )::int)
             )
             OR NOT EXISTS (
               SELECT 1 FROM calls c WHERE c.id = pn.reserved_call_id
             )
           )
         RETURNING pn.id
        """).bindparams(bindparam("terminal_statuses", expanding=True))
    result = await session.execute(
        stmt,
        {
            "grace_s": grace_seconds,
            "terminal_statuses": list(TERMINAL_CALL_STATUSES),
        },
    )
    return [row[0] for row in result.fetchall()]


__all__ = [
    "CALL_META_FROM_POOL",
    "claim_pool_number",
    "release_pool_reservation",
    "sweep_pool_reservations",
]
