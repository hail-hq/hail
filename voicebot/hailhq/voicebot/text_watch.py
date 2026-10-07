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
from sqlalchemy import Text, cast, func, literal, literal_column, select, update
from sqlalchemy.dialects.postgresql import JSONB
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
ACTIVE_CALL_SKIP = threads.ACTIVE_CALL_SKIP


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
    poll_seconds: float = POLL_SECONDS,
    delivered: set[UUID] | None = None,
) -> None:
    """Run until cancelled. Delivery is at-least-once: every poll re-reads the
    window starting ``CURSOR_OVERLAP`` before the call row was created and skips
    ids already delivered, so no clock skew or commit order can lose a text. A failed poll
    is logged and retried. A delivered text is marked ``done`` in the database
    (so no later call or requeue touches it) and its id is added to
    ``delivered``; a text whose injection fails 3 times is counted as delivered
    and logged, but stays ``skipped`` so the call-end requeue gives it to the
    text agent."""
    ctx = None
    for attempt in range(KEY_LOOKUP_ATTEMPTS):
        try:
            async with session_scope() as db:
                ctx = await threads.call_thread_context(db, call_id)
            break
        except Exception:
            logger.exception("call_id=%s text watch lookup failed", call_id)
            if attempt == KEY_LOOKUP_ATTEMPTS - 1:
                return
            await asyncio.sleep(poll_seconds)
    if ctx is None:
        return
    org, agent_id, caller = ctx.organization_id, ctx.agent_id, ctx.caller_e164
    after = ctx.created_at - CURSOR_OVERLAP
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
                await mark_delivered(row.id, call_id)
        except Exception:
            logger.exception("call_id=%s text watch poll failed", call_id)


async def mark_delivered(sms_id: UUID, call_id: UUID) -> None:
    """A text handed to the live call: ``done``, only while it is still
    ``skipped`` for a call, with ``delivered_to_call`` noted. Best effort: a
    failure is logged and the in-memory ``delivered`` set still protects this
    call."""
    meta = Sms.metadata_.op("||")(
        func.jsonb_build_object(
            literal_column("'delivered_to_call'"), cast(literal(str(call_id)), Text)
        )
    )
    try:
        async with session_scope() as db:
            await db.execute(
                update(Sms)
                .where(
                    Sms.id == sms_id,
                    Sms.agent_reply_state == "skipped",
                    Sms.metadata_["skipped_reason"].astext == ACTIVE_CALL_SKIP,
                )
                .values(agent_reply_state="done", metadata_=cast(meta, JSONB))
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    except Exception:
        logger.exception("call_id=%s marking sms_id=%s done failed", call_id, sms_id)
