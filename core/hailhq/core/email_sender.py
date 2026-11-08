"""How an ``email_domains`` row turns into a From-address.

One formatter, three callers — ``POST /emails``, the voicebot's internal
send route, and ``GET /email-domains`` (which previews the same decision
as ``default_from``) — so the address a caller is shown cannot drift
from the address a send actually goes out as.

Row selection itself (which identity, and what happens when there is
none) stays in ``api/hailhq/api/routes/email_domains.py``: it needs the
session and the operator's hail-mail config.
"""

from __future__ import annotations

from uuid import UUID

from hailhq.core.models import EmailDomain, RetiredHailMailAddress
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Local-part used when the caller names no address and the identity is a
# custom domain (a hail-mail row already carries a full address).
DEFAULT_LOCAL_PART = "noreply"


def from_address_for(sd: EmailDomain, explicit: str | None = None) -> str:
    """Resolve the wire ``From:`` for a send.

    Hail-mail: ``sd.domain`` is the full address.
    Custom:    ``sd.domain`` is just the DNS name; if the caller didn't
               supply an explicit local-part, default to ``noreply``.

    The no-``explicit`` result doubles as the ``from`` value a caller can
    paste back: custom rows accept any local-part, so ``noreply@`` is a
    working example rather than the only choice.
    """
    if explicit is not None:
        return explicit
    if sd.kind == "hail_mail":
        return sd.domain
    return f"{DEFAULT_LOCAL_PART}@{sd.domain}"


__all__ = ["DEFAULT_LOCAL_PART", "from_address_for"]


async def head_retired_by_other_org(
    db: AsyncSession, head: str, organization_id: UUID
) -> bool:
    """Would the ``email_domains`` guard trigger refuse this head for this org?

    Only the org that gave a head up last may take it back.
    """
    last_holder = (
        await db.execute(
            select(RetiredHailMailAddress.organization_id)
            .where(RetiredHailMailAddress.local_prefix_user == head)
            .order_by(
                RetiredHailMailAddress.retired_at.desc(),
                RetiredHailMailAddress.organization_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return last_holder is not None and last_holder != organization_id
