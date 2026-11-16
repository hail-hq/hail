"""Reads one uploaded document with Anthropic's vision model and returns the
JSON the caller's schema asks for.

The models are tried in this order, each id once (``_models``):

1. ``DOCUMENT_READER_MODEL``, by default ``ANTHROPIC_BACKUP_MODEL`` (Haiku
   5.5: cheap and fast, and it reads images and PDFs), with thinking
   "disabled".
2. ``ANTHROPIC_BACKUP_MODEL``, with thinking "disabled".
3. ``ANTHROPIC_MODEL``, with thinking "between_tools": Sonnet 5.5 refuses
   "disabled" with a 400. Probed on 2026-11-16 on Bedrock eu-central-1:
   ``eu.anthropic.claude-haiku-5-5`` reads a PNG and a PDF with "disabled";
   ``eu.anthropic.claude-sonnet-5-5`` refuses "disabled" and reads both with
   "between_tools".

Every model also needs image and PDF input and structured output (effort
"low", a JSON schema). The next model is tried only when the call before it
raised an API error (a refusal of the request, a missing model, a server or
connection error), never after a timeout, and only while ``_MIN_SEND_S`` is
left of the same deadline. One read is used for all the tries. A model that
cannot take the request is harmless: its try fails and the read fails open.
They run on Amazon Bedrock when ``ANTHROPIC_BEDROCK_REGION`` is set (then
Bedrock ids, like ``eu.anthropic.claude-haiku-5-5``), else on Anthropic's
API with ``ANTHROPIC_API_KEY``. With neither, or with no model id, nothing
is read. Every model is Anthropic's: the documents never go to another
provider.

The document goes to the model provider and nowhere else. Nothing here logs
the file, the prompt or the answer: only the exception type on a failure,
and which try it was.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
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
# Every file is prepared in a child process without credentials, under CPU
# and memory limits (document_image.py, run by core/isolated_process.py):
# an image is scaled down to a JPEG; a PDF of at most 5 pages is cut to its
# first 3 (a longer one is not read). One at a time per API process: the
# server has little memory and no swap, and live calls run beside it. A
# file waits for its turn (bounded only by the caller's own deadline:
# another request being busy is no reason to drop a file), then has
# ``_DECODE_LIMIT_S`` from its start; past that its process is killed.
# The providers' limits (Bedrock requests up to 20 MB, at most 100 PDF
# pages) are far above what is sent.
_DECODES = 1
_DECODE_LIMIT_S = 10.0
# The least time left at which a file is still sent to the model.
_MIN_SEND_S = 1.0
_MAX_DECODED_BYTES = 8 * 1024 * 1024
# The first pages of a PDF of at most 10 MB; 12 MB is 16 MB in base64,
# under Bedrock's 20 MB request limit.
_MAX_PDF_BYTES = 12 * 1024 * 1024
# One semaphore per event loop (tests run several loops; the API runs one).
_decode_slots: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()

_client: anthropic.AsyncAnthropic | anthropic.AsyncAnthropicBedrock | None = None

# Thinking off. Sonnet 5.5 takes only "between_tools" for that.
_NO_THINKING = {"type": "disabled"}
_BETWEEN_TOOLS = {"type": "between_tools"}


def document_reader_model() -> str:
    """``DOCUMENT_READER_MODEL``, else ``ANTHROPIC_BACKUP_MODEL``."""
    return settings.document_reader_model or settings.anthropic_backup_model


def _models() -> list[tuple[str, dict[str, str]]]:
    """The models to try, in order, each with the thinking value it takes:
    see the module docstring. Each id once, at its first place."""
    chain = [
        (document_reader_model(), _NO_THINKING),
        (settings.anthropic_backup_model, _NO_THINKING),
        (settings.anthropic_model, _BETWEEN_TOOLS),
    ]
    out: list[tuple[str, dict[str, str]]] = []
    for model, thinking in chain:
        if model and model not in {m for m, _ in out}:
            out.append((model, thinking))
    return out


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


async def _run_decoder(content_type: str, data: bytes, limit_s: float) -> bytes:
    """``document_image.py`` in a credential-free child process."""
    return await run_isolated(
        "hailhq.core.providers.llm.document_image",
        content_type,
        stdin=data,
        timeout_s=limit_s,
        max_output=(
            _MAX_PDF_BYTES if content_type == "application/pdf" else _MAX_DECODED_BYTES
        ),
    )


async def _decode_image(content_type: str, data: bytes, deadline: float) -> bytes:
    """Waits for a free decode, then gives it ``_DECODE_LIMIT_S``, or what is
    left of the read's time when less. With under ``_MIN_SEND_S`` left, it
    does not start."""
    loop = asyncio.get_running_loop()
    slots = _decode_slots.get(loop)
    if slots is None:
        slots = _decode_slots[loop] = asyncio.Semaphore(_DECODES)
    async with slots:
        left = deadline - loop.time()
        if left < _MIN_SEND_S:
            raise TimeoutError("no time left to decode")
        return await _run_decoder(content_type, data, min(_DECODE_LIMIT_S, left))


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
    limits, not decoded) never uses one; a fallback model never uses a
    second one. ``timeout_s`` is the whole budget: the model calls get what
    the decode left of it."""
    if not document_reader_configured() or content_type not in READABLE_TYPES:
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    try:
        # A PDF over 5 pages, or an image over the limits, fails here: no
        # read is used.
        data = await _decode_image(content_type, data, deadline)
        if content_type != "application/pdf":
            content_type = "image/jpeg"
        # Everything that can fail here, before anything leaves: the client,
        # the request and its time.
        client = _get_client()
        models = _models()
        # Thinking off (each model's own value, in ``models``): fast, and the
        # task is reading, not reasoning.
        request = {
            "max_tokens": _MAX_TOKENS,
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
        if left < _MIN_SEND_S:
            # Too little time for any answer: do not use a read on it.
            logger.warning("document read skipped: no time left")
            return None
        # The read is used from here on, whatever happens to the calls.
        if on_send is not None:
            on_send()
    except Exception as exc:
        # Fail open: no warning, no prefill. Never the message: it can quote
        # the request.
        logger.warning("document read failed (%s)", type(exc).__name__)
        return None
    response = None
    for index, (model, thinking) in enumerate(models):
        if index:
            left = deadline - loop.time()
            if left < _MIN_SEND_S:
                logger.warning("document read: no time left for try %d", index)
                return None
        try:
            response = await client.messages.create(
                **request, model=model, thinking=thinking, timeout=left
            )
            break
        except (anthropic.APITimeoutError, TimeoutError):
            # A slow model: another one would not finish in time either.
            logger.warning("document read timed out (try %d)", index)
            return None
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            # A refused request, a missing or broken model: the next one.
            logger.warning(
                "document read failed (%s, try %d)", type(exc).__name__, index
            )
        except Exception as exc:
            logger.warning(
                "document read failed (%s, try %d)", type(exc).__name__, index
            )
            return None
    if response is None:
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
