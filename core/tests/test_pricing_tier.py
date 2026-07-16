"""Tests for SMS pricing-tier classification (US/Canada/rest-of-world).

Distinct from core.hailhq.core.sender_id's corridor classification — that
module answers "can this destination use an alphanumeric Sender ID";
this one answers "which billing rate applies." Same E.164 input, two
different, unrelated output axes.
"""

from __future__ import annotations

import pytest

from hailhq.core.pricing_tier import classify_pricing_tier


def test_us_number_is_us_tier() -> None:
    assert classify_pricing_tier("+14155551234") == "us"


def test_canadian_area_code_is_ca_tier() -> None:
    assert classify_pricing_tier("+14165551234") == "ca"  # 416 = Toronto


def test_uk_number_is_row_tier() -> None:
    assert classify_pricing_tier("+447911123456") == "row"


def test_germany_number_is_row_tier() -> None:
    assert classify_pricing_tier("+491701234567") == "row"


@pytest.mark.parametrize(
    "npa, country",
    [
        ("876", "Jamaica"),
        ("658", "Jamaica overlay"),
        ("809", "Dominican Republic"),
        ("829", "Dominican Republic"),
        ("849", "Dominican Republic"),
        ("242", "Bahamas"),
        ("246", "Barbados"),
        ("868", "Trinidad and Tobago"),
        ("441", "Bermuda"),
        ("345", "Cayman Islands"),
        ("284", "British Virgin Islands"),
        ("721", "Sint Maarten"),
    ],
)
def test_foreign_nanp_country_is_row_tier(npa: str, country: str) -> None:
    """+1 is not a synonym for US/Canada — ~20 NANP countries bill at row.

    These used to fall through to "us" (2.5¢) despite wholesale cost running
    at or above the 20¢ row rate, i.e. negative margin on every segment.
    """
    assert classify_pricing_tier(f"+1{npa}5551234") == "row", country


@pytest.mark.parametrize(
    "npa, region",
    [
        ("263", "Montreal QC"),
        ("354", "QC 450/579 overlay"),
        ("367", "QC 418/581 overlay"),
        ("368", "Alberta"),
        ("382", "ON 519/226/548 overlay"),
        ("428", "New Brunswick"),
        ("468", "QC 819/873 overlay"),
        ("474", "Saskatchewan"),
        ("584", "Manitoba"),
        ("683", "Ontario"),
        ("742", "Ontario"),
        ("753", "Ottawa ON"),
        ("879", "Newfoundland"),
        ("942", "Toronto ON"),
    ],
)
def test_canadian_relief_area_codes_are_ca_tier(npa: str, region: str) -> None:
    """Newer Canadian relief NPAs bill at the ca rate, not the us rate."""
    assert classify_pricing_tier(f"+1{npa}5551234") == "ca", region


def test_us_territories_are_us_tier() -> None:
    """Carriers rate US territories as domestic, so they stay on the us tier."""
    assert classify_pricing_tier("+17875551234") == "us"  # Puerto Rico
    assert classify_pricing_tier("+13405551234") == "us"  # US Virgin Islands
