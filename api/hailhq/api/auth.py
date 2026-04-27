"""API-key crypto primitives.

Inputs are ~256-bit random tokens, so SHA-256 is sufficient and a
password hasher (argon2) would be the wrong tool.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

KEY_SCHEME_PREFIX = "hk_"
_RANDOM_BYTES = 32
_PREFIX_LEN = len(KEY_SCHEME_PREFIX) + 5


def _sha256_hex(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def hash_key(plain: str) -> tuple[str, str]:
    """Return ``(key_prefix, hex_digest)`` for ``plain``."""
    return plain[:_PREFIX_LEN], _sha256_hex(plain)


def verify_key(plain: str, stored_hex_digest: str) -> bool:
    """Constant-time check of ``plain`` against the stored SHA-256 hex."""
    return hmac.compare_digest(_sha256_hex(plain), stored_hex_digest)


def generate_key() -> tuple[str, str, str]:
    """Generate a fresh key.

    Returns ``(full_key, key_prefix, hex_digest)``. The full key is shown
    to the user **once**; only the prefix and hex digest get persisted.
    """
    body = base64.urlsafe_b64encode(secrets.token_bytes(_RANDOM_BYTES)).rstrip(b"=")
    full = KEY_SCHEME_PREFIX + body.decode("ascii")
    prefix, hex_digest = hash_key(full)
    return full, prefix, hex_digest
