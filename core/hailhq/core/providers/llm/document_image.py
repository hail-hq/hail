"""Decodes one uploaded document image for the model, in a child process.

The API runs this as ``python -m hailhq.core.providers.llm.document_image
<content type>``, with the image on stdin and no credentials in its
environment, under CPU and memory limits (as the voicebot's
``attachment_decoder.py`` does). It writes a JPEG at most ``LONG_SIDE`` on
its long side, upright as its EXIF tag says, transparent areas on white. A
file that is not its declared type, or too large, exits non-zero.

Pillow (HPND) is AGPL-compatible.
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
# The most pixels decoded, within the child's 1 GB memory limit (100 million
# pixels with transparency is 400 MB). A larger scan (a 600 dpi A4 page is
# 35 million) is decoded, then scaled down. A JPEG is first decoded at a
# reduced scale (``draft``, down to 1/8 per side), so a 48 to 200 million
# pixel phone photo decodes at about 12 million. Its header may claim up to
# ``MAX_HEADER_PIXELS``.
MAX_PIXELS = 100_000_000
MAX_HEADER_PIXELS = 256_000_000
# Pillow decodes only the format the file was declared as, never another
# format it happens to know. Pillow opens a phone's multi-picture JPEG
# through its JPEG opener, as format MPO.
FORMATS = {"image/jpeg": ("JPEG",), "image/png": ("PNG",)}
_MAX_INPUT_BYTES = 16 * 1024 * 1024


def decode(content_type: str, data: bytes) -> bytes:
    """The image as a JPEG for the model. Raises for a file that is not its
    declared format, or too large."""
    with Image.open(io.BytesIO(data), formats=FORMATS[content_type]) as image:
        if image.width * image.height > MAX_HEADER_PIXELS:
            raise ValueError("image too large")
        if image.format in ("JPEG", "MPO"):
            # Decode a large JPEG at a reduced scale from the start.
            image.draft("RGB", (LONG_SIDE, LONG_SIDE))
        # For a JPEG this is the size after draft: what is really decoded.
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("image too large")
        # Scale down first, in place, so every step after works on a small
        # image: a 100 million pixel RGBA scan is 400 MB once decoded, and
        # one more full-size copy would pass the 1 GB limit. The EXIF turn
        # comes after (the bounding box is square, so it fits either way).
        image.thumbnail((LONG_SIDE, LONG_SIDE), reducing_gap=3.0)
        image = ImageOps.exif_transpose(image)
        if image.mode.startswith("I"):
            # 16- or 32-bit grey (scanner output): scale to 8 bits, never clip.
            peak = image.getextrema()[1] or 1
            scale = 255 / peak if peak > 255 else 1
            image = image.convert("I").point(lambda v: v * scale).convert("L")
        if image.mode in ("RGBA", "LA", "P", "PA"):
            # Transparent pixels on white, as a viewer shows them, not black.
            rgba = image.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, "JPEG", quality=85)
        return out.getvalue()


def main() -> None:
    # Native decoders run without application credentials, with time and
    # memory bounds. The pixel limit is raised in this process only: a 200
    # million pixel photo is over Pillow's default before ``draft``.
    try:
        if resource is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
            if sys.platform == "linux":
                resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    except (ValueError, OSError):
        pass
    Image.MAX_IMAGE_PIXELS = MAX_HEADER_PIXELS // 2
    content_type = sys.argv[1]
    data = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if content_type not in FORMATS or len(data) > _MAX_INPUT_BYTES:
        sys.exit(2)
    sys.stdout.buffer.write(decode(content_type, data))


if __name__ == "__main__":
    main()
