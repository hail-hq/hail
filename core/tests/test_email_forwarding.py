from uuid import uuid4

import pytest

from hailhq.core.email_forwarding import (
    LoopDetected,
    build_forwarded,
    detect_loop,
)
from hailhq.core.email_mime import ParsedMime


def _parsed(subject: str = "Hello", message_id: str = "<m1@example.com>") -> ParsedMime:
    return ParsedMime(
        from_address="alice@example.com",
        to_addresses=["bob+acme@mail.hail.so"],
        cc_addresses=[],
        subject=subject,
        message_id=message_id,
        in_reply_to=None,
        references_ids=None,
        body_text="hi",
        body_html=None,
    )


def test_build_forwarded_rewrites_from_and_reply_to():
    inbound_id = uuid4()
    fwd = build_forwarded(
        parsed=_parsed(),
        target="team@acme.com",
        forwarder_address="forwarder+acme@mail.hail.so",
        inbound_id=inbound_id,
        hops=0,
    )
    assert fwd.from_address == "forwarder+acme@mail.hail.so"
    assert fwd.reply_to == "alice@example.com"
    assert fwd.to_addresses == ["team@acme.com"]
    assert fwd.subject.startswith("Fwd:")
    assert fwd.body_text and "Forwarded message" in fwd.body_text
    assert fwd.headers["X-Hail-Forwarded-From"] == "alice@example.com"
    assert fwd.headers["X-Hail-Original-Message-Id"] == "<m1@example.com>"
    assert fwd.headers["X-Hail-Inbound-Id"] == str(inbound_id)
    assert fwd.headers["X-Hail-Forward-Hops"] == "1"
    assert fwd.headers["Auto-Submitted"] == "auto-forwarded"


def test_build_forwarded_does_not_double_prefix_subject():
    fwd = build_forwarded(
        parsed=_parsed(subject="Fwd: already"),
        target="team@acme.com",
        forwarder_address="forwarder+acme@mail.hail.so",
        inbound_id=uuid4(),
        hops=0,
    )
    assert fwd.subject == "Fwd: already"


def test_build_forwarded_handles_fw_prefix_case_insensitive():
    fwd = build_forwarded(
        parsed=_parsed(subject="FW: thing"),
        target="team@acme.com",
        forwarder_address="forwarder+acme@mail.hail.so",
        inbound_id=uuid4(),
        hops=0,
    )
    assert fwd.subject == "FW: thing"


def test_build_forwarded_increments_hop_counter():
    fwd = build_forwarded(
        parsed=_parsed(),
        target="team@acme.com",
        forwarder_address="forwarder+acme@mail.hail.so",
        inbound_id=uuid4(),
        hops=2,
    )
    assert fwd.headers["X-Hail-Forward-Hops"] == "3"


def test_build_forwarded_sets_references_and_html_preamble():
    parsed = ParsedMime(
        from_address="alice@example.com",
        to_addresses=["alice+acme@mail.hail.so"],
        cc_addresses=[],
        subject="hi",
        message_id="<orig@example.com>",
        in_reply_to=None,
        references_ids=None,
        body_text=None,
        body_html="<p>hello</p>",
    )
    fwd = build_forwarded(
        parsed=parsed,
        target="ops@example.com",
        forwarder_address="forwarder+acme@mail.hail.so",
        inbound_id=uuid4(),
        hops=0,
    )
    assert fwd.headers["References"] == "<orig@example.com>"
    assert fwd.body_html is not None and "Forwarded message" in fwd.body_html


def test_build_forwarded_bodyless_gets_preamble_text():
    parsed = ParsedMime(
        from_address="alice@example.com",
        to_addresses=["alice+acme@mail.hail.so"],
        cc_addresses=[],
        subject="invoice",
        message_id=None,
        in_reply_to=None,
        references_ids=None,
        body_text=None,
        body_html=None,
    )
    fwd = build_forwarded(
        parsed=parsed,
        target="ops@example.com",
        forwarder_address="forwarder+acme@mail.hail.so",
        inbound_id=uuid4(),
        hops=0,
    )
    assert fwd.body_text is not None and "Forwarded message" in fwd.body_text


def test_detect_loop_rejects_base_domain_self_forward():
    with pytest.raises(LoopDetected):
        detect_loop(
            target="someone@mail.hail.so",
            hops=0,
            base_domain="mail.hail.so",
            max_hops=3,
        )


def test_detect_loop_rejects_at_max_hops():
    with pytest.raises(LoopDetected):
        detect_loop(
            target="ops@acme.com",
            hops=3,
            base_domain="mail.hail.so",
            max_hops=3,
        )


def test_detect_loop_passes_at_under_max():
    detect_loop(
        target="ops@acme.com",
        hops=2,
        base_domain="mail.hail.so",
        max_hops=3,
    )


def test_detect_loop_case_insensitive_base_domain():
    with pytest.raises(LoopDetected):
        detect_loop(
            target="someone@MAIL.HAIL.SO",
            hops=0,
            base_domain="mail.hail.so",
            max_hops=3,
        )


def test_build_forwarded_strips_crlf_from_hostile_headers():
    parsed = ParsedMime(
        from_address="alice@example.com",
        to_addresses=["alice+acme@mail.hail.so"],
        cc_addresses=[],
        subject="hi\r\nBcc: evil@example.com",
        message_id="<x\r\nBcc: evil@example.com>",
        in_reply_to=None,
        references_ids=None,
        body_text="hello",
        body_html=None,
    )
    fwd = build_forwarded(
        parsed=parsed,
        target="ops@example.com",
        forwarder_address="forwarder+acme@mail.hail.so",
        inbound_id=uuid4(),
        hops=0,
    )
    assert "\r" not in fwd.subject and "\n" not in fwd.subject
    for value in fwd.headers.values():
        assert "\r" not in value and "\n" not in value


def test_build_forwarded_appends_branding_footer():
    parsed = ParsedMime(
        from_address="alice@example.com",
        to_addresses=["alice+acme@mail.hail.so"],
        cc_addresses=[],
        subject="hi",
        message_id="<orig@example.com>",
        in_reply_to=None,
        references_ids=None,
        body_text="original",
        body_html="<p>original</p>",
    )
    fwd = build_forwarded(
        parsed=parsed,
        target="ops@example.com",
        forwarder_address="forwarder+acme@mail.hail.so",
        inbound_id=uuid4(),
        hops=0,
    )
    assert "Forwarded by Hail.so" in fwd.body_text
    assert fwd.body_text.index("original") < fwd.body_text.index("Forwarded by")
    assert 'href="https://hail.so"' in fwd.body_html


def test_loop_detected_carries_cause():
    # base-domain self-reference → cause == "base_domain"
    with pytest.raises(LoopDetected) as exc_info:
        detect_loop(
            target="self@mail.hail.so",
            hops=0,
            base_domain="mail.hail.so",
            max_hops=3,
        )
    assert exc_info.value.cause == "base_domain"

    # hop-cap exceeded → cause == "hop_cap"
    with pytest.raises(LoopDetected) as exc_info:
        detect_loop(
            target="ops@acme.com",
            hops=3,
            base_domain="mail.hail.so",
            max_hops=3,
        )
    assert exc_info.value.cause == "hop_cap"
