from pathlib import Path

from hailhq.core.email_mime import parse_mime

FIX = Path(__file__).parent / "fixtures" / "inbound"


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def test_parse_simple():
    p = parse_mime(_read("simple.eml"))
    assert p.from_address == "alice@example.com"
    assert "bob+acme@mail.hail.so" in p.to_addresses
    assert p.subject == "Hello"
    assert p.message_id == "<m1@example.com>"
    assert p.body_text and "Alice" in p.body_text
    assert p.body_html is None
    assert p.attachments == []


def test_parse_multipart_with_attachment():
    p = parse_mime(_read("multipart_attachment.eml"))
    assert p.body_text and "See attached" in p.body_text
    assert len(p.attachments) == 1
    a = p.attachments[0]
    assert a.filename == "report.pdf"
    assert a.content_type == "application/pdf"
    assert a.payload.startswith(b"%PDF-1.4")


def test_parse_threaded():
    p = parse_mime(_read("threaded.eml"))
    assert p.in_reply_to == "<m1@example.com>"
    assert p.references_ids == ["<m1@example.com>", "<m2@example.com>"]


def test_parse_no_message_id_returns_none():
    raw = b"From: x@example.com\r\nTo: y@example.com\r\nSubject: nope\r\n\r\nbody"
    p = parse_mime(raw)
    assert p.message_id is None


def test_parse_extracts_cc():
    raw = (
        b"From: x@example.com\r\n"
        b"To: y@example.com\r\n"
        b"Cc: z@example.com, w@example.com\r\n"
        b"Subject: cc\r\n"
        b"\r\n"
        b"body"
    )
    p = parse_mime(raw)
    assert p.cc_addresses == ["z@example.com", "w@example.com"]


def test_nested_rfc822_is_attachment_not_parent_body():
    parsed = parse_mime(_read("nested_rfc822.eml"))
    assert parsed.body_text.strip() == "Parent body — this must be body_text."
    assert "Inner body" not in (parsed.body_text or "")
    rfc822 = [a for a in parsed.attachments if a.content_type == "message/rfc822"]
    assert len(rfc822) == 1
    assert rfc822[0].filename == "inner.eml"
    assert b"Inner body" in rfc822[0].payload


def test_unknown_charset_does_not_raise():
    parsed = parse_mime(_read("bad_charset.eml"))
    assert "hello with a bad charset label" in (parsed.body_text or "")


def test_parse_html_part_extracted():
    raw = (
        b"From: x@example.com\r\n"
        b"To: y@example.com\r\n"
        b"Subject: html\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"plain body\r\n"
        b"--B\r\n"
        b"Content-Type: text/html\r\n\r\n"
        b"<p>html body</p>\r\n"
        b"--B--\r\n"
    )
    p = parse_mime(raw)
    assert p.body_text and "plain body" in p.body_text
    assert p.body_html and "html body" in p.body_html


def test_multiple_inline_text_parts_are_concatenated():
    raw = (
        b"From: a@x\r\nTo: b@y\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b1"\r\n\r\n'
        b"--b1\r\nContent-Type: text/plain\r\n\r\npart one\r\n"
        b"--b1\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="f.pdf"\r\n\r\nx\r\n'
        b"--b1\r\nContent-Type: text/plain\r\n\r\npart two\r\n"
        b"--b1--\r\n"
    )
    parsed = parse_mime(raw)
    assert "part one" in parsed.body_text
    assert "part two" in parsed.body_text
