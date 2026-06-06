"""Stale-call reconciler: a backstop that force-closes stuck calls.

A call's terminal status is normally written on the hot path by the voicebot's
``on_call_end`` shutdown callback. When that callback never runs — a worker
crash, a dropped LiveKit room-teardown, an exception before finalization — the
call row pins at a non-terminal status (``queued`` / ``dialing`` / ``ringing``
/ ``in_progress``) indefinitely, which breaks status polling and leaks the
call from completion/duration accounting.

:func:`sweep_stale_calls` is the deterministic backstop. It reuses the same
bound the pool sweeper uses (see :func:`hailhq.core.pool.sweep_pool_reservations`):
a call is "stuck" once ``now() > COALESCE(started_at, requested_at) +
max_duration_seconds + grace``. The voicebot enforces ``max_duration_seconds``
server-side, so anything past that bound plus a teardown/clock-skew grace
cannot legitimately still be running. Such rows are failed with
``end_reason='sweeper_timeout'`` (an existing ``call_end_reason`` ENUM member).

Run alongside the pool sweeper in the API service's periodic loop. Ordering it
*before* the pool sweep means a call it force-closes here is seen as terminal
by the pool sweep in the same tick, so its reservation is released immediately.
"""

from __future__ import annotations

import uuid

from sqlalchemy import bindparam, func, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.call_end_reasons import CallEndReason
from hailhq.core.models import Call, CallEvent
from hailhq.core.schemas import TERMINAL_CALL_STATUSES


async def sweep_stale_calls(
    session: AsyncSession, *, grace_seconds: int
) -> list[uuid.UUID]:
    """Force-close non-terminal calls past their max-duration bound.

    Returns the list of ``calls.id`` values transitioned to ``failed`` so the
    caller can log them — force-closes should be rare, and surfacing them is
    the signal for operational investigation.

    Two passes in one transaction: a ``SELECT ... FOR UPDATE SKIP LOCKED`` to
    snapshot the stuck rows (and their prior status, for an accurate
    ``state_change`` event), then a single bulk ``UPDATE``. ``SKIP LOCKED``
    keeps the sweep from fighting the voicebot's ``on_call_end`` over the same
    row: whichever grabs the lock first wins and the other side no-ops (the
    sweep skips it; ``on_call_end``'s write would land on an already-terminal
    row). The terminal guard (``status NOT IN terminal``) makes a redundant
    sweep idempotent.
    """
    select_stmt = text("""
        SELECT c.id, c.status
          FROM calls c
         WHERE c.status NOT IN :terminal_statuses
           AND c.max_duration_seconds IS NOT NULL
           AND now() > COALESCE(c.started_at, c.requested_at)
                       + make_interval(secs => (
                           c.max_duration_seconds + :grace_s
                         )::int)
         FOR UPDATE SKIP LOCKED
        """).bindparams(bindparam("terminal_statuses", expanding=True))
    rows = (
        await session.execute(
            select_stmt,
            {
                "grace_s": grace_seconds,
                "terminal_statuses": list(TERMINAL_CALL_STATUSES),
            },
        )
    ).fetchall()
    if not rows:
        return []

    ids = [row[0] for row in rows]
    await session.execute(
        update(Call)
        .where(Call.id.in_(ids))
        .values(
            status="failed",
            end_reason=CallEndReason.SWEEPER_TIMEOUT.value,
            ended_at=func.now(),
        )
    )
    for call_id, prior_status in rows:
        session.add(
            CallEvent(
                call_id=call_id,
                kind="state_change",
                payload={
                    "from": prior_status,
                    "to": "failed",
                    "reason": CallEndReason.SWEEPER_TIMEOUT.value,
                },
            )
        )
    return ids


__all__ = ["sweep_stale_calls"]
