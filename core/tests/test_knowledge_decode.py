"""The knowledge file decoder, in process and through run_isolated."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from hailhq.core.isolated_process import run_isolated
from hailhq.core.knowledge.decode import DOCX, DecodeError, decode


def make_pdf(pages: list[str]) -> bytes:
    """A small PDF with one line of Helvetica text per page."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font = 3 + 2 * len(pages)
    for i, line in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> "
            f"/Contents {4 + 2 * i} 0 R >>".encode()
        )
        objects.append(
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % n + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref)
    )
    return out.getvalue()


def make_docx(body_xml: str, *, extra: dict[str, bytes] | None = None) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xml = f'<w:document xmlns:w="{ns}"><w:body>{body_xml}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", xml)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buf.getvalue()


def test_pdf_pages_keep_their_numbers() -> None:
    raw = make_pdf(
        [
            "Mount the NS-5000 inverter on a vertical wall away from the sun.",
            "Error E-204 means the grid voltage is out of range for now.",
        ]
    )
    blocks = decode("application/pdf", raw)["blocks"]
    assert [b["p"] for b in blocks] == [1, 2]
    assert "E-204" in blocks[1]["t"]


def test_pdf_without_text_is_refused() -> None:
    with pytest.raises(DecodeError, match="no text"):
        decode("application/pdf", make_pdf(["", ""]))


def test_docx_headings_paragraphs_and_tables() -> None:
    raw = make_docx(
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Warranty</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>Ten years from purchase.</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>NS-WB-02</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Wall bracket</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    blocks = decode(DOCX, raw)["blocks"]
    assert blocks[0] == {"t": "Warranty", "p": None, "h": True}
    assert blocks[1]["t"] == "Ten years from purchase."
    assert blocks[2]["t"] == "NS-WB-02 | Wall bracket"


def test_docx_with_doctype_is_refused() -> None:
    xml = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]>'
        '<w:document xmlns:w="x"><w:body/></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", xml)
    with pytest.raises(DecodeError, match="XML declarations"):
        decode(DOCX, buf.getvalue())


def test_docx_bomb_stops_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from hailhq.core.knowledge import decode as module

    monkeypatch.setattr(module, "MAX_DOCX_XML_BYTES", 1000)
    raw = make_docx("<w:p><w:r><w:t>" + "a" * 50_000 + "</w:t></w:r></w:p>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            zipfile.ZipFile(io.BytesIO(raw)).read("word/document.xml"),
        )
    with pytest.raises(DecodeError, match="too large"):
        decode(DOCX, buf.getvalue())


def test_not_a_docx() -> None:
    with pytest.raises(DecodeError):
        decode(DOCX, b"PK\x03\x04 not really a zip")


def test_markdown_headings() -> None:
    blocks = decode("text/markdown", b"# Battery\n\nCharge it\nfully.\n")["blocks"]
    assert blocks == [
        {"t": "Battery", "p": None, "h": True},
        {"t": "Charge it fully.", "p": None, "h": False},
    ]


def test_text_must_be_utf8() -> None:
    with pytest.raises(DecodeError):
        decode("text/plain", b"caf\xe9")
    with pytest.raises(DecodeError):
        decode("text/plain", b"a\x00b")


def test_csv_rows_become_cards() -> None:
    raw = "Part;Name;Price\nNS-BAT-48V-100;Battery;1200\n;;\nNS-WB-02;Wall bracket;\n"
    rows = decode("text/csv", raw.encode())["rows"]
    assert rows == [
        {"r": 1, "t": "Part: NS-BAT-48V-100; Name: Battery; Price: 1200"},
        {"r": 3, "t": "Part: NS-WB-02; Name: Wall bracket"},
    ]


def test_text_size_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    from hailhq.core.knowledge import decode as module

    monkeypatch.setattr(module, "MAX_TEXT_BYTES", 100)
    with pytest.raises(DecodeError, match="MB of text"):
        decode("text/plain", ("word " * 100).encode())


async def test_child_process_reads_and_reports_errors() -> None:
    out = await run_isolated(
        "hailhq.core.knowledge.decode",
        "text/plain",
        stdin=b"Hello there.",
        timeout_s=30,
        max_output=1024 * 1024,
    )
    assert json.loads(out)["blocks"][0]["t"] == "Hello there."
    out = await run_isolated(
        "hailhq.core.knowledge.decode",
        "image/png",
        stdin=b"x",
        timeout_s=30,
        max_output=1024 * 1024,
    )
    assert json.loads(out) == {"error": "this file type cannot be read"}
