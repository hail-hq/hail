"""Feed a text the caller sends during a call into the live voice session.

The API marks such a text's auto-reply ``skipped`` (see
``hailhq.core.sms_ingest``). This watcher polls ``sms`` for new inbound texts
of the call's thread and hands each to the voice agent as a user message.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from hailhq.core import threads
from hailhq.core.db import session_scope
from hailhq.core.models import Sms
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("hailhq.voicebot")

POLL_SECONDS = 2.0
MAX_INJECT_CHARS = 1000
KEY_LOOKUP_ATTEMPTS = 3
# Covers app/DB clock skew and the ingest transaction stamping requested_at at
# its start. A text inside the overlap may be injected once more than needed.
CURSOR_OVERLAP = timedelta(seconds=30)


async def new_inbound_texts(
    db: AsyncSession,
    organization_id: UUID,
    agent_id: UUID,
    caller_e164: str,
    after: datetime,
) -> list[Sms]:
    stmt = (
        select(Sms)
        .where(
            Sms.organization_id == organization_id,
            Sms.agent_id == agent_id,
            Sms.direction == "inbound",
            Sms.from_e164 == caller_e164,
            Sms.requested_at >= after,
        )
        .order_by(Sms.requested_at, Sms.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def watch_incoming_texts(
    session: Any,
    call_id: UUID,
    *,
    since: datetime,
    poll_seconds: float = POLL_SECONDS,
) -> None:
    """Run until cancelled. Delivery is at-least-once: every poll re-reads the
    window starting ``CURSOR_OVERLAP`` before ``since`` and skips ids already
    delivered, so no clock skew or commit order can lose a text. A failed poll
    is logged and retried."""
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
    delivered: set[UUID] = set()
    while True:
        await asyncio.sleep(poll_seconds)
        try:
            async with session_scope() as db:
                rows = await new_inbound_texts(db, org, agent_id, caller, after)
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
                delivered.add(row.id)
        except Exception:
            logger.exception("call_id=%s text watch poll failed", call_id)
