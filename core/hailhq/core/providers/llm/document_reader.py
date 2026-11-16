"""Reads one uploaded document with Anthropic's vision model and returns the
JSON the caller's schema asks for.

The model is ``ANTHROPIC_BACKUP_MODEL`` (Haiku 5.5: cheap and fast, and it
reads images and PDFs). It runs on Amazon Bedrock when
``ANTHROPIC_BEDROCK_REGION`` is set, else on Anthropic's API with
``ANTHROPIC_API_KEY``. With neither, or with no model id, nothing is read.

The document goes to the model provider and nowhere else. Nothing here logs
the file, the prompt or the answer: only the exception type on a failure.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import threading
from typing import Any

import anthropic
from hailhq.core.config import settings
from hailhq.core.providers.llm.bedrock import bedrock_client
from PIL import Image, ImageOps

__all__ = ["READABLE_TYPES", "document_reader_configured", "read_document"]

logger = logging.getLogger(__name__)

# The file types the model reads. A PDF is read page by page (images and text).
READABLE_TYPES = {"image/jpeg", "image/png", "application/pdf"}

# Enough for one form's worth of values. Short on purpose: the answer is JSON.
_MAX_TOKENS = 2048
# The model scales a larger image down to this long side anyway, and the
# providers refuse large images (about 5 MB on Anthropic's API, less on
# Bedrock). A phone photo is sent at this size, upright as a viewer shows it.
_IMAGE_LONG_SIDE = 1568
# Refuse to decode an image larger than this many pixels (a decompression
# bomb). A phone photo is about 12 to 24 million.
_MAX_PIXELS = 25_000_000
# Pillow decodes only the format the file was declared as, never another
# format it happens to know.
_IMAGE_FORMATS = {"image/jpeg": ("JPEG",), "image/png": ("PNG",)}
# Images decoded at once, so a burst of uploads cannot take every thread.
# Held by the decoding thread itself: a read cancelled at the time limit
# leaves its thread running, and the slot stays taken until it ends.
_decodes = threading.BoundedSemaphore(2)
_DECODE_WAIT_S = 10.0

_client: anthropic.AsyncAnthropic | anthropic.AsyncAnthropicBedrock | None = None


def document_reader_configured() -> bool:
    """True when a model id and a way to reach it are set."""
    return bool(settings.anthropic_backup_model) and bool(
        settings.anthropic_bedrock_region or settings.anthropic_api_key
    )


def _get_client() -> anthropic.AsyncAnthropic | anthropic.AsyncAnthropicBedrock:
    """One client per process. No retries: the caller has a short time limit
    and fails open."""
    global _client
    if _client is None:
        if settings.anthropic_bedrock_region:
            _client = bedrock_client(settings.anthropic_bedrock_region)
        else:
            _client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key, max_retries=0
            )
    return _client


def _image_for_model(content_type: str, data: bytes) -> bytes:
    """The image as a JPEG at most ``_IMAGE_LONG_SIDE`` on its long side,
    turned as its EXIF tag says, so the model sees what a person sees.
    Raises for a file that is not its declared format, or too large."""
    if not _decodes.acquire(timeout=_DECODE_WAIT_S):
        raise TimeoutError("too many images decoding")
    try:
        return _decode(content_type, data)
    finally:
        _decodes.release()


def _decode(content_type: str, data: bytes) -> bytes:
    formats = _IMAGE_FORMATS[content_type]
    with Image.open(io.BytesIO(data), formats=formats) as image:
        if image.width * image.height > _MAX_PIXELS:
            raise ValueError("image too large")
        if image.format == "JPEG":
            # Decode a large JPEG at a reduced scale from the start.
            image.draft("RGB", (_IMAGE_LONG_SIDE, _IMAGE_LONG_SIDE))
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA", "P", "PA"):
            # Transparent pixels on white, as a viewer shows them, not black.
            rgba = image.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = image.convert("RGB")
        image.thumbnail((_IMAGE_LONG_SIDE, _IMAGE_LONG_SIDE), reducing_gap=3.0)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=85)
        return out.getvalue()


def _file_block(content_type: str, data: bytes) -> dict[str, Any]:
    source = {
        "type": "base64",
        "media_type": content_type,
        "data": base64.b64encode(data).decode(),
    }
    if content_type == "application/pdf":
        return {"type": "document", "source": source}
    return {"type": "image", "source": source}


async def read_document(
    *,
    content_type: str,
    data: bytes,
    prompt: str,
    schema: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any] | None:
    """The model's answer to ``prompt`` about the file, as JSON that matches
    ``schema`` (structured output). None when the reader is not configured,
    the file type is not readable, or the call fails, times out or is
    refused: the caller then goes on without it."""
    if not document_reader_configured() or content_type not in READABLE_TYPES:
        return None
    try:
        if content_type != "application/pdf":
            data = await asyncio.to_thread(_image_for_model, content_type, data)
            content_type = "image/jpeg"
        response = await _get_client().messages.create(
            model=settings.anthropic_backup_model,
            max_tokens=_MAX_TOKENS,
            # Haiku 5.5 takes thinking "disabled": fast, and the task is
            # reading, not reasoning.
            thinking={"type": "disabled"},
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": schema},
            },
            messages=[
                {
                    "role": "user",
                    "content": [
                        _file_block(content_type, data),
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            timeout=timeout_s,
        )
    except Exception as exc:
        # Fail open: no warning, no prefill. Never the message: it can quote
        # the request.
        logger.warning("document read failed (%s)", type(exc).__name__)
        return None
    if response.stop_reason != "end_turn":
        logger.warning("document read stopped early (%s)", response.stop_reason)
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        answer = json.loads(text)
    except ValueError:
        logger.warning("document read returned no JSON")
        return None
    return answer if isinstance(answer, dict) else None
