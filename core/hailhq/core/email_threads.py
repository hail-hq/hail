"""Email conversation queries and validated RFC reply references."""

from dataclasses import dataclass
from uuid import UUID

from hailhq.core.email_agent import MAX_HEADER_CHARS, email_history_filter
from hailhq.core.models import Email
from sqlalchemy import and_, func, or_, select


@dataclass(frozen=True)
class EmailScope:
    organization_id: UUID
    peer: str
    inbox: str
    email_ids: tuple[UUID, ...] | None = None
    email_domain_id: UUID | None = None

    def query(self):
        query = select(Email).where(
            Email.organization_id == self.organization_id,
            or_(
                Email.virus_verdict.is_(None),
                func.upper(Email.virus_verdict).not_in(("FAIL", "FAILED")),
            ),
        )
        if self.email_ids is not None:
            return query.where(Email.id.in_(self.email_ids))
        if self.email_domain_id is not None:
            query = query.where(
                Email.email_domain_id == self.email_domain_id,
                email_history_filter(self.peer.lower()),
            )
        incoming = [
            Email.direction == "inbound",
            Email.from_verified.is_(True),
            func.lower(Email.from_address) == self.peer.lower(),
            or_(Email.spam_verdict.is_(None), Email.spam_verdict != "FAIL"),
        ]
        outgoing = [
            Email.direction == "outbound",
            Email.to_addresses.any(self.peer),
            Email.metadata_["forwarded_from"].astext.is_(None),
            Email.metadata_["system_kind"].astext.is_(None),
        ]
        if self.email_domain_id is None:
            incoming.append(Email.to_addresses.any(self.inbox))
            outgoing.append(func.lower(Email.from_address) == self.inbox.lower())
        return query.where(
            or_(
                and_(*incoming),
                and_(*outgoing),
            )
        )


def reference_ids(email, *, limit=30):
    """Only safe Message-ID header values; never copy arbitrary headers."""
    ids = list(
        dict.fromkeys(
            x
            for x in [
                *(email.references_ids or []),
                email.in_reply_to,
                email.message_id,
            ]
            if isinstance(x, str)
            and x.startswith("<")
            and x.endswith(">")
            and len(x) <= 998
            and not any(c in x for c in "\r\n \t")
        )
    )
    if limit is None:
        return ids
    # Preserve the root and newest parents within both service header limits.
    # Unbounded IDs above remain available for comparing stored threads.
    ids = [value for value in ids if len(value) <= MAX_HEADER_CHARS]
    if not ids:
        return []
    root = ids[0]
    if len(ids) > 1 and len(root) + 1 + len(ids[-1]) > MAX_HEADER_CHARS:
        return ids[-1:]
    recent = []
    length = len(root)
    for value in reversed(ids[1:]):
        if len(recent) >= limit - 1 or length + 1 + len(value) > MAX_HEADER_CHARS:
            break
        recent.append(value)
        length += 1 + len(value)
    return [root, *reversed(recent)]


def same_thread(first, second):
    return first.id == second.id or bool(
        set(reference_ids(first, limit=None)) & set(reference_ids(second, limit=None))
    )
