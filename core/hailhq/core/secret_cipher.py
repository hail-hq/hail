"""Symmetric encryption for webhook secrets at rest.

Webhook signing needs the plaintext secret at delivery time, so we can't
store only a hash. Instead we Fernet-encrypt the secret with a deployment
key (``HAIL_WEBHOOK_SECRET_KEY``) and persist the ciphertext. The worker
decrypts on each delivery — so secrets survive restarts and work across
processes, unlike the previous in-process cache.

Generate a key with::

    python -c "from hailhq.core.secret_cipher import generate_key; print(generate_key())"
"""

from __future__ import annotations

from cryptography.fernet import Fernet

__all__ = ["SecretCipher", "SecretKeyMissing", "generate_key"]


class SecretKeyMissing(RuntimeError):
    """A Fernet secret key is unset but a secret op was attempted."""


def generate_key() -> str:
    return Fernet.generate_key().decode()


class SecretCipher:
    def __init__(self, key: str) -> None:
        if not key:
            raise SecretKeyMissing(
                "a Fernet secret key must be set (HAIL_WEBHOOK_SECRET_KEY for webhooks, "
                "HAIL_PROVIDER_SECRET_KEY for provider keys)"
            )
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise SecretKeyMissing(
                "a Fernet secret key is set but is not a valid Fernet key. "
                "Generate one with: python -c "
                '"from hailhq.core.secret_cipher import generate_key; print(generate_key())"'
            ) from exc

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
