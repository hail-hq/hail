"""Bounded file decoding in a credential-free child process.

PDFium: https://pypdfium2.readthedocs.io/en/stable/python_api.html
Pillow (HPND) and pypdfium2/PDFium (Apache-2.0/BSD) are AGPL-compatible.
This module does not import the voice pipeline or model clients.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import zipfile
from xml.etree import ElementTree

import pypdfium2 as pdfium
from hailhq.core.email_attachment_limits import AGENT_ATTACHMENT_READ_BYTES
from PIL import Image, ImageOps

try:
    import resource
except ImportError:
    resource = None

MAX_BYTES = AGENT_ATTACHMENT_READ_BYTES
MAX_PIXELS = 40_000_000
MAX_EDGE = 1600
MAX_DECODED_BYTES = 4 * 1024 * 1024
TEXT_BYTES = 24000
MAX_EXPANDED_TEXT_BYTES = 32 * 1024 * 1024


def _image(image):
    if image.width * image.height > MAX_PIXELS:
        raise ValueError("Image dimensions exceed the reading limit")
    image = ImageOps.exif_transpose(image)
    image.thumbnail((MAX_EDGE, MAX_EDGE))
    buffer = io.BytesIO()
    rgba = image.convert("RGBA")
    flattened = Image.new("RGB", image.size, "white")
    flattened.paste(rgba, mask=rgba.getchannel("A"))
    flattened.save(buffer, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode(
        "ascii"
    )


def _text_chunk(text, offset=0, budget=TEXT_BYTES):
    encoded = text.encode("utf-8")
    if offset < 0 or offset > len(encoded):
        raise ValueError("That text offset is not in the file")
    # Decode strictly at the start, so a caller cannot skip half a character.
    remaining = encoded[offset:].decode("utf-8")
    chunk = remaining.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    next_offset = offset + len(chunk.encode("utf-8"))
    return chunk, next_offset if next_offset < len(encoded) else None


def _fits(images, text):
    # Reserve space for range/cursor metadata and JSON framing as well as bytes
    # sent to the model. Counting JSON also accounts for escaped text characters.
    return (
        len(json.dumps({"images": images, "text": text}).encode()) + 1024
        <= MAX_DECODED_BYTES
    )


def decode_file(data):
    raw = base64.b64decode(data["base64"], validate=True)
    if len(raw) > MAX_BYTES:
        raise ValueError("File exceeds the reading limit")
    kind = data["content_type"]
    if kind == "application/pdf":
        with pdfium.PdfDocument(raw) as pdf:
            start = int(data.get("page", 1)) - 1
            if start < 0 or start >= len(pdf):
                raise ValueError("That page is not in the PDF")
            images = []
            texts = []
            continuation = None
            end = start
            offset = int(data.get("text_offset", 0))
            for index in range(start, len(pdf)):
                page = pdf[index]
                try:
                    width, height = page.get_size()
                    if width <= 0 or height <= 0:
                        raise ValueError("Invalid page dimensions")
                    textpage = page.get_textpage()
                    try:
                        remaining = (
                            TEXT_BYTES
                            - len("\n".join(texts).encode("utf-8"))
                            - bool(texts)
                        )
                        text, next_offset = _text_chunk(
                            textpage.get_text_range(), offset, max(0, remaining)
                        )
                    finally:
                        textpage.close()
                    bitmap = page.render(scale=min(2, MAX_EDGE / max(width, height)))
                    try:
                        image = _image(bitmap.to_pil())
                    finally:
                        bitmap.close()
                    if not _fits([*images, image], "\n".join([*texts, text])):
                        if not images:
                            raise ValueError("Decoded page exceeds the reading limit")
                        continuation = {"page": index + 1, "text_offset": offset}
                        break
                    images.append(image)
                    texts.append(text)
                    end = index + 1
                    if next_offset is not None:
                        continuation = {"page": index + 1, "text_offset": next_offset}
                        break
                    offset = 0
                finally:
                    page.close()
            result = {
                "images": images,
                "text": "\n".join(texts),
                "pages": f"{start+1}-{end} of {len(pdf)}",
                "partial": start > 0
                or int(data.get("text_offset", 0)) > 0
                or continuation is not None,
            }
            if continuation:
                result["continuation"] = continuation
            return result
    if kind in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ):
        pieces = []
        expanded = 0
        documents = {}
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = [
                info
                for info in archive.infolist()
                if info.filename == "word/document.xml"
                or info.filename == "xl/sharedStrings.xml"
                or info.filename.startswith("xl/worksheets/sheet")
                and info.filename.endswith(".xml")
            ]
            if len(entries) > 50:
                raise ValueError("Too many document sheets")
            for entry in entries:
                expanded += entry.file_size
                if expanded > 5 * 1024 * 1024:
                    raise ValueError("Expanded document exceeds the reading limit")
                xml = archive.read(entry)
                if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                    raise ValueError("Document contains unsupported XML declarations")
                root = ElementTree.fromstring(xml)
                documents[entry.filename] = root
            strings = []
            for name, root in documents.items():
                if name == "xl/sharedStrings.xml":
                    strings = [
                        "".join(
                            n.text or ""
                            for n in item.iter()
                            if n.tag.rsplit("}", 1)[-1] == "t"
                        )
                        for item in root
                        if item.tag.rsplit("}", 1)[-1] == "si"
                    ]
            for name, root in documents.items():
                if name == "word/document.xml":
                    for paragraph in root.iter():
                        if paragraph.tag.rsplit("}", 1)[-1] == "p":
                            pieces.append(
                                "".join(
                                    n.text or ""
                                    for n in paragraph.iter()
                                    if n.tag.rsplit("}", 1)[-1] == "t"
                                )
                            )
                elif name.startswith("xl/worksheets/"):
                    pieces.append(name.rsplit("/", 1)[-1])
                    for cell in root.iter():
                        if cell.tag.rsplit("}", 1)[-1] != "c":
                            continue
                        value = "".join(
                            n.text or ""
                            for n in cell.iter()
                            if n.tag.rsplit("}", 1)[-1] in ("v", "t")
                        )
                        if cell.get("t") == "s":
                            index = int(value)
                            if index < 0 or index >= len(strings):
                                raise ValueError("Invalid shared string reference")
                            value = strings[index]
                        pieces.append(f"{cell.get('r', 'cell')}: {value}")
        if (
            sum(len(piece.encode("utf-8")) + 1 for piece in pieces)
            > MAX_EXPANDED_TEXT_BYTES
        ):
            raise ValueError("Expanded document text exceeds the reading limit")
        offset = int(data.get("text_offset", 0))
        text, next_offset = _text_chunk("\n".join(pieces), offset)
        result = {
            "images": [],
            "text": text,
            "partial": offset > 0 or next_offset is not None,
        }
        if next_offset is not None:
            result["continuation"] = {"text_offset": next_offset}
        return result
    if kind in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        with Image.open(io.BytesIO(raw)) as image:
            frame = int(data.get("page", 1))
            frames = getattr(image, "n_frames", 1)
            if frame < 1 or frame > frames:
                raise ValueError("That frame is not in the image")
            image.seek(frame - 1)
            images = [_image(image)]
            if not _fits(images, ""):
                raise ValueError("Decoded image exceeds the reading limit")
            result = {"images": images, "text": "", "partial": frames > 1}
            if frame < frames:
                result["continuation"] = {"page": frame + 1}
            return result
    raise ValueError(
        "This file type cannot be read. The original can still be reattached"
    )


def main():
    # Native parsers run without application credentials, with time/memory bounds.
    try:
        if resource is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
            if sys.platform == "linux":
                resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    except (ValueError, OSError):
        pass
    data = json.loads(sys.stdin.buffer.read(15 * 1024 * 1024))
    sys.stdout.write(json.dumps(decode_file(data)))


if __name__ == "__main__":
    main()
