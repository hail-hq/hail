"""Reads one knowledge file into paragraphs or CSV rows, in a child process.

Run by ``hailhq.core.knowledge.ingest`` through ``run_isolated``
(``core/hailhq/core/isolated_process.py``): ``python -m
hailhq.core.knowledge.decode <content_type>`` with the file on stdin. The
child has no credentials, sets its own CPU and memory limits, and is killed
when its time is up. It writes JSON on stdout:

* prose: ``{"blocks": [{"t": text, "p": page or null, "h": heading}]}``
* CSV: ``{"rows": [{"r": row number, "t": "Header: value; Header: value"}]}``

A file it cannot read gives ``{"error": reason}`` instead, a short reason
the worker shows the customer (exit status 0, so ``run_isolated`` passes it
on); a crash or a kill gives nothing. Every decoded size
is capped (``MAX_TEXT_BYTES``), so a zip bomb or a huge PDF stops early.

* PDF: pypdfium2 (PDFium), text only. A PDF with almost no text (a scan)
  is refused: there is no OCR in v1.
* DOCX: the zip's ``word/document.xml``, read with a size cap, no DTDs or
  entities. Paragraph styles named Heading or Title are headings; table rows
  are read as "cell | cell".
* TXT and Markdown: UTF-8 (a BOM is fine). Markdown ``#`` lines are headings.
* CSV: one row card per data row, the first row being the header.

pypdfium2/PDFium are Apache-2.0/BSD (AGPL-compatible). Imports no Hail code
that touches credentials or the network.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from xml.etree import ElementTree

import pypdfium2 as pdfium

try:
    import resource
except ImportError:  # pragma: no cover - not on Linux/macOS
    resource = None

__all__ = ["DecodeError", "decode"]

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# The most text one file may give, in UTF-8 bytes.
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 2000
MAX_DOCX_XML_BYTES = 32 * 1024 * 1024
MAX_ZIP_ENTRIES = 2000
MAX_CSV_ROWS = 100_000
MAX_CSV_COLUMNS = 100
MAX_CELL_CHARS = 300
MAX_CARD_CHARS = 2000
# A PDF needs at least this much text per page on average, or it is a scan.
MIN_PDF_CHARS_PER_PAGE = 20
_CPU_SECONDS = 30
_MEMORY_BYTES = 1024**3

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DecodeError(ValueError):
    """A file that cannot be read; the message is for the customer."""


class _Budget:
    def __init__(self) -> None:
        self.used = 0

    def take(self, text: str) -> str:
        self.used += len(text.encode("utf-8")) + 1
        if self.used > MAX_TEXT_BYTES:
            raise DecodeError(
                f"the file has more than {MAX_TEXT_BYTES // (1024 * 1024)} MB of text"
            )
        return text


def _text(raw: bytes) -> str:
    if b"\x00" in raw:
        raise DecodeError("the file is not text")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DecodeError("the file is not UTF-8 text") from None


def _paragraphs(text: str, page: int | None, budget: _Budget, *, markdown: bool):
    blocks = []
    lines: list[str] = []

    def end() -> None:
        if lines:
            joined = " ".join(lines).strip()
            if joined:
                blocks.append({"t": budget.take(joined), "p": page, "h": False})
            lines.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            end()
        elif markdown and stripped.startswith("#"):
            end()
            title = stripped.lstrip("#").strip()
            if title:
                blocks.append({"t": budget.take(title), "p": page, "h": True})
        else:
            lines.append(stripped)
    end()
    return blocks


def _pdf(raw: bytes, budget: _Budget):
    try:
        pdf = pdfium.PdfDocument(raw)
    except Exception:
        raise DecodeError("the PDF could not be opened") from None
    with pdf:
        pages = len(pdf)
        if pages > MAX_PDF_PAGES:
            raise DecodeError(f"the PDF has more than {MAX_PDF_PAGES} pages")
        blocks = []
        chars = 0
        for index in range(pages):
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range()
                finally:
                    textpage.close()
            finally:
                page.close()
            chars += len(text.strip())
            blocks.extend(_paragraphs(text, index + 1, budget, markdown=False))
    if pages == 0 or chars < MIN_PDF_CHARS_PER_PAGE * pages:
        raise DecodeError(
            "the PDF has little or no text (a scan?); only PDFs with text can "
            "be read"
        )
    return blocks


def _docx(raw: bytes, budget: _Budget):
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise DecodeError("the DOCX file could not be opened") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise DecodeError("the DOCX file has too many parts")
        try:
            info = archive.getinfo("word/document.xml")
        except KeyError:
            raise DecodeError("the file is not a Word document") from None
        if info.file_size > MAX_DOCX_XML_BYTES:
            raise DecodeError("the Word document is too large once unpacked")
        # The size in the header can lie: read at most the cap, whatever it says.
        with archive.open(info) as part:
            xml = part.read(MAX_DOCX_XML_BYTES + 1)
        if len(xml) > MAX_DOCX_XML_BYTES:
            raise DecodeError("the Word document is too large once unpacked")
    # Entities need a DOCTYPE (always upper case in XML): none is allowed.
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise DecodeError("the Word document has unsupported XML declarations")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise DecodeError("the Word document could not be read") from None
    body = root.find(f"{_W}body")
    blocks: list[dict] = []
    if body is not None:
        for kind, node in _docx_blocks(body):
            if kind == "p":
                text = _docx_text(node).strip()
                if text:
                    style = node.find(f"{_W}pPr/{_W}pStyle")
                    name = (style.get(f"{_W}val") or "") if style is not None else ""
                    heading = name.lower().startswith(("heading", "title"))
                    blocks.append({"t": budget.take(text), "p": None, "h": heading})
            else:
                for row in _unwrapped(node, "tr"):
                    cells = [_docx_text(c).strip() for c in _unwrapped(row, "tc")]
                    line = " | ".join(c for c in cells if c)
                    if line:
                        blocks.append({"t": budget.take(line), "p": None, "h": False})
    return blocks


# Elements that only wrap content: a content control and custom XML.
_WRAPPERS = (f"{_W}sdt", f"{_W}customXml")


def _unwrapped(node, tag: str):
    """``node``'s children named ``tag``, looking through content controls
    and custom XML, never into a nested table."""
    for child in node:
        if child.tag == f"{_W}{tag}":
            yield child
        elif child.tag in _WRAPPERS:
            inner = child.find(f"{_W}sdtContent")
            yield from _unwrapped(inner if inner is not None else child, tag)


def _docx_blocks(node):
    """The paragraphs and tables of a body or cell, in order, through
    wrappers: ("p", paragraph) or ("tbl", table)."""
    for child in node:
        if child.tag == f"{_W}p":
            yield "p", child
        elif child.tag == f"{_W}tbl":
            yield "tbl", child
        elif child.tag in _WRAPPERS:
            inner = child.find(f"{_W}sdtContent")
            yield from _docx_blocks(inner if inner is not None else child)


def _docx_text(node) -> str:
    """The text of a paragraph, or of a cell: its paragraphs and nested
    tables joined with spaces (a nested row as "cell | cell")."""
    if node.tag == f"{_W}p":
        return "".join(
            (t.text or "") if t.tag == f"{_W}t" else "\t" if t.tag == f"{_W}tab" else ""
            for t in node.iter()
        )
    parts = []
    for kind, child in _docx_blocks(node):
        if kind == "p":
            parts.append(_docx_text(child).strip())
        else:
            for row in _unwrapped(child, "tr"):
                cells = [_docx_text(c).strip() for c in _unwrapped(row, "tc")]
                parts.append(" | ".join(c for c in cells if c))
    return " ".join(p for p in parts if p)


def _csv(raw: bytes, budget: _Budget):
    text = _text(raw)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise DecodeError("the CSV file is empty") from None
    except csv.Error:
        raise DecodeError("the CSV file could not be read") from None
    header = [h.strip()[:MAX_CELL_CHARS] for h in header[:MAX_CSV_COLUMNS]]
    rows = []
    try:
        for number, row in enumerate(reader, start=1):
            if number > MAX_CSV_ROWS:
                raise DecodeError(f"the CSV file has more than {MAX_CSV_ROWS} rows")
            fields = []
            for index, value in enumerate(row[:MAX_CSV_COLUMNS]):
                value = " ".join(value.split())[:MAX_CELL_CHARS]
                if not value:
                    continue
                name = header[index] if index < len(header) and header[index] else ""
                fields.append(f"{name}: {value}" if name else value)
            if fields:
                card = "; ".join(fields)[:MAX_CARD_CHARS]
                rows.append({"r": number, "t": budget.take(card)})
    except csv.Error:
        raise DecodeError("the CSV file could not be read") from None
    return rows


def decode(content_type: str, raw: bytes) -> dict:
    """The file's blocks or rows (see the module docstring). Raises
    ``DecodeError`` with a reason for the customer."""
    budget = _Budget()
    if content_type == "application/pdf":
        blocks = _pdf(raw, budget)
    elif content_type == DOCX:
        blocks = _docx(raw, budget)
    elif content_type in ("text/plain", "text/markdown"):
        blocks = _paragraphs(
            _text(raw), None, budget, markdown=content_type == "text/markdown"
        )
    elif content_type == "text/csv":
        rows = _csv(raw, budget)
        if not rows:
            raise DecodeError("the CSV file has no rows under its header")
        return {"rows": rows}
    else:
        raise DecodeError("this file type cannot be read")
    if not blocks:
        raise DecodeError("the file has no text")
    return {"blocks": blocks}


def main() -> None:
    try:
        if resource is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (_CPU_SECONDS, _CPU_SECONDS))
            if sys.platform == "linux":
                resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_BYTES, _MEMORY_BYTES))
    except (ValueError, OSError):
        pass
    content_type = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = sys.stdin.buffer.read()
    try:
        result = decode(content_type, raw)
    except DecodeError as exc:
        sys.stdout.write(json.dumps({"error": str(exc)}))
        return
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
