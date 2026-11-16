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
import json
import logging
import re
import weakref
from collections.abc import Callable
from typing import Any

import anthropic
from hailhq.core.config import settings
from hailhq.core.isolated_process import run_isolated
from hailhq.core.providers.llm.bedrock import bedrock_client

__all__ = ["READABLE_TYPES", "document_reader_configured", "read_document"]

logger = logging.getLogger(__name__)

# The file types the model reads. A PDF is read page by page (images and text).
READABLE_TYPES = {"image/jpeg", "image/png", "application/pdf"}

# Enough for one form's worth of values. Short on purpose: the answer is JSON.
_MAX_TOKENS = 2048
# PDFs are sent as they are, within the providers' limits (checked
# 2026-11-16): Amazon Bedrock takes requests up to 20 MB, Anthropic's API up
# to 128 MB, and at most 100 pages for a model under 1M tokens of context
# (600 at 1M). A verification file is at most 10 MB (about 13.4 MB in
# base64), so the size always fits; the pages are checked, at the lowest
# limit, so any model fits.
# Sources: platform.claude.com/docs/en/api/overview (request size limits)
# and platform.claude.com/docs/en/build-with-claude/pdf-support.
_MAX_PDF_PAGES = 100
_PDF_PAGE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
# Images are decoded in a child process without credentials, under CPU and
# memory limits (document_image.py, run by core/isolated_process.py), at
# most 4 at once. A decode waits for its turn (bounded only by the caller's
# own deadline: another request being busy is no reason to drop a file),
# then has ``_DECODE_LIMIT_S`` from its start; past that its process is
# killed.
_DECODES = 4
_DECODE_LIMIT_S = 10.0
_MAX_DECODED_BYTES = 8 * 1024 * 1024
# One semaphore per event loop (tests run several loops; the API runs one).
_decode_slots: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()

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


def _pdf_fits(data: bytes) -> bool:
    """True when the PDF is within the providers' page limit. The pages are
    counted from the page objects the file shows; a file that hides them
    (compressed object streams) is sent, and the provider refuses it if it
    is over (fail open)."""
    return len(_PDF_PAGE.findall(data)) <= _MAX_PDF_PAGES


async def _run_decoder(content_type: str, data: bytes) -> bytes:
    """``document_image.py`` in a credential-free child process."""
    return await run_isolated(
        "hailhq.core.providers.llm.document_image",
        content_type,
        stdin=data,
        timeout_s=_DECODE_LIMIT_S,
        max_output=_MAX_DECODED_BYTES,
    )


async def _decode_image(content_type: str, data: bytes) -> bytes:
    """Waits for a free decode, then gives it ``_DECODE_LIMIT_S``."""
    loop = asyncio.get_running_loop()
    slots = _decode_slots.setdefault(loop, asyncio.Semaphore(_DECODES))
    async with slots:
        return await _run_decoder(content_type, data)


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
    on_send: Callable[[], None] | None = None,
) -> dict[str, Any] | None:
    """The model's answer to ``prompt`` about the file, as JSON that matches
    ``schema`` (structured output). None when the reader is not configured,
    the file type is not readable or over the limits, or the call fails,
    times out or is refused: the caller then goes on without it.
    ``on_send`` runs just before the file goes to the model: from then on
    the read is used, whatever happens to the call (an error, a timeout, a
    cancellation). A file stopped before that (not readable, over the
    limits, not decoded) never uses one. ``timeout_s`` is the whole budget:
    the model call gets what the decode left of it."""
    if not document_reader_configured() or content_type not in READABLE_TYPES:
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    try:
        if content_type == "application/pdf":
            if not _pdf_fits(data):
                logger.warning("document read skipped: PDF over the limits")
                return None
        else:
            data = await _decode_image(content_type, data)
            content_type = "image/jpeg"
        # Everything that can fail here, before anything leaves: the client,
        # the request and its time.
        client = _get_client()
        request = {
            "model": document_reader_model(),
            "max_tokens": _MAX_TOKENS,
            # Haiku 5.5 takes thinking "disabled": fast, and the task is
            # reading, not reasoning.
            "thinking": {"type": "disabled"},
            "output_config": {
                "effort": "low",
                "format": {"type": "json_schema", "schema": schema},
            },
            "messages": [
                {
                    "role": "user",
                    "content": [
                        _file_block(content_type, data),
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        left = deadline - loop.time()
        if left <= 0:
            logger.warning("document read skipped: no time left")
            return None
        # The read is used from here on, whatever happens to the call.
        if on_send is not None:
            on_send()
        response = await client.messages.create(**request, timeout=left)
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
