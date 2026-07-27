import pytest
from cryptography.fernet import InvalidToken
from hailhq.core.secret_cipher import (
    SecretCipher,
    SecretKeyMissing,
    generate_key,
)


def test_round_trip():
    cipher = SecretCipher(generate_key())
    token = cipher.encrypt("whs_supersecret")
    assert token != "whs_supersecret"  # not plaintext at rest
    assert cipher.decrypt(token) == "whs_supersecret"


def test_distinct_keys_cannot_decrypt():
    a, b = SecretCipher(generate_key()), SecretCipher(generate_key())
    token = a.encrypt("x")
    with pytest.raises(InvalidToken):
        b.decrypt(token)


def test_missing_key_raises():
    with pytest.raises(SecretKeyMissing):
        SecretCipher("")


def test_garbage_key_raises_secret_key_missing():
    with pytest.raises(SecretKeyMissing):
        SecretCipher("not-a-valid-fernet-key")


def test_generate_key_is_usable():
    key = generate_key()
    assert isinstance(key, str)
    c = SecretCipher(key)
    assert c.decrypt(c.encrypt("x")) == "x"
