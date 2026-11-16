"""Prepares one uploaded document for the model, in a child process.

The API runs this as ``python -m hailhq.core.providers.llm.document_image
<content type>``, with the file on stdin and no credentials in its
environment, under CPU and memory limits (as the voicebot's
``attachment_decoder.py`` does). For an image it writes a JPEG at most
``LONG_SIDE`` on its long side, upright as its EXIF tag says, transparent
areas on white. For a PDF it writes a PDF of its first ``PDF_PAGES_SENT``
pages. A file that is not its declared type, too large, or a PDF of more
than ``MAX_PDF_PAGES`` pages exits non-zero.

Pillow (HPND) and pypdfium2/PDFium (Apache-2.0/BSD) are AGPL-compatible.
PDFium: https://pypdfium2.readthedocs.io/en/stable/python_api.html
"""

from __future__ import annotations

import io
import sys

# Registered by import: ``formats`` only accepts formats Pillow has loaded.
from PIL import Image, ImageOps, JpegImagePlugin, PngImagePlugin  # noqa: F401

try:
    import resource
except ImportError:  # not on every platform
    resource = None

# The model scales a larger image down to this long side anyway, and the
# providers refuse large images (about 5 MB on Anthropic's API, less on
# Bedrock).
LONG_SIDE = 1568
# The child's memory limit. The API server has little memory and no swap, and
# live calls run beside it: one decode at a time, at most this much.
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
# The most pixels decoded: 40 million pixels with transparency is 160 MB
# (a palette image converted is 200 MB at most), well in the limit. A 600
# dpi A4 scan is 35 million. A larger PNG is refused (fail open).
MAX_PIXELS = 40_000_000
# A baseline JPEG is decoded at a reduced scale from the start (``draft``,
# down to 1/8 per side), line by line, so its header may claim up to
# ``MAX_HEADER_PIXELS``: a 200 million pixel phone photo decodes at about 3
# million. A progressive JPEG keeps all of its data at full size while it
# decodes (up to 6 bytes a pixel), so it may claim only ``MAX_PIXELS``.
MAX_HEADER_PIXELS = 256_000_000
# A PDF: only its first pages go to the model (the check needs the pages
# that show who it is about); a longer PDF is not a document to check, and
# is not read at all.
PDF_PAGES_SENT = 3
MAX_PDF_PAGES = 5
# Pillow decodes only the format the file was declared as, never another
# format it happens to know. Pillow opens a phone's multi-picture JPEG
# through its JPEG opener, as format MPO.
FORMATS = {"image/jpeg": ("JPEG",), "image/png": ("PNG",)}
CONTENT_TYPES = {*FORMATS, "application/pdf"}
_MAX_INPUT_BYTES = 16 * 1024 * 1024


def decode(content_type: str, data: bytes) -> bytes:
    """The image as a JPEG for the model. Raises for a file that is not its
    declared format, or too large."""
    with Image.open(io.BytesIO(data), formats=FORMATS[content_type]) as image:
        if image.width * image.height > MAX_HEADER_PIXELS:
            raise ValueError("image too large")
        if image.format in ("JPEG", "MPO"):
            progressive = image.info.get("progressive") or image.info.get("progression")
            if progressive and image.width * image.height > MAX_PIXELS:
                raise ValueError("image too large")
            # Decode a large JPEG at a reduced scale from the start.
            image.draft("RGB", (LONG_SIDE, LONG_SIDE))
        # For a JPEG this is the size after draft: what is really decoded.
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("image too large")
        # Scale down first, in place, so every step after works on a small
        # image: no second full-size copy. The EXIF turn comes after (the
        # bounding box is square, so it fits either way). Pillow scales
        # palette and 1-bit images with NEAREST, which breaks small print:
        # those are converted first (in the limit, see MAX_PIXELS).
        if image.mode in ("P", "PA"):
            image = image.convert("RGBA")
        elif image.mode == "1":
            image = image.convert("L")
        image.thumbnail((LONG_SIDE, LONG_SIDE), reducing_gap=3.0)
        image = ImageOps.exif_transpose(image)
        if image.mode.startswith("I"):
            # 16- or 32-bit grey (scanner output): scale to 8 bits, never clip.
            peak = image.getextrema()[1] or 1
            scale = 255 / peak if peak > 255 else 1
            image = image.convert("I").point(lambda v: v * scale).convert("L")
        if image.mode in ("RGBA", "LA"):
            # Transparent pixels on white, as a viewer shows them, not black.
            rgba = image.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, "JPEG", quality=85)
        return out.getvalue()


def first_pages(data: bytes) -> bytes:
    """A PDF of the first ``PDF_PAGES_SENT`` pages. Raises for a file that is
    not a PDF, or of more than ``MAX_PDF_PAGES`` pages."""
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(data) as pdf:
        pages = len(pdf)
        if pages < 1 or pages > MAX_PDF_PAGES:
            raise ValueError("PDF page count over the limit")
        out = pdfium.PdfDocument.new()
        try:
            out.import_pages(pdf, list(range(min(pages, PDF_PAGES_SENT))))
            buf = io.BytesIO()
            out.save(buf)
            return buf.getvalue()
        finally:
            out.close()


def prepare(content_type: str, data: bytes) -> bytes:
    """The file as it goes to the model."""
    if content_type == "application/pdf":
        return first_pages(data)
    return decode(content_type, data)


def main() -> None:
    # Native decoders run without application credentials, with time and
    # memory bounds. The pixel limit is raised in this process only: a 200
    # million pixel photo is over Pillow's default before ``draft``.
    try:
        if resource is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
            if sys.platform == "linux":
                resource.setrlimit(
                    resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES)
                )
    except (ValueError, OSError):
        pass
    Image.MAX_IMAGE_PIXELS = MAX_HEADER_PIXELS // 2
    content_type = sys.argv[1]
    data = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if content_type not in CONTENT_TYPES or len(data) > _MAX_INPUT_BYTES:
        sys.exit(2)
    sys.stdout.buffer.write(prepare(content_type, data))


if __name__ == "__main__":
    main()
