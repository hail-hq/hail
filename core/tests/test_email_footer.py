from hailhq.core.email_footer import (
    SENT_FOOTER_TEXT,
    append_forwarded_footer,
    append_sent_footer,
)


def test_append_sent_footer_is_one_blended_line():
    text, html = append_sent_footer("hello", "<p>hello</p>")
    assert text == (
        "hello\n\n--\nSent via Hail.so (https://hail.so), "
        "an AI communication platform."
    )
    assert html is not None
    assert html.startswith("<p>hello</p>")
    assert 'href="https://hail.so"' in html
    # One footer paragraph — not a branding footer plus a separate
    # disclosure paragraph (the pre-2026-07 layout this replaced).
    assert html.count("an AI communication platform") == 1


def test_append_sent_footer_none_parts_stay_none():
    text, html = append_sent_footer(None, "<p>x</p>")
    assert text is None
    assert html is not None and "an AI communication platform" in html

    text, html = append_sent_footer("x", None)
    assert html is None
    assert text is not None and "an AI communication platform" in text


def test_append_forwarded_footer_keeps_forwarded_label():
    text, html = append_forwarded_footer("hello", "<p>hello</p>")
    assert text == "hello\n\n--\nForwarded by Hail.so (https://hail.so)"
    assert html is not None
    assert "Forwarded by" in html
    assert 'href="https://hail.so"' in html
    # Forwarded mail is relayed human content — no AI disclosure.
    assert "AI communication platform" not in html


def test_append_forwarded_footer_none_parts_stay_none():
    # An HTML-only forward must not grow a footer-only text part (and
    # vice versa) — email_forwarding.py relies on this passthrough.
    text, html = append_forwarded_footer(None, "<p>x</p>")
    assert text is None
    assert html is not None and "Forwarded by" in html

    text, html = append_forwarded_footer("x", None)
    assert html is None
    assert text is not None and "Forwarded by" in text


def test_both_footers_share_one_html_wrapper_style():
    _, sent_html = append_sent_footer(None, "")
    _, fwd_html = append_forwarded_footer(None, "")
    assert sent_html is not None and fwd_html is not None
    # Same opening <p style=...--<br> wrapper — restyles land in one place.
    assert sent_html.split("--<br>")[0] == fwd_html.split("--<br>")[0]


def test_sent_footer_text_constant_is_the_wire_line():
    assert SENT_FOOTER_TEXT == (
        "Sent via Hail.so (https://hail.so), an AI communication platform."
    )
