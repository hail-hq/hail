"""The AI line: direction-aware defaults, workspace and agent templates."""

from __future__ import annotations

from hailhq.core.disclosure import (
    DEFAULT_INBOUND_LINE,
    DEFAULT_OUTBOUND_LINE,
    disclosure_text,
)


def test_outbound_default_with_and_without_org():
    assert (
        disclosure_text("outbound", "Acme Dental")
        == "Hi, this is an AI assistant calling on behalf of Acme Dental."
    )
    assert (
        disclosure_text("outbound", None)
        == "Hi, this is an AI assistant calling on behalf of whoever requested this call."
    )


def test_inbound_default_with_and_without_org():
    assert (
        disclosure_text("inbound", " Acme Dental ")
        == "Hi, this is an AI assistant answering on behalf of Acme Dental."
    )
    assert (
        disclosure_text("inbound", "")
        == "Hi, this is an AI assistant answering on behalf of this number."
    )


def test_template_with_placeholder():
    line = disclosure_text("inbound", "Acme", template="You reached {org}. I am an AI.")
    assert line == "You reached Acme. I am an AI."


def test_template_without_placeholder_is_spoken_as_is():
    assert disclosure_text("outbound", "Acme", template="An AI assistant here.") == (
        "An AI assistant here."
    )


def test_blank_template_falls_back_to_default():
    assert disclosure_text(
        "outbound", "Acme", template="   "
    ) == DEFAULT_OUTBOUND_LINE.format(org="Acme")
    assert DEFAULT_INBOUND_LINE.format(org="X").endswith("X.")
