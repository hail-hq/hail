from hailhq.core.email_footer import (
    FOOTER_FORWARDED,
    SENT_FOOTER_TEXT,
    append_footer,
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


def test_append_footer_forwarded_label_unchanged():
    text, html = append_footer("hello", "<p>hello</p>", label=FOOTER_FORWARDED)
    assert text == "hello\n\n--\nForwarded by Hail.so (https://hail.so)"
    assert html is not None
    assert "Forwarded by Hail.so" in html
    assert 'href="https://hail.so"' in html


def test_sent_footer_text_constant_is_the_wire_line():
    assert SENT_FOOTER_TEXT == (
        "Sent via Hail.so (https://hail.so), an AI communication platform."
    )
