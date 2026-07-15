"""Tests for SMS pricing-tier classification (US/Canada/rest-of-world).

Distinct from core.hailhq.core.sender_id's corridor classification — that
module answers "can this destination use an alphanumeric Sender ID";
this one answers "which billing rate applies." Same E.164 input, two
different, unrelated output axes.
"""

from __future__ import annotations

from hailhq.core.pricing_tier import classify_pricing_tier


def test_us_number_is_us_tier() -> None:
    assert classify_pricing_tier("+14155551234") == "us"


def test_canadian_area_code_is_ca_tier() -> None:
    assert classify_pricing_tier("+14165551234") == "ca"  # 416 = Toronto


def test_uk_number_is_row_tier() -> None:
    assert classify_pricing_tier("+447911123456") == "row"


def test_germany_number_is_row_tier() -> None:
    assert classify_pricing_tier("+491701234567") == "row"
