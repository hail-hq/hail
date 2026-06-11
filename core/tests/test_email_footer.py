from hailhq.core.email_footer import FOOTER_FORWARDED, FOOTER_SENT, append_footer


def test_appends_to_both_parts():
    text, html = append_footer("hello", "<p>hello</p>", label=FOOTER_SENT)
    assert text.startswith("hello")
    assert "Sent by Hail.so" in text and "https://hail.so" in text
    assert html.startswith("<p>hello</p>")
    assert 'href="https://hail.so"' in html


def test_none_parts_stay_none():
    text, html = append_footer(None, "<p>x</p>", label=FOOTER_FORWARDED)
    assert text is None
    assert "Forwarded by Hail.so" in html

    text, html = append_footer("x", None, label=FOOTER_FORWARDED)
    assert html is None
    assert "Forwarded by Hail.so" in text
