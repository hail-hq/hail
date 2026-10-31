"""Email replies by an agent.

``ingest_inbound`` calls :func:`queue_decision` for each new row. A row that
passes every rule becomes ``agent_reply_state = 'pending'``. The voicebot's text
worker (``hailhq.voicebot.emailbot``) claims it, builds the chat from the agent's
instructions and the thread, asks the LLM for one reply, and sends it through
``POST /internal/agent/reply-email``. The route runs the send-time rules again.

Same claim, lease, retry, and thread cap as ``hailhq.core.text_agent``. This
module holds the DB side so the worker stays LLM-only.
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from hailhq.core.config import settings
from hailhq.core.directory import is_verified_member
from hailhq.core.email_mime import ParsedMime
from hailhq.core.email_sender import from_address_for
from hailhq.core.forward_targets import normalize_address
from hailhq.core.models import Agent, Email, EmailDomain, Sms
from hailhq.core.prompts import build_email_instructions
from hailhq.core.text_agent import (
    CHAT_LIMIT,
    CLAIM_LEASE,
    MAX_ATTEMPTS,
    MAX_REPLIES_PER_THREAD,
    REPLY_CAP_WINDOW,
    RETRY_BACKOFF,
)
from sqlalchemy import and_, any_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("hailhq.core.email_agent")

__all__ = [
    "MAX_REPLY_CHARS",
    "MAX_TOOL_SENDS",
    "ClaimedEmailReply",
    "ReplyDecision",
    "answers_emails",
    "build_chat_messages",
    "claim_pending_reply",
    "email_thread",
    "finish_reply",
    "note_no_funds",
    "queue_decision",
    "replies_in_thread",
    "reply_from_address",
    "reply_references",
    "reply_subject",
    "retry_reply",
    "sender_refusal",
    "thread_lock_key",
    "tool_sends_for_reply",
]

MAX_REPLY_CHARS = 5000  # same cap as the send_email tool
# Characters of one email in the chat. Quoted history makes bodies long.
MAX_EMAIL_CHARS = 6000
# Texts and emails one reply run may send, so one email cannot spend without limit.
MAX_TOOL_SENDS = 3
# SES caps a header value at 995 characters.
MAX_HEADER_CHARS = 900

ReplyState = Literal["done", "skipped", "failed"]

_NO_REPLY_LOCAL = re.compile(
    r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounces?)([+.\-_].*)?$"
)
_MESSAGE_ID = re.compile(r"^<[!-;=?-~]+@[!-;=?-~]+>$")
_TAG = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class ReplyDecision:
    agent: Agent
    state: Literal["pending", "skipped"]
    reason: str | None = None


@dataclass(frozen=True)
class ClaimedEmailReply:
    email: Email
    agent: Agent
    domain: EmailDomain
    attempt: int  # which claim this is, 1-based


def answers_emails(agent: Agent | None) -> bool:
    """True for a live agent with ``email_enabled``: the one test for queueing,
    claiming, and sending an email reply."""
    return agent is not None and agent.status == "live" and agent.email_enabled


def _sender_blocked(
    sender: str, domain: EmailDomain, recipients: Sequence[str], base_domain: str
) -> bool:
    """True for an address that must never get a reply: a daemon, a noreply
    box, Hail itself, or this message's own recipient (a loop)."""
    local, _, host = sender.rpartition("@")
    if not local or not host:
        return True
    if _NO_REPLY_LOCAL.match(local):
        return True
    if base_domain and host == base_domain.lower():
        return True
    return sender == domain.domain.lower() or sender in {
        normalize_address(r) for r in recipients
    }


