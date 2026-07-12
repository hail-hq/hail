"""Tests for Sender ID corridor classification and resolution."""

from __future__ import annotations

from hailhq.core.sender_id import resolve_sender


def test_us_always_requires_dedicated_number() -> None:
    result = resolve_sender("+14155551234", custom_sender_id="ACME")
    assert result.kind == "dedicated_number_required"


def test_canada_always_requires_dedicated_number() -> None:
    # Canadian NANP number (+1 area code 416 = Toronto)
    result = resolve_sender("+14165551234", custom_sender_id="ACME")
    assert result.kind == "dedicated_number_required"


def test_germany_uses_custom_sender_id_when_set() -> None:
    result = resolve_sender("+491701234567", custom_sender_id="ACME")
    assert result.kind == "alphanumeric"
    assert result.sender_id == "ACME"


def test_germany_falls_back_to_platform_default_when_unset() -> None:
    result = resolve_sender("+491701234567", custom_sender_id=None)
    assert result.kind == "alphanumeric"
    assert result.sender_id == "HAIL"


def test_uk_uses_custom_sender_id() -> None:
    result = resolve_sender("+447911123456", custom_sender_id="ACME")
    assert result.kind == "alphanumeric"
    assert result.sender_id == "ACME"


def test_australia_ignores_custom_id_uses_platform_default() -> None:
    # Registration-required corridor: custom per-org IDs not supported yet.
    result = resolve_sender("+61412345678", custom_sender_id="ACME")
    assert result.kind == "alphanumeric"
    assert result.sender_id == "HAIL"


def test_india_excluded_requires_dedicated_number() -> None:
    result = resolve_sender("+919876543210", custom_sender_id="ACME")
    assert result.kind == "dedicated_number_required"


def test_unclassified_country_conservatively_requires_dedicated_number() -> None:
    # +33 France is not in the researched corridor list — safe fallback.
    result = resolve_sender("+33612345678", custom_sender_id="ACME")
    assert result.kind == "dedicated_number_required"
