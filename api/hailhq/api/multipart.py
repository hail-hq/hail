"""Multipart bodies read into memory with a size cap checked while reading.

Used by POST /verifications and POST /verifications/sms/evidence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from hailhq.api.errors import unprocessable
from starlette.datastructures import FormData
from starlette.formparsers import MultiPartException, MultiPartParser

# The largest cap any caller passes.
_MAX_ANY_BYTES = 30 * 1024 * 1024


class _InMemoryMultiPartParser(MultiPartParser):
    """Starlette spools any file part over 1 MB to a temporary file on disk.
    Every body is capped at _MAX_ANY_BYTES or less, so with this limit no part
    can reach the spool size and every byte stays in memory."""

    spool_max_size = _MAX_ANY_BYTES + 1


async def _capped_body(
    request: Request, max_bytes: int, too_large: str
) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise unprocessable(too_large, loc=["body"])
        yield chunk


async def parse_capped_form(
    request: Request,
    *,
    max_bytes: int,
    max_files: int,
    max_fields: int,
    too_large: str = "request is too large",
) -> FormData:
    """The parsed form. 422 when the body is over ``max_bytes`` or not multipart.

    ``too_large`` is the message for an over-cap body, so a caller can name
    its own limit."""
    if max_bytes > _MAX_ANY_BYTES:
        raise ValueError("max_bytes is over the in-memory limit")
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > max_bytes:
        raise unprocessable(too_large, loc=["body"])
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise unprocessable("send multipart/form-data", loc=["body"])
    try:
        return await _InMemoryMultiPartParser(
            request.headers,
            _capped_body(request, max_bytes, too_large),
            max_files=max_files,
            max_fields=max_fields,
        ).parse()
    except MultiPartException as exc:
        raise unprocessable(exc.message, loc=["body"]) from exc


__all__ = ["parse_capped_form"]