async def sender_refusal(
    db: AsyncSession,
    domain: EmailDomain,
    *,
    sender: str,
    dmarc_verdict: str | None,
    from_count: int = 1,
) -> str | None:
    """Why this sender may not reach the agent, or None. Fails closed.

    DMARC ``PASS`` is the only proof of a ``From`` address. SES reports ``GRAY``
    when any SPF or DKIM check passed for a domain with no policy or ``p=none``,
    aligned or not, so a forger with a valid signature of their own gets
    ``GRAY``. The agent has tools, so ``members`` accepts nothing less.
    """
    if domain.email_agent_scope == "anyone":
        # A reply to a forged From would reach an innocent third party.
        return "sender_not_verified" if dmarc_verdict == "FAIL" else None
    if from_count != 1 or dmarc_verdict != "PASS":
        return "sender_not_verified"
    if not await is_verified_member(db, domain.organization_id, sender):
        return "sender_not_member"
    return None


async def replies_in_thread(
    db: AsyncSession, organization_id: uuid.UUID, sender: str
) -> int:
    """Agent replies to this sender within ``REPLY_CAP_WINDOW``."""
    since = datetime.now(timezone.utc) - REPLY_CAP_WINDOW
    stmt = select(func.count(Email.id)).where(
        Email.organization_id == organization_id,
        Email.direction == "outbound",
        Email.agent_id.is_not(None),
        Email.requested_at >= since,
        Email.metadata_["reply_to_email_id"].astext.is_not(None),
        sender == any_(Email.to_addresses),
    )
    return int((await db.execute(stmt)).scalar_one() or 0)


async def queue_decision(
    db: AsyncSession,
    domain: EmailDomain,
    *,
    parsed: ParsedMime,
    envelope_from: str,
    recipients: Sequence[str],
    dmarc_verdict: str | None,
    suppress: str | None,
    over_cap: bool,
    funded: bool,
    base_domain: str,
) -> ReplyDecision | None:
    """What to do with a new inbound row. None when no agent answers this
    address. The first failed rule names the skip reason; funds come last, so
    the out-of-credit alert means a reply was really missed."""
    if domain.email_agent_id is None:
        return None
    agent = await db.get(Agent, domain.email_agent_id)
    if not answers_emails(agent):
        return None
    assert agent is not None

    def skip(reason: str) -> ReplyDecision:
        return ReplyDecision(agent=agent, state="skipped", reason=reason)

    if suppress is not None:
        return skip(suppress)
    if over_cap:
        return skip("inbound_rate_limit")
    if parsed.auto_generated or not envelope_from:
        return skip("auto_generated")
    sender = normalize_address(parsed.from_address or "")
    if _sender_blocked(sender, domain, recipients, base_domain):
        return skip("sender_blocked")
    refusal = await sender_refusal(
        db,
        domain,
        sender=sender,
        dmarc_verdict=dmarc_verdict,
        from_count=parsed.from_count,
    )
    if refusal is not None:
        return skip(refusal)
    count = await replies_in_thread(db, domain.organization_id, sender)
    if count >= MAX_REPLIES_PER_THREAD:
        return skip("thread_cap")
    if not funded:
        return skip("insufficient_funds")
    return ReplyDecision(agent=agent, state="pending")


async def _expire_unclaimable(db: AsyncSession, now: datetime) -> None:
    """Close rows that must not be answered: too old, or out of attempts."""
    base = (
        Email.direction == "inbound",
        or_(
            Email.agent_reply_state == "pending",
            and_(
                Email.agent_reply_state == "processing",
                Email.agent_reply_available_at <= now,
            ),
        ),
    )
    cutoff = now - timedelta(seconds=settings.hail_text_reply_max_age_seconds)
    await db.execute(
        update(Email)
        .where(*base, Email.requested_at < cutoff)
        .values(agent_reply_state="skipped")
    )
    await db.execute(
        update(Email)
        .where(*base, Email.agent_reply_attempts >= MAX_ATTEMPTS)
        .values(agent_reply_state="failed")
    )


