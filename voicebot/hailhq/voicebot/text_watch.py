"""Feed a text the caller sends during a call into the live voice session.

The API marks such a text's auto-reply ``skipped`` (see
``hailhq.core.sms_ingest``). This watcher polls ``sms`` for new inbound texts
of the call's thread and hands each to the voice agent as a user message.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from hailhq.core import threads
from hailhq.core.db import session_scope
from hailhq.core.models import Sms
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("hailhq.voicebot")

POLL_SECONDS = 2.0
MAX_INJECT_CHARS = 1000
KEY_LOOKUP_ATTEMPTS = 3
# Covers app/DB clock skew and the ingest transaction stamping requested_at at
# its start. A text inside the overlap may be injected once more than needed.
CURSOR_OVERLAP = timedelta(seconds=30)
# Failed ``generate_reply`` calls (other than a closed session) per text before
# the watcher gives up on it and counts it as delivered.
MAX_INJECT_ATTEMPTS = 3
# ``sms.metadata_["skipped_reason"]`` written by ingest for a text skipped
# because the agent was on a call. Only such rows are injected or requeued.
ACTIVE_CALL_SKIP = "active_call"


async def new_inbound_texts(
    db: AsyncSession,
    organization_id: UUID,
    agent_id: UUID,
    caller_e164: str,
    after: datetime,
    exclude: Collection[UUID] = (),
) -> list[Sms]:
    """Skipped-for-call inbound texts of the thread since ``after``, oldest
    first, leaving out the ids in ``exclude`` (already delivered)."""
    stmt = (
        select(Sms)
        .where(
            Sms.organization_id == organization_id,
            Sms.agent_id == agent_id,
            Sms.direction == "inbound",
            Sms.agent_reply_state == "skipped",
            Sms.metadata_["skipped_reason"].astext == ACTIVE_CALL_SKIP,
            Sms.from_e164 == caller_e164,
            Sms.requested_at >= after,
        )
        .order_by(Sms.requested_at, Sms.id)
    )
    if exclude:
        stmt = stmt.where(Sms.id.not_in(exclude))
    return list((await db.execute(stmt)).scalars().all())


async def watch_incoming_texts(
    session: Any,
    call_id: UUID,
    *,
    since: datetime,
    poll_seconds: float = POLL_SECONDS,
    delivered: set[UUID] | None = None,
) -> None:
    """Run until cancelled. Delivery is at-least-once: every poll re-reads the
    window starting ``CURSOR_OVERLAP`` before ``since`` and skips ids already
    delivered, so no clock skew or commit order can lose a text. A failed poll
    is logged and retried. ``delivered`` is filled with the ids handed to the
    session (the caller passes it in to read it after the call ends); a text
    whose injection fails 3 times is counted as delivered and logged."""
    key = None
    for attempt in range(KEY_LOOKUP_ATTEMPTS):
        try:
            async with session_scope() as db:
                key = await threads.call_thread_key(db, call_id)
            break
        except Exception:
            logger.exception("call_id=%s text watch lookup failed", call_id)
            if attempt == KEY_LOOKUP_ATTEMPTS - 1:
                return
            await asyncio.sleep(poll_seconds)
    if key is None:
        return
    org, agent_id, caller = key
    after = since - CURSOR_OVERLAP
    if delivered is None:
        delivered = set()
    failures: dict[UUID, int] = {}
    while True:
        await asyncio.sleep(poll_seconds)
        try:
            async with session_scope() as db:
                rows = await new_inbound_texts(
                    db, org, agent_id, caller, after, delivered
                )
            for row in rows:
                if row.id in delivered:
                    continue
                try:
                    session.generate_reply(
                        user_input=threads.TEXT_MARKER
                        + (row.body or "")[:MAX_INJECT_CHARS]
                    )
                except RuntimeError:
                    logger.info(
                        "call_id=%s text watch stopped: session closed", call_id
                    )
                    return
                except Exception:
                    failures[row.id] = failures.get(row.id, 0) + 1
                    logger.exception(
                        "call_id=%s text watch inject failed (%d of %d) sms_id=%s",
                        call_id,
                        failures[row.id],
                        MAX_INJECT_ATTEMPTS,
                        row.id,
                    )
                    if failures[row.id] >= MAX_INJECT_ATTEMPTS:
                        delivered.add(row.id)
                    continue
                delivered.add(row.id)
        except Exception:
            logger.exception("call_id=%s text watch poll failed", call_id)


async def requeue_undelivered(
    key: tuple[UUID, UUID, str], since: datetime, delivered_ids: set[UUID]
) -> int:
    """At call end, hand texts the voice agent never got back to the text
    agent: inbound texts of this thread ``skipped`` during the call (requested
    at or after ``since`` minus ``CURSOR_OVERLAP``) and not in ``delivered_ids``
    become ``pending``, only when still ``skipped`` with the active-call marker
    (one conditional UPDATE). Returns how many. ``key`` is ``call_thread_key``."""
    org, agent_id, caller = key
    stmt = update(Sms).where(
        Sms.organization_id == org,
        Sms.agent_id == agent_id,
        Sms.direction == "inbound",
        Sms.from_e164 == caller,
        Sms.agent_reply_state == "skipped",
        Sms.metadata_["skipped_reason"].astext == ACTIVE_CALL_SKIP,
        Sms.requested_at >= since - CURSOR_OVERLAP,
    )
    if delivered_ids:
        stmt = stmt.where(Sms.id.not_in(delivered_ids))
    async with session_scope() as db:
        result = await db.execute(stmt.values(agent_reply_state="pending"))
        await db.commit()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
