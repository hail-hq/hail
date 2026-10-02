"""Text replies by an agent.

``ingest_inbound_sms`` marks an inbound row ``agent_reply_state = 'pending'``
when the number routes texts to a live agent with ``sms_enabled``. The
voicebot's text worker (``hailhq.voicebot.textbot``) claims pending rows,
builds the chat from the agent's instructions plus the recent thread, asks
the LLM for one reply, and sends it through ``POST /internal/agent/reply-sms``.
This module holds the DB side so the worker stays LLM-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from hailhq.core.models import Agent, PhoneNumber, Sms
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "MAX_REPLIES_PER_THREAD",
    "MAX_REPLY_CHARS",
    "TEXT_PREAMBLE",
    "THREAD_LIMIT",
    "THREAD_WINDOW",
    "ClaimedReply",
    "build_chat_messages",
    "claim_pending_reply",
    "finish_reply",
    "replies_in_thread",
    "should_queue_reply",
    "thread_messages",
]

# A text is a text: short, plain, no markdown, no voice stage directions.
TEXT_PREAMBLE = (
    "You are replying by SMS on behalf of the business described below. "
    "Write like a person texting: plain text, no markdown, no lists, no "
    "emoji unless the other side used them. Keep each reply under 300 "
    "characters and answer only what was asked. If you cannot help, say so "
    "and tell the person how to reach a human. Never claim to be a human: if "
    "asked, say you are an AI assistant."
)

MAX_REPLY_CHARS = 480  # same cap as the voice send_sms tool (about 3 segments)
THREAD_LIMIT = 20  # messages of history given to the model
THREAD_WINDOW = timedelta(hours=24)
# After this many agent replies in one thread within THREAD_WINDOW the agent
# goes quiet until a person on the org side writes (a loop breaker).
MAX_REPLIES_PER_THREAD = 20

ReplyState = Literal["done", "skipped", "failed"]


@dataclass(frozen=True)
class ClaimedReply:
    sms: Sms
    agent: Agent
    number: PhoneNumber


async def should_queue_reply(db: AsyncSession, number: PhoneNumber) -> bool:
    """True when this number's text agent exists, is live and answers texts."""
    if number.sms_agent_id is None:
        return False
    agent = await db.get(Agent, number.sms_agent_id)
    return agent is not None and agent.status == "live" and agent.sms_enabled


async def claim_pending_reply(db: AsyncSession) -> ClaimedReply | None:
    """Lock and return the oldest pending inbound text, or None.

    ``FOR UPDATE SKIP LOCKED`` lets several workers poll the same table. The
    caller holds the row until ``finish_reply``; a crash releases the lock and
    the row stays pending for the next poll.
    """
    stmt = (
        select(Sms)
        .where(Sms.direction == "inbound", Sms.agent_reply_state == "pending")
        .order_by(Sms.requested_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    sms = (await db.execute(stmt)).scalar_one_or_none()
    if sms is None:
        return None
    number = await db.get(PhoneNumber, sms.to_number_id) if sms.to_number_id else None
    agent = (
        await db.get(Agent, number.sms_agent_id)
        if number is not None and number.sms_agent_id
        else None
    )
    if (
        number is None
        or agent is None
        or agent.status != "live"
        or not agent.sms_enabled
    ):
        # Routing changed between ingest and now: drop it quietly.
        await finish_reply(db, sms, "skipped")
        return None
    return ClaimedReply(sms=sms, agent=agent, number=number)


def _thread_filter(a: str, b: str):
    return or_(
        and_(Sms.from_e164 == a, Sms.to_e164 == b),
        and_(Sms.from_e164 == b, Sms.to_e164 == a),
    )


async def thread_messages(
    db: AsyncSession, sms: Sms, *, limit: int = THREAD_LIMIT
) -> list[Sms]:
    """The last ``limit`` messages between the two numbers (oldest first),
    including ``sms`` itself, within ``THREAD_WINDOW``."""
    since = datetime.now(timezone.utc) - THREAD_WINDOW
    stmt = (
        select(Sms)
        .where(
            Sms.organization_id == sms.organization_id,
            _thread_filter(sms.from_e164, sms.to_e164),
            Sms.requested_at >= since,
            Sms.requested_at <= sms.requested_at,
        )
        .order_by(Sms.requested_at.desc(), Sms.created_at.desc())
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    rows.reverse()
    return rows


async def replies_in_thread(db: AsyncSession, sms: Sms) -> int:
    """Agent replies already sent in this thread within ``THREAD_WINDOW``."""
    since = datetime.now(timezone.utc) - THREAD_WINDOW
    stmt = select(func.count(Sms.id)).where(
        Sms.organization_id == sms.organization_id,
        Sms.direction == "outbound",
        Sms.agent_id.is_not(None),
        _thread_filter(sms.from_e164, sms.to_e164),
        Sms.requested_at >= since,
    )
    return int((await db.execute(stmt)).scalar_one() or 0)


def build_chat_messages(agent: Agent, history: list[Sms]) -> list[dict[str, Any]]:
    """OpenAI-style messages: system (preamble + instructions) then the
    thread, inbound as ``user`` and outbound as ``assistant``."""
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": f"{TEXT_PREAMBLE}\n\n# Business instructions\n\n{agent.system_prompt}",
        }
    ]
    for row in history:
        role = "user" if row.direction == "inbound" else "assistant"
        messages.append({"role": role, "content": row.body})
    return messages


async def finish_reply(db: AsyncSession, sms: Sms, state: ReplyState) -> None:
    await db.execute(
        update(Sms).where(Sms.id == sms.id).values(agent_reply_state=state)
    )
    await db.commit()


async def mark_pending(db: AsyncSession, sms_id: UUID) -> None:
    await db.execute(
        update(Sms).where(Sms.id == sms_id).values(agent_reply_state="pending")
    )
