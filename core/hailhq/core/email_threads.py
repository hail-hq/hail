"""Email conversation queries and validated RFC reply references."""

from dataclasses import dataclass
from uuid import UUID

from hailhq.core.models import Email
from sqlalchemy import and_, func, or_, select


@dataclass(frozen=True)
class EmailScope:
    organization_id: UUID
    peer: str
    inbox: str

    def query(self):
        return select(Email).where(
            Email.organization_id == self.organization_id,
            or_(
                Email.virus_verdict.is_(None),
                func.upper(Email.virus_verdict).not_in(("FAIL", "FAILED")),
            ),
            or_(
                and_(
                    Email.direction == "inbound",
                    func.lower(Email.from_address) == self.peer.lower(),
                    Email.to_addresses.any(self.inbox),
                ),
                and_(
                    Email.direction == "outbound",
                    func.lower(Email.from_address) == self.inbox.lower(),
                    Email.to_addresses.any(self.peer),
                ),
            ),
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
    return ids[-limit:] if limit is not None else ids


def same_thread(first, second):
    return first.id == second.id or bool(
        set(reference_ids(first, limit=None)) & set(reference_ids(second, limit=None))
    )