async def claim_pending_reply(db: AsyncSession) -> ClaimedEmailReply | None:
    """Claim the oldest answerable inbound email that still has an agent, or
    None. Same locking and lease rules as ``text_agent.claim_pending_reply``."""
    now = datetime.now(timezone.utc)
    await _expire_unclaimable(db, now)
    stmt = (
        select(Email)
        .where(
            Email.direction == "inbound",
            or_(
                and_(
                    Email.agent_reply_state == "pending",
                    or_(
                        Email.agent_reply_available_at.is_(None),
                        Email.agent_reply_available_at <= now,
                    ),
                ),
                and_(
                    Email.agent_reply_state == "processing",
                    Email.agent_reply_available_at <= now,
                ),
            ),
        )
        .order_by(Email.requested_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    while True:
        email = (await db.execute(stmt)).scalar_one_or_none()
        if email is None:
            await db.commit()  # keep the expiry updates
            return None
        domain = (
            await db.get(EmailDomain, email.email_domain_id)
            if email.email_domain_id
            else None
        )
        agent = (
            await db.get(Agent, domain.email_agent_id)
            if domain is not None and domain.email_agent_id
            else None
        )
        if domain is not None and agent is not None and answers_emails(agent):
            email.agent_reply_state = "processing"
            email.agent_reply_attempts += 1
            email.agent_reply_available_at = now + CLAIM_LEASE
            attempt = email.agent_reply_attempts
            await db.commit()
            return ClaimedEmailReply(
                email=email, agent=agent, domain=domain, attempt=attempt
            )
        # Routing changed between ingest and now: drop it and take the next row.
        await finish_reply(db, email, "skipped")


async def finish_reply(
    db: AsyncSession, email: Email, state: ReplyState, *, attempt: int | None = None
) -> None:
    """Record the final state. With ``attempt`` it applies only while that
    claim still owns the row."""
    stmt = update(Email).where(Email.id == email.id)
    if attempt is not None:
        stmt = stmt.where(
            Email.agent_reply_state == "processing",
            Email.agent_reply_attempts == attempt,
        )
    await db.execute(stmt.values(agent_reply_state=state))
    await db.commit()


async def retry_reply(db: AsyncSession, claimed: ClaimedEmailReply) -> str | None:
    """A transient error: back to ``pending`` after a backoff, or ``failed``
    once ``MAX_ATTEMPTS`` claims are used. None when the claim lost the row."""
    if claimed.attempt >= MAX_ATTEMPTS:
        await finish_reply(db, claimed.email, "failed", attempt=claimed.attempt)
        return "failed"
    retry_at = datetime.now(timezone.utc) + RETRY_BACKOFF * 2 ** (claimed.attempt - 1)
    result = await db.execute(
        update(Email)
        .where(
            Email.id == claimed.email.id,
            Email.agent_reply_state == "processing",
            Email.agent_reply_attempts == claimed.attempt,
        )
        .values(agent_reply_state="pending", agent_reply_available_at=retry_at)
    )
    await db.commit()
    return "pending" if result.rowcount else None  # type: ignore[attr-defined]


async def note_no_funds(db: AsyncSession, email: Email) -> None:
    """Mark the row so the console shows the same alert as at ingest."""
    meta = dict(email.metadata_ or {})
    reasons = list(meta.get("suppressed_reasons") or [])
    if "insufficient_funds" not in reasons:
        reasons.append("insufficient_funds")
    meta["suppressed_reasons"] = reasons
    meta["agent_skip_reason"] = "insufficient_funds"
    email.metadata_ = meta
    await db.commit()


def thread_lock_key(email: Email) -> str:
    """Advisory-lock key for this sender's thread."""
    return (
        f"reply-email:{email.organization_id}:{normalize_address(email.from_address)}"
    )


def _referenced_ids(email: Email) -> list[str]:
    ids = list(email.references_ids or [])
    if email.in_reply_to:
        ids.append(email.in_reply_to.strip())
    return ids


def _local_part(message_id: str) -> str:
    """SES overrides ``Message-ID`` on outbound mail with its own id at an SES
    host, so a referenced reply is found through ``provider_message_id``."""
    return message_id.strip("<> ").partition("@")[0]


async def email_thread(db: AsyncSession, email: Email) -> list[Email]:
    """The thread up to and including ``email``, oldest first, at most
    ``CHAT_LIMIT`` rows.

    Only rows with this sender count. ``References`` is written by the sender,
    so an unknown sender cannot name someone else's message and read it.
    """
    sender = normalize_address(email.from_address)
    seen: dict[uuid.UUID, Email] = {email.id: email}
    frontier = [email]
    while frontier and len(seen) < CHAT_LIMIT:
        ids = {i for row in frontier for i in _referenced_ids(row)}
        if not ids:
            break
        stmt = select(Email).where(
            Email.organization_id == email.organization_id,
            Email.email_domain_id == email.email_domain_id,
            or_(
                Email.message_id.in_(ids),
                Email.provider_message_id.in_({_local_part(i) for i in ids}),
            ),
            or_(
                and_(
                    Email.direction == "inbound",
                    func.lower(Email.from_address) == sender,
                ),
                and_(Email.direction == "outbound", sender == any_(Email.to_addresses)),
            ),
        )
        rows = (await db.execute(stmt.limit(CHAT_LIMIT))).scalars().all()
        frontier = [r for r in rows if r.id not in seen]
        seen.update({r.id: r for r in frontier})
    earlier = [row for row in seen.values() if row.id != email.id]
    earlier.sort(key=lambda r: r.requested_at)
    return earlier[-(CHAT_LIMIT - 1) :] + [email]


def _plain_body(email: Email) -> str:
    body = email.body_text
    if not (body or "").strip() and email.body_html:
        body = html.unescape(_TAG.sub(" ", email.body_html))
    return (body or "").strip()[:MAX_EMAIL_CHARS]


def build_chat_messages(
    agent: Agent, thread: list[Email], *, member: bool
) -> list[dict[str, Any]]:
    """OpenAI-style messages: the system message, then the thread. Inbound
    mail is ``user``, labeled so the model reads it as mail, and replies are
    ``assistant``."""
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": build_email_instructions(agent.system_prompt, member=member),
        }
    ]
    for row in thread:
        if row.direction == "inbound":
            text = (
                f"Email from {row.from_address}\n"
                f"Subject: {row.subject}\n\n{_plain_body(row)}"
            )
            messages.append({"role": "user", "content": text})
        else:
            messages.append({"role": "assistant", "content": _plain_body(row)})
    return messages


