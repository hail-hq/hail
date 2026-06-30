"""Hail-mail address derivation shared across the API and workers.

The org side of a hail-mail address (``<user>+<org>@<base>``) is derived
deterministically from the organization id rather than from a human name or
an operator-wide constant. That guarantees every org gets a globally-unique
prefix by construction — the inbound routing key ``(local_prefix_user,
local_prefix_org)`` can never collide across orgs.

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


def org_prefix_from_id(org_id: uuid.UUID | str) -> str:
    """Derive a stable 8-char base32 org prefix from an organization id."""
    if not isinstance(org_id, uuid.UUID):
        org_id = uuid.UUID(str(org_id))
    digest = hashlib.sha256(org_id.bytes).digest()[:_ORG_PREFIX_BYTES]
    return base64.b32encode(digest).decode("ascii").lower()
