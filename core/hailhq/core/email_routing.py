"""Hail-mail local-part routing.

Mirrors the prefix grammar enforced on outbound:
``^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$``. The head before the plus names the
mailbox; the tail after it is an alias and is ignored, as mail providers
ignore it. ``<head>@<base>`` and ``<head>+<anything>@<base>`` reach one
mailbox. The classifier returns None for any other recipient, and for a bare
role name such as ``postmaster@<base>``, so the caller can decide to drop or
log.
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

# Certificate authorities prove control of a domain by mailing names like
# ``admin@``, and RFC 2142 keeps others for the domain's operator. A tenant
# may hold the head ``admin``, so a bare role name must never reach a tenant:
# the base domain belongs to the operator. Only the bare form is refused.
_ROLE_LOCAL_PARTS = frozenset(
    {
        "abuse",
        "admin",
        "administrator",
        "hostmaster",
        "mailer-daemon",
        "noc",
        "postmaster",
        "root",
        "security",
        "webmaster",
    }
)


@dataclass(frozen=True)
class HailMailRecipient:
    user_prefix: str
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
    if local in _ROLE_LOCAL_PARTS:
        return None
    user = local.partition("+")[0]
    if not _PREFIX_RE.match(user):
        return None
    return HailMailRecipient(user_prefix=user, base_domain=domain.lower())