def reply_subject(subject: str | None) -> str:
    clean = " ".join((subject or "").split())
    if clean.lower().startswith("re:"):
        return clean
    return f"Re: {clean}" if clean else "Re: your email"


def reply_from_address(domain: EmailDomain, email: Email) -> str:
    """The address the mail reached. A custom domain takes any local part."""
    if domain.kind == "hail_mail":
        return domain.domain
    suffix = f"@{domain.domain.lower()}"
    for address in [*email.to_addresses, *(email.cc_addresses or [])]:
        if address.lower().endswith(suffix):
            return address
    return from_address_for(domain)


def reply_references(email: Email) -> tuple[str | None, list[str] | None]:
    """``(In-Reply-To, References)`` for a reply. A malformed id is dropped: it
    came from the sender and goes into a header. The first id and the newest
    ones stay when the list is too long for one header."""
    message_id = (email.message_id or "").strip()
    if not _MESSAGE_ID.match(message_id):
        return None, None
    chain = [r for r in (email.references_ids or []) if _MESSAGE_ID.match(r)]
    chain.append(message_id)
    while len(" ".join(chain)) > MAX_HEADER_CHARS and len(chain) > 2:
        del chain[1]
    if len(" ".join(chain)) > MAX_HEADER_CHARS:
        chain = chain[-1:]
    return message_id, chain


async def tool_sends_for_reply(db: AsyncSession, inbound_id: uuid.UUID) -> int:
    """Emails and texts this reply run sent with its tools."""
    total = 0
    for model in (Email, Sms):
        stmt = select(func.count(model.id)).where(
            model.direction == "outbound",
            model.metadata_["inbound_email_id"].astext == str(inbound_id),
        )
        total += int((await db.execute(stmt)).scalar_one() or 0)
    return total
