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

import base64
import json
import logging
from typing import Any

import anthropic
from hailhq.core.config import settings
from hailhq.core.providers.llm.bedrock import bedrock_client

__all__ = ["READABLE_TYPES", "document_reader_configured", "read_document"]

logger = logging.getLogger(__name__)

# The file types the model reads. A PDF is read page by page (images and text).
READABLE_TYPES = {"image/jpeg", "image/png", "application/pdf"}

# Enough for one form's worth of values. Short on purpose: the answer is JSON.
_MAX_TOKENS = 2048

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
