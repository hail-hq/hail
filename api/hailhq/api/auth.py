"""API-key hashing — matches the auth backend's storage format.

The auth backend (in hail-website) is the sole producer of `hl_live_*` keys.
hail/api is a read-only consumer: incoming bearer tokens are hashed with the
same scheme as the backend and looked up in the shared `apikey` table.

Hash format: ``base64url(SHA-256(key))`` with no padding.
"""

from __future__ import annotations

import base64
import hashlib


def hash_key(plain: str) -> str:
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
