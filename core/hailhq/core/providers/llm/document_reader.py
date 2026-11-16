"""Reads one uploaded document with Anthropic's vision model and returns the
JSON the caller's schema asks for.

The model is ``DOCUMENT_READER_MODEL``, by default ``ANTHROPIC_BACKUP_MODEL``
(Haiku 5.5: cheap and fast, and it reads images and PDFs). Any model set here
must take thinking "disabled" and structured output. It runs on Amazon Bedrock
when ``ANTHROPIC_BEDROCK_REGION`` is set (then a Bedrock id, like
``eu.anthropic.claude-haiku-5-5``), else on Anthropic's API with
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
import re
import weakref
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
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
# The most pixels decoded. A PNG over this is skipped. A JPEG is first
# decoded at a reduced scale (``draft``, down to 1/8 per side), so a 48 to 200
# million pixel phone photo decodes at a few million; it is skipped only when
# still over this after that. The header alone may claim up to
# ``_MAX_HEADER_PIXELS`` (Pillow refuses more as a decompression bomb).
_MAX_PIXELS = 25_000_000
_MAX_HEADER_PIXELS = 256_000_000
# Pillow refuses an image over twice this at open. Its default (about 179
# million) would refuse a 200 million pixel phone photo before ``draft``.
Image.MAX_IMAGE_PIXELS = _MAX_HEADER_PIXELS // 2
# PDFs are sent as they are, within the providers' limits (checked
# 2026-11-16): Amazon Bedrock takes requests up to 20 MB, Anthropic's API up
# to 128 MB, and at most 100 pages for a model under 1M tokens of context
# (600 at 1M). Hail keeps to the lowest of each, so any model fits.
# Sources: platform.claude.com/docs/en/api/overview (request size limits)
# and platform.claude.com/docs/en/build-with-claude/pdf-support.
_MAX_PDF_REQUEST_BYTES = 20 * 1024 * 1024
_MAX_PDF_PAGES = 100
_PDF_PAGE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
# Pillow decodes only the format the file was declared as, never another
# format it happens to know.
_IMAGE_FORMATS = {"image/jpeg": ("JPEG",), "image/png": ("PNG",)}
# Images are decoded on their own 2 threads, never on asyncio's shared
# pool. A decode waits for a free thread (bounded only by the caller's own
# deadline: another request being busy is no reason to drop a file), then
# has ``_DECODE_LIMIT_S`` from its start. A thread counts as busy until its
# decode really ends, even when the read that asked for it was cancelled.
_DECODE_THREADS = 2
_decoder = ThreadPoolExecutor(
    max_workers=_DECODE_THREADS, thread_name_prefix="document-decode"
)
# One semaphore per event loop (tests run several loops; the API runs one).
_decode_slots: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_DECODE_LIMIT_S = 10.0

_client: anthropic.AsyncAnthropic | anthropic.AsyncAnthropicBedrock | None = None


def document_reader_model() -> str:
    """``DOCUMENT_READER_MODEL``, else ``ANTHROPIC_BACKUP_MODEL``."""
    return settings.document_reader_model or settings.anthropic_backup_model


def document_reader_configured() -> bool:
    """True when a model id and a way to reach it are set."""
    return bool(document_reader_model()) and bool(
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
    formats = _IMAGE_FORMATS[content_type]
    with Image.open(io.BytesIO(data), formats=formats) as image:
        if image.width * image.height > _MAX_HEADER_PIXELS:
            raise ValueError("image too large")
        if image.format == "JPEG":
            # Decode a large JPEG at a reduced scale from the start.
            image.draft("RGB", (_IMAGE_LONG_SIDE, _IMAGE_LONG_SIDE))
        # For a JPEG this is the size after draft: what is really decoded.
        if image.width * image.height > _MAX_PIXELS:
            raise ValueError("image too large")
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


def _pdf_fits(data: bytes) -> bool:
    """True when the PDF is within the providers' size and page limits. The
    pages are counted from the page objects the file shows; a file that
    hides them (compressed object streams) is limited by its size only."""
    if (len(data) + 2) // 3 * 4 > _MAX_PDF_REQUEST_BYTES:
        return False
    return len(_PDF_PAGE.findall(data)) <= _MAX_PDF_PAGES


async def _decode_image(content_type: str, data: bytes) -> bytes:
    """``_image_for_model`` on the decode threads. Waits for a free thread,
    then gives the decode ``_DECODE_LIMIT_S``. The slot is freed only when
    the decode ends, so a cancelled read never lets a third decode run."""
    loop = asyncio.get_running_loop()
    slots = _decode_slots.setdefault(loop, asyncio.Semaphore(_DECODE_THREADS))
    await slots.acquire()
    future = loop.run_in_executor(_decoder, _image_for_model, content_type, data)
    future.add_done_callback(lambda _: slots.release())
    return await asyncio.wait_for(asyncio.shield(future), timeout=_DECODE_LIMIT_S)


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
    before_send: Callable[[], Awaitable[bool]] | None = None,
) -> dict[str, Any] | None:
    """The model's answer to ``prompt`` about the file, as JSON that matches
    ``schema`` (structured output). None when the reader is not configured,
    the file type is not readable or over the limits, ``before_send``
    answers False (no read left today), or the call fails, times out or is
    refused: the caller then goes on without it. ``before_send`` runs only
    when the file is about to go to the model, so only a sent file counts."""
    if not document_reader_configured() or content_type not in READABLE_TYPES:
        return None
    try:
        if content_type == "application/pdf":
            if not _pdf_fits(data):
                logger.warning("document read skipped: PDF over the limits")
                return None
        else:
            data = await _decode_image(content_type, data)
            content_type = "image/jpeg"
        if before_send is not None and not await before_send():
            return None
        response = await _get_client().messages.create(
            model=document_reader_model(),
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
