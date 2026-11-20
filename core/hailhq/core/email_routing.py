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
    workspace_prefix: str
    # The text up to the next ``+``. Empty for a bare address.
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
    head, _, rest = local.partition("+")
    # alice+globex+promo is alice+globex, and admin++ is a bare admin.
    tail = rest.partition("+")[0]
    if not _PREFIX_RE.match(head):
        return None
    if not tail and head in ROLE_MAILBOXES:
        return None
    return HailMailRecipient(
        workspace_prefix=head, tail=tail, base_domain=domain.lower()
    )
