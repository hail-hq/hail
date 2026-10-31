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

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Literal

from hailhq.core.billing import has_funds
from hailhq.core.config import settings
from hailhq.core.directory import is_verified_member
from hailhq.core.email_mime import ParsedMime
from hailhq.core.email_sender import from_address_for
from hailhq.core.forward_targets import normalize_address
from hailhq.core.models import Agent, Email, EmailDomain, Sms
from hailhq.core.prompts import SenderTrust, build_email_instructions
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
    "is_authenticated",
    "proves_sender",
    "queue_decision",
    "replies_in_thread",
    "reply_from_address",
    "reply_references",
    "reply_subject",
    "retry_reply",
    "run_refusal",
    "sender_refusal",
    "sender_trust",
    "stamp_skip",
    "thread_lock_key",
    "tool_sends_for_reply",
]

MAX_REPLY_CHARS = 5000  # same cap as the send_email tool
# Characters of one email in the chat. Quoted history makes bodies long.
MAX_EMAIL_CHARS = 6000
# HTML read per email, cut before parsing so a huge body costs a bounded time.
MAX_HTML_CHARS = 200_000
# Texts and emails one reply run may send, so one email cannot spend without limit.
MAX_TOOL_SENDS = 3
# SES caps a header value at 995 characters.
MAX_HEADER_CHARS = 900

ReplyState = Literal["done", "skipped", "failed"]

_NO_REPLY_LOCAL = re.compile(
    r"^(no[-_]?reply|do[-_]?not[-_]?reply|mailer[-_]?daemon|postmaster|bounces?)"
    r"([+.\-_].*)?$"
)
_MESSAGE_ID = re.compile(r"^<[!-;=?-~]+@[!-;=?-~]+>$")
# Why a stored row stays out of the chat: nothing proved who wrote it, or it was
# never meant for the agent.
_UNTRUSTED_SKIPS = (
    "auto_generated",
    "sender_blocked",
    "sender_not_member",
    "sender_not_verified",
    "spam",
    "virus",
)


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


def proves_sender(parsed: ParsedMime, dmarc_verdict: str | None) -> bool:
    """One well-formed ``From`` address and a DMARC ``PASS``. Ingest stores the
    answer in ``emails.from_verified``."""
    return parsed.from_count == 1 and parsed.from_clean and dmarc_verdict == "PASS"


def is_authenticated(email: Email) -> bool:
    """True when ingest proved the ``From`` address. Tools and thread history
    need it, under every scope."""
    return email.from_verified is True


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
    db: AsyncSession, domain: EmailDomain, *, sender: str, verified: bool
) -> str | None:
    """Why this sender may not reach the agent, or None. Fails closed.

    DMARC ``PASS`` is the only proof of a ``From`` address. SES reports ``GRAY``
    when any SPF or DKIM check passed for a domain with no policy or ``p=none``,
    aligned or not, so a forger with a valid signature of their own gets
    ``GRAY``. Unproven mail gets no reply under any scope: a reply to a forged
    ``From`` would reach an innocent third party.
    """
    if not verified:
        return "sender_not_verified"
    if domain.email_agent_scope == "anyone":
        return None
    if not await is_verified_member(db, domain.organization_id, sender):
        return "sender_not_member"
    return None


async def replies_in_thread(
    db: AsyncSession, organization_id: uuid.UUID, sender: str
) -> int:
    """Agent sends for this sender within ``REPLY_CAP_WINDOW``: replies, and
    tool sends in any channel. A run that only uses tools still counts."""
    since = datetime.now(timezone.utc) - REPLY_CAP_WINDOW
    emails = select(func.count(Email.id)).where(
        Email.organization_id == organization_id,
        Email.direction == "outbound",
        Email.agent_id.is_not(None),
        Email.requested_at >= since,
        or_(
            and_(
                Email.metadata_["reply_to_email_id"].astext.is_not(None),
                sender == any_(Email.to_addresses),
            ),
            Email.metadata_["thread_sender"].astext == sender,
        ),
    )
    texts = select(func.count(Sms.id)).where(
        Sms.organization_id == organization_id,
        Sms.direction == "outbound",
        Sms.agent_id.is_not(None),
        Sms.requested_at >= since,
        Sms.metadata_["thread_sender"].astext == sender,
    )
    total = int((await db.execute(emails)).scalar_one() or 0)
    return total + int((await db.execute(texts)).scalar_one() or 0)


async def run_refusal(
    db: AsyncSession, domain: EmailDomain, email: Email, *, check_cap: bool = True
) -> str | None:
    """Why this row must not be answered now, or None. Scope, cap, and funds
    can change between ingest and a claim, and the model costs money.
    ``check_cap`` is off when the reply already exists and only needs sending."""
    sender = normalize_address(email.from_address)
    refusal = await sender_refusal(
        db, domain, sender=sender, verified=is_authenticated(email)
    )
    if refusal is not None:
        return refusal
    if check_cap:
        count = await replies_in_thread(db, email.organization_id, sender)
        if count >= MAX_REPLIES_PER_THREAD:
            return "thread_cap"
    if not await has_funds(db, email.organization_id):
        return "insufficient_funds"
    return None


async def sender_trust(db: AsyncSession, email: Email) -> SenderTrust:
    """What the prompt may say about a proven sender."""
    sender = normalize_address(email.from_address)
    member = await is_verified_member(db, email.organization_id, sender)
    return "member" if member else "outside"


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
        db, domain, sender=sender, verified=proves_sender(parsed, dmarc_verdict)
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
        .values(
            agent_reply_state="skipped",
            metadata_=Email.metadata_.op("||")(
                func.jsonb_build_object("agent_skip_reason", "expired")
            ),
        )
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
        await finish_reply(db, email, "skipped", reason="no_agent")


