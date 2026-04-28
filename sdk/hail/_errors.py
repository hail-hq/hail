"""Typed exception hierarchy for the Hail SDK.

All HTTP-status-coded errors derive from :class:`HailError` so callers can
``except hail.HailError`` once and still distinguish auth vs. validation vs.
server-side faults via ``isinstance``.

Local validation errors (malformed resource ids, missing API key) are also
``HailError`` subclasses so a single except clause covers everything the SDK
raises directly. Network-layer failures from ``httpx`` (connection refused,
DNS, timeouts) are deliberately NOT wrapped — they bubble up as
``httpx.HTTPError`` so callers can apply their own retry/circuit-breaker
policy on top.
"""

from __future__ import annotations

from typing import Any


class HailError(Exception):
    """Base class for every Hail-SDK-raised exception."""


class HailAPIError(HailError):
    """Base for any error backed by an HTTP response from the Hail API.

    Attributes:
        status_code: HTTP status code from the server.
        detail: Parsed JSON body (commonly ``{"detail": "..."}``) or raw text.
        response_text: The raw text body, useful for debugging unexpected shapes.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        detail: Any = None,
        response_text: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.response_text = response_text


class HailAuthError(HailAPIError):
    """401 — the API key was missing, malformed, or revoked."""


class HailNotFoundError(HailAPIError):
    """404 — the requested resource doesn't exist (or isn't in your org)."""


class HailValidationError(HailAPIError):
    """422 — the request body or query failed server-side validation.

    The server returns a list of error objects; ``self.detail`` holds them
    verbatim (FastAPI's standard ``HTTPValidationError`` shape).
    """


class HailIdempotencyConflict(HailAPIError):
    """409 — same Idempotency-Key was reused with a different request body."""


class HailServerError(HailAPIError):
    """5xx — server reported a fault. Retried automatically up to the SDK's limit."""


class HailClientError(HailAPIError):
    """Any other 4xx not modeled above (400, 403, 429, ...)."""


class HailMalformedResourceId(HailError):
    """Local validation: a resource id wasn't ``<type>:<uuid>`` shaped."""


class HailConfigError(HailError):
    """Local config error — e.g. no API key supplied or discoverable."""


__all__ = [
    "HailError",
    "HailAPIError",
    "HailAuthError",
    "HailNotFoundError",
    "HailValidationError",
    "HailIdempotencyConflict",
    "HailServerError",
    "HailClientError",
    "HailMalformedResourceId",
    "HailConfigError",
]
