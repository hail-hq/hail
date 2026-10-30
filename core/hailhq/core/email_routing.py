"""Hail-mail local-part routing.

Mirrors the prefix grammar enforced on outbound:
``^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$``. The head before the plus names the
mailbox; the tail after it is an alias and is ignored, as mail providers
ignore it. ``<head>@<base>`` and ``<head>+<anything>@<base>`` reach one
mailbox. The classifier returns None for any other recipient, and for a bare
role name (``ROLE_MAILBOXES``) such as ``postmaster@<base>``, so the caller
can decide to drop or log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hailhq.core.hail_mail import ROLE_MAILBOXES

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
    # Empty for ``<head>@<base>`` and ``<head>+@<base>``.
    tail: str
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
    user, _, tail = local.partition("+")
    if not _PREFIX_RE.match(user):
        return None
    if not tail and user in ROLE_MAILBOXES:
        return None
    return HailMailRecipient(user_prefix=user, tail=tail, base_domain=domain.lower())