async def stamp_skip(db: AsyncSession, email_id: uuid.UUID, reason: str) -> None:
    """Say why a row got no reply. A sender refusal also keeps the row out of
    later chats (``_trusted_inbound``), and no funds raises the console's
    out-of-credit alert."""
    row = await db.get(Email, email_id)
    if row is None or row.direction != "inbound":
        return
    meta = dict(row.metadata_ or {})
    meta["agent_skip_reason"] = reason
    if reason == "insufficient_funds":
        reasons = list(meta.get("suppressed_reasons") or [])
        if reason not in reasons:
            reasons.append(reason)
        meta["suppressed_reasons"] = reasons
    row.metadata_ = meta
    await db.commit()


async def finish_reply(
    db: AsyncSession,
    email: Email,
    state: ReplyState,
    *,
    attempt: int | None = None,
    reason: str | None = None,
) -> None:
    """Record the final state, and with ``reason`` why it was skipped. With
    ``attempt`` it applies only while that claim still owns the row."""
    stmt = update(Email).where(Email.id == email.id)
    if attempt is not None:
        stmt = stmt.where(
            Email.agent_reply_state == "processing",
            Email.agent_reply_attempts == attempt,
        )
    result = await db.execute(stmt.values(agent_reply_state=state))
    await db.commit()
    if reason is not None and result.rowcount:  # type: ignore[attr-defined]
        await stamp_skip(db, email.id, reason)


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


def _trusted_inbound():
    """Inbound rows the chat may quote. ``from_verified`` is the allowlist: a
    forged row with the same ``From`` stays out, and so does any row ingest did
    not prove. The other checks only take rows away."""
    return and_(
        Email.direction == "inbound",
        Email.from_verified.is_(True),
        or_(Email.spam_verdict.is_(None), Email.spam_verdict != "FAIL"),
        or_(Email.virus_verdict.is_(None), Email.virus_verdict != "FAIL"),
        or_(
            Email.metadata_["agent_skip_reason"].astext.is_(None),
            Email.metadata_["agent_skip_reason"].astext.not_in(_UNTRUSTED_SKIPS),
        ),
    )


def _answered_id(row: Email) -> uuid.UUID | None:
    """The inbound row an agent's outbound row answers, from its metadata."""
    meta = row.metadata_ or {}
    raw = meta.get("reply_to_email_id") or meta.get("inbound_email_id")
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


async def _answers_trusted(
    db: AsyncSession, email: Email, sender: str, rows: Sequence[Email]
) -> list[Email]:
    """``rows`` without any outbound row that answers something unproven. A reply
    to forged mail says what the forger asked for, so it stays out too."""
    answered = {r.id: _answered_id(r) for r in rows if r.direction == "outbound"}
    ids = {i for i in answered.values() if i is not None}
    trusted: set[uuid.UUID] = set()
    if ids:
        stmt = select(Email.id).where(
            Email.id.in_(ids),
            Email.organization_id == email.organization_id,
            _trusted_inbound(),
            func.lower(Email.from_address) == sender,
        )
        trusted = set((await db.execute(stmt)).scalars().all())
    return [r for r in rows if r.direction != "outbound" or answered[r.id] in trusted]


async def email_thread(db: AsyncSession, email: Email) -> list[Email]:
    """The thread up to and including ``email``, oldest first, at most
    ``CHAT_LIMIT`` rows.

    No reply goes to an unproven sender, and this stays closed for one anyway. A
    proven sender gets rows with this sender only, because ``References`` is
    written by the sender and could name someone else's message.
    """
    if not is_authenticated(email):
        return [email]
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
                and_(_trusted_inbound(), func.lower(Email.from_address) == sender),
                and_(Email.direction == "outbound", sender == any_(Email.to_addresses)),
            ),
        )
        # Newest first, so a long thread keeps its recent mail.
        newest = stmt.order_by(Email.requested_at.desc()).limit(CHAT_LIMIT)
        rows = (await db.execute(newest)).scalars().all()
        rows = await _answers_trusted(db, email, sender, rows)
        frontier = [r for r in rows if r.id not in seen]
        seen.update({r.id: r for r in frontier})
    earlier = [row for row in seen.values() if row.id != email.id]
    earlier.sort(key=lambda r: r.requested_at)
    return earlier[-(CHAT_LIMIT - 1) :] + [email]


class _TextExtractor(HTMLParser):
    """The visible text of an HTML body."""

    _HIDDEN = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._HIDDEN:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._HIDDEN and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _html_text(body_html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(body_html[:MAX_HTML_CHARS])
        parser.close()
    except (AssertionError, ValueError):
        # Sender-written markup must not fail the reply. Keep what was read.
        logger.warning("email html could not be parsed to the end")
    return " ".join(" ".join(parser.parts).split())


def _plain_body(email: Email) -> str:
    body = email.body_text
    if not (body or "").strip() and email.body_html:
        body = _html_text(email.body_html)
    return (body or "").strip()[:MAX_EMAIL_CHARS]


def build_chat_messages(
    agent: Agent, thread: list[Email], *, trust: SenderTrust
) -> list[dict[str, Any]]:
    """OpenAI-style messages: the system message, then the thread. Inbound
    mail is ``user``, labeled so the model reads it as mail, and replies are
    ``assistant``."""
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": build_email_instructions(agent.system_prompt, trust=trust),
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


def reply_from_address(domain: EmailDomain) -> str:
    """The hail-mail address, or ``noreply@`` on a custom domain. To and Cc
    are sender-written headers: they must not choose the ``From``."""
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
