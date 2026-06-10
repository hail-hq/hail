import re

from hailhq.core.email_routing import (
    HAIL_MAIL_PREFIX_PATTERN,
    HailMailRecipient,
    classify_hail_mail_recipient,
)


def test_classify_full_address():
    r = classify_hail_mail_recipient("alice+acme@mail.hail.so", "mail.hail.so")
    assert r == HailMailRecipient(
        user_prefix="alice", org_prefix="acme", base_domain="mail.hail.so"
    )


def test_classify_case_insensitive_domain():
    r = classify_hail_mail_recipient("alice+acme@Mail.HAIL.so", "mail.hail.so")
    assert r is not None
    assert r.base_domain == "mail.hail.so"


def test_unknown_domain_returns_none():
    assert (
        classify_hail_mail_recipient("alice+acme@other.example", "mail.hail.so") is None
    )


def test_missing_plus_returns_none():
    assert classify_hail_mail_recipient("alice@mail.hail.so", "mail.hail.so") is None


def test_postmaster_returns_none():
    assert (
        classify_hail_mail_recipient("postmaster@mail.hail.so", "mail.hail.so") is None
    )


def test_invalid_prefix_returns_none():
    # mixed-case local part is now normalised — NOT invalid
    assert (
        classify_hail_mail_recipient("Alice+acme@mail.hail.so", "mail.hail.so")
        is not None
    )
    # leading hyphen
    assert (
        classify_hail_mail_recipient("-alice+acme@mail.hail.so", "mail.hail.so") is None
    )
    # too long org prefix (>20 chars)
    twenty_one = "a" * 21
    assert (
        classify_hail_mail_recipient(f"alice+{twenty_one}@mail.hail.so", "mail.hail.so")
        is None
    )


def test_missing_at_returns_none():
    assert classify_hail_mail_recipient("nobody", "mail.hail.so") is None


def test_pattern_matches_valid_prefixes():
    assert re.match(HAIL_MAIL_PREFIX_PATTERN, "a")
    assert re.match(HAIL_MAIL_PREFIX_PATTERN, "alice")
    assert re.match(HAIL_MAIL_PREFIX_PATTERN, "alice-bob")
    assert re.match(HAIL_MAIL_PREFIX_PATTERN, "a" * 20)


def test_pattern_rejects_invalid_prefixes():
    assert not re.match(HAIL_MAIL_PREFIX_PATTERN, "Alice")
    assert not re.match(HAIL_MAIL_PREFIX_PATTERN, "-alice")
    assert not re.match(HAIL_MAIL_PREFIX_PATTERN, "alice-")
    assert not re.match(HAIL_MAIL_PREFIX_PATTERN, "a" * 21)
    assert not re.match(HAIL_MAIL_PREFIX_PATTERN, "")


def test_mixed_case_local_part_routes():
    r = classify_hail_mail_recipient("Alice+Acme@MAIL.HAIL.SO", "mail.hail.so")
    assert r is not None
    assert r.user_prefix == "alice"
    assert r.org_prefix == "acme"
