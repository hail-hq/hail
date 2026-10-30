"""Hail-mail address derivation shared across the API and workers.

A hail-mail address is ``<head>+<tail>@<base>``. The head names the mailbox:
inbound mail routes on it alone, and one row per deployment may hold it. The
tail is an alias.

The default tail is derived deterministically from the organization id rather
than from a human name or an operator-wide constant. A default head adds the
same token, so two orgs never compete for one head.

The TypeScript mirror lives in hail-website ``lib/org-mail-prefix.ts``; keep
the encoding identical so both sides mint the same address for a given org:
``base32(sha256(uuid_bytes)[:5])``, lowercased, no padding.
"""

from __future__ import annotations

import base64
import hashlib
import uuid

# 5 bytes -> exactly 8 base32 chars, no padding. base32's alphabet (a–z, 2–7
# once lowercased) is a strict subset of the LOCAL_PREFIX charset [a-z0-9].
# Hash the full 16 UUID bytes first so all 128 bits feed the prefix — a raw
# byte-prefix truncation collides for ids that differ only in low bytes.
_ORG_PREFIX_BYTES = 5

# Longest prefix the LOCAL_PREFIX rule accepts.
_PREFIX_MAX_LEN = 20


def org_prefix_from_id(org_id: uuid.UUID | str) -> str:
    """Derive a stable 8-char base32 org prefix from an organization id."""
    if not isinstance(org_id, uuid.UUID):
        org_id = uuid.UUID(str(org_id))
    digest = hashlib.sha256(org_id.bytes).digest()[:_ORG_PREFIX_BYTES]
    return base64.b32encode(digest).decode("ascii").lower()


def default_user_prefix(prefix: str, org_id: uuid.UUID | str) -> str:
    """``<prefix>-<org token>``: a shared default head made unique per org."""
    token = org_prefix_from_id(org_id)
    return f"{prefix[: _PREFIX_MAX_LEN - len(token) - 1].rstrip('-')}-{token}"
