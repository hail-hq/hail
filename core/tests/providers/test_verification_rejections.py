"""The reason a customer reads when a carrier rejects a verification."""

from __future__ import annotations

from hailhq.core.providers.verification.base import DETAILS_REJECTED
from hailhq.core.providers.verification.rejections import (
    START_AGAIN,
    TWILIO_FIXES,
    customer_reason,
    twilio_reason,
)


def test_known_twilio_code_gets_hails_fix() -> None:
    reason = twilio_reason([(18008, "Name mismatch text from Twilio")])
    assert reason == f"{TWILIO_FIXES[18008]} {START_AGAIN}"
    assert "Twilio" not in reason


def test_unknown_twilio_code_shows_the_carriers_text() -> None:
    assert twilio_reason([(18999, "Something new.")]) == (
        f"The carrier said: Something new. {START_AGAIN}"
    )


def test_twilio_text_without_a_code_is_shown() -> None:
    assert "The carrier said: Use a newer bill." in twilio_reason(
        [(None, "Use a newer bill.")]
    )


def test_twilio_summary_code_is_dropped_when_a_document_says_more() -> None:
    reason = twilio_reason([(18050, "See each document."), (18003, "Expired.")])
    assert reason == f"{TWILIO_FIXES[18003]} {START_AGAIN}"
    # Alone, the summary is all there is.
    assert "See each document." in twilio_reason([(18050, "See each document.")])


def test_duplicates_are_dropped_in_order() -> None:
    reason = twilio_reason([(18008, "a"), (18003, "b"), (18008, "c")])
    assert reason == f"{TWILIO_FIXES[18008]} {TWILIO_FIXES[18003]} {START_AGAIN}"


def test_nothing_known_gives_the_generic_wording() -> None:
    assert twilio_reason([]) == DETAILS_REJECTED
    assert customer_reason([" "], [""]) == DETAILS_REJECTED


def test_every_fix_is_a_full_sentence() -> None:
    for code, fix in TWILIO_FIXES.items():
        assert fix.endswith("."), code
        assert "  " not in fix, code
