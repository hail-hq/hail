"""Internal HTTP transport for the Hail SDK.

Wraps ``httpx.AsyncClient`` so the public ``Client`` and its resource
classes don't need to know about retries, status-code mapping, or auth
header plumbing.

Retry policy (final, pin-down version)
--------------------------------------

* Idempotent requests — GET/HEAD/PUT/DELETE, plus any request that
  carries an ``Idempotency-Key`` header — are retried up to **3 extra
  times** on 5xx (4 total attempts).
* Backoff is exponential starting at 0.5s, doubling per attempt
  (0.5, 1.0, 2.0), with up to ``delay/2`` of random jitter on top.
* If the server sets ``Retry-After``, that value (parsed as an integer
  seconds count) overrides the computed backoff.
* Non-idempotent requests without an ``Idempotency-Key`` (e.g. a bare
  ``POST``) are NOT retried — failing fast is safer than risking a
  duplicate side effect.
* Connection-layer ``httpx`` failures are NOT retried; they bubble up
  to the caller, who can apply their own policy.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any
from uuid import uuid4

import httpx

from hail._errors import (
    HailAPIError,
    HailAuthError,
    HailClientError,
    HailIdempotencyConflict,
    HailNotFoundError,
    HailServerError,
    HailValidationError,
)

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Pinned retry knobs. Exposed at module level for test legibility.
MAX_RETRIES = 3
BASE_BACKOFF = 0.5  # seconds
BACKOFF_MULTIPLIER = 2.0


def _is_idempotent(method: str, headers: dict[str, str]) -> bool:
    """Return True iff this request is safe to retry on a transient 5xx.

    Idempotent verbs (GET/HEAD/PUT/DELETE) qualify unconditionally. Mutating
    verbs (POST/PATCH) only qualify if the caller provided an
    ``Idempotency-Key`` — the server collapses replays into one effect.
    """
    if method.upper() in {"GET", "HEAD", "PUT", "DELETE"}:
        return True
    return any(k.lower() == "idempotency-key" for k in headers)


def _compute_backoff(attempt: int, retry_after: str | None) -> float:
    """Backoff seconds for the *next* retry given the current attempt index.

    ``attempt`` starts at 0 (the first retry uses ``BASE_BACKOFF``). When the
    server set ``Retry-After`` (integer seconds, RFC 7231), that wins.
    """
    if retry_after is not None:
        try:
            value = float(retry_after)
            if value >= 0:
                return value
        except ValueError:
            # Some servers emit an HTTP-date — fall through to computed backoff.
            pass
    delay = BASE_BACKOFF * (BACKOFF_MULTIPLIER**attempt)
    jitter = random.uniform(0, delay / 2)
    return delay + jitter


def _raise_for_status(resp: httpx.Response) -> None:
    """Translate a non-2xx httpx Response into the right typed Hail error."""
    code = resp.status_code
    if 200 <= code < 300:
        return
    try:
        detail: Any = resp.json()
    except ValueError:
        detail = None
    text = resp.text
    # FastAPI commonly nests the human message under "detail".
    msg = ""
    if isinstance(detail, dict):
        d = detail.get("detail")
        if isinstance(d, str):
            msg = d
        elif isinstance(d, list):
            # validation errors are a list of dicts
            msg = "; ".join(
                str(e.get("msg", e)) if isinstance(e, dict) else str(e) for e in d
            )
    if not msg:
        msg = text or f"HTTP {code}"

    cls: type[HailAPIError]
    if code == 401:
        cls = HailAuthError
    elif code == 404:
        cls = HailNotFoundError
    elif code == 409:
        cls = HailIdempotencyConflict
    elif code == 422:
        cls = HailValidationError
    elif 500 <= code < 600:
        cls = HailServerError
    else:
        cls = HailClientError

    raise cls(
        f"Hail API error {code}: {msg}",
        status_code=code,
        detail=detail.get("detail") if isinstance(detail, dict) else detail,
        response_text=text,
    )


class _HailHTTP:
    """Async HTTP wrapper around ``httpx.AsyncClient``.

    The underlying client is lazy: nothing is opened until the first
    ``request`` call. ``aclose`` is idempotent — safe to call from
    ``__aexit__`` whether or not the client was ever used.

    Tests can pass a pre-built ``httpx.AsyncClient`` via ``transport_client``
    to plug in respx or a custom transport; the wrapper will not close it
    on ``aclose`` in that case (caller-owned).
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: httpx.Timeout | float | None = None,
        transport_client: httpx.AsyncClient | None = None,
        sleep: Any = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT
        self._client: httpx.AsyncClient | None = transport_client
        self._client_owned = transport_client is None
        # Injected for tests so retry assertions don't actually wait.
        self._sleep = sleep if sleep is not None else asyncio.sleep

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._client_owned:
            await self._client.aclose()
        self._client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send a request and return parsed JSON (or ``None`` for 204).

        Raises a typed :class:`HailAPIError` subclass on non-2xx responses.
        """
        client = self._ensure_client()
        merged_headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": "hail-sdk-python",
        }
        if headers:
            merged_headers.update(headers)

        # Drop None-valued query params so optional kwargs don't end up as
        # `?status=None` on the wire.
        clean_params: dict[str, Any] | None = None
        if params is not None:
            clean_params = {k: v for k, v in params.items() if v is not None}

        retryable = _is_idempotent(method, merged_headers)
        attempt = 0
        while True:
            resp = await client.request(
                method,
                path,
                json=json,
                params=clean_params,
                headers=merged_headers,
            )
            if resp.status_code < 500 or not retryable or attempt >= MAX_RETRIES:
                _raise_for_status(resp)
                if resp.status_code == 204 or not resp.content:
                    return None
                return resp.json()
            # Retryable 5xx — back off and try again.
            delay = _compute_backoff(attempt, resp.headers.get("Retry-After"))
            await self._sleep(delay)
            attempt += 1


def generate_idempotency_key() -> str:
    """Fresh UUIDv4 — used when the caller doesn't pass one explicitly."""
    return str(uuid4())


__all__ = [
    "_HailHTTP",
    "MAX_RETRIES",
    "BASE_BACKOFF",
    "BACKOFF_MULTIPLIER",
    "generate_idempotency_key",
]
