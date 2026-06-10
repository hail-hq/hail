"""Hail-mail local-part routing.

Mirrors the prefix grammar enforced on outbound:
``^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$``. The classifier returns
None for any recipient that isn't a well-formed ``<user>+<org>@<base>``
on the configured hail-mail base domain — including postmaster/abuse
aliases — so the caller can decide to drop or log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "HAIL_MAIL_PREFIX_PATTERN",
    "HailMailRecipient",
    "classify_hail_mail_recipient",
]


HAIL_MAIL_PREFIX_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$"
_PREFIX_RE = re.compile(HAIL_MAIL_PREFIX_PATTERN)


@dataclass(frozen=True)
class HailMailRecipient:
    user_prefix: str
    org_prefix: str
    base_domain: str


def classify_hail_mail_recipient(
    address: str, base_domain: str
) -> HailMailRecipient | None:
    if "@" not in address:
        return None
    local, _, domain = address.partition("@")
    if domain.lower() != base_domain.lower():
        return None
    # Hail mints lowercase prefixes; senders type whatever they like.
    # Local-part case-insensitivity is safe here because these are our
    # own addresses, not arbitrary third-party mailboxes.
    local = local.lower()
    if "+" not in local:
        return None
    user, _, org = local.partition("+")
    if not _PREFIX_RE.match(user) or not _PREFIX_RE.match(org):
        return None
    return HailMailRecipient(
        user_prefix=user, org_prefix=org, base_domain=domain.lower()
    )
