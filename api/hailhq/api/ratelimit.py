"""General per-caller request-rate limiting.

Distinct from core.agent_caps (a DB-backed, per-org, per-channel *send*
velocity cap for agent-origin orgs only). This is a general HTTP
request-rate limiter across every customer-facing route, for every
caller. In-memory storage — safe because this API runs single-instance
(one VM, docker-compose, no replica orchestration; see deploy.yml). If a
second instance is ever added, swap the storage to Redis (add
``limits[redis]``). The storage calls in ``dispatch`` are synchronous and
would block the event loop on network I/O, so move them to an async storage
(or a thread) first.

Keyed on the bearer token (hashed) rather than the resolved Principal, so
the limiter runs before any DB round-trip — distinct callers with distinct
keys/tokens get distinct buckets even before auth resolves whether the token
is valid. The token is parsed the same way ``deps._parse_bearer`` does
(case-insensitive scheme, token stripped), so cosmetic variants of one
credential ("bearer K", "Bearer  K") share one bucket. A request with no
usable bearer falls back to the remote address. One bucket per caller
across *all* customer routes combined (not one bucket per route) — this
is a blanket abuse guard on total request volume, not a per-endpoint
budget.

Wired as ``GeneralRateLimitMiddleware`` in main.py rather than slowapi's
own ``SlowAPIMiddleware`` + ``default_limits`` pattern: that pattern
matches every app route by handler identity and needs each exempt route
individually registered via ``limiter.exempt(...)``, which would mean
editing every internal/* route module. Path-based exemption
(``/internal/*``, exact ``/healthz``, the docs/spec paths and
``_EXEMPT_PATHS`` below) is equivalent here and self-contained — it matches
on path shape like ``deprecation.py``'s ``DeprecationHeaderMiddleware``,
though that middleware exempts a different set (only ``/v1/`` and
``/internal/``).

Emits the IETF ``draft-ietf-httpapi-ratelimit-headers`` header names
directly (``RateLimit-Limit`` / ``RateLimit-Remaining`` / ``RateLimit-
Reset``) rather than slowapi's own default ``X-RateLimit-*`` mapping —
this middleware sets headers itself (see dispatch()) instead of relying
on ``Limiter._inject_headers``, so there is no library default to
diverge from.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from hailhq.api.route_prefixes import INTERNAL_PREFIX as _INTERNAL_PREFIX
from hailhq.api.route_prefixes import V1_PREFIX as _V1_PREFIX
from hailhq.core.config import settings
from limits import RateLimitItem, parse
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

__all__ = [
    "GENERAL_RATE_LIMITED_RESPONSES",
    "GeneralRateLimitMiddleware",
    "limiter",
    "merge_rate_limited_responses",
    "rate_limit_string",
]


def _rate_limit_key(request: Request) -> str:
    # Mirrors deps._parse_bearer without raising (importing deps would pull
    # the DB layer into this module): the scheme is matched case-insensitively
    # and the token is stripped, so every spelling that authenticates as one
    # credential lands in one bucket.
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, rest = auth.partition(" ")
        token = rest.strip()
        if scheme.lower() == "bearer" and token:
            return hashlib.sha256(token.encode()).hexdigest()
    return get_remote_address(request)


# Used only for its in-memory storage (``limiter.limiter.storage``).
# ``key_func`` is a required Limiter() argument but slowapi never calls it
# here: dispatch() calls _rate_limit_key and storage.incr() directly. See the
# module docstring for why wiring is a custom middleware rather than
# slowapi's own SlowAPIMiddleware.
limiter = Limiter(key_func=_rate_limit_key)


def rate_limit_string() -> str:
    return f"{settings.api_rate_limit_per_minute}/minute"


# Paths the general limiter never applies to: internal service-to-service
# routes (HMAC-authenticated, not customer traffic — see routes/internal/),
# the health check, the docs/spec paths, and the self-credentialed public
# routes below. Matches on path shape, same convention as deprecation.py's
# DeprecationHeaderMiddleware.
_HEALTHZ_PATH = "/healthz"

# Unauthenticated, static docs/spec endpoints (never dual-mounted under /v1).
# Anonymous callers share one remote-IP bucket behind the proxy, so without
# this exemption an anonymous flood would 429 the spec agents fetch.
_DOCS_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

# Self-credentialed public routes with no Authorization header by design
# (Twilio signature auth for the SMS webhooks; an HMAC token query param for
# unsubscribe, RFC 8058). Without this exemption they fall back to the
# remote-IP rate-limit key (_rate_limit_key below) — and in production every
# anonymous caller resolves to the same upstream-proxy IP, so all anonymous
# traffic would share one bucket with these legitimate routes. Listed
# unprefixed; dual-mounted at both /v1/... and the legacy path (main.py), so
# matching strips a leading /v1 before comparing.
_EXEMPT_PATHS = frozenset(
    {"/sms/inbound", "/sms/status", "/sms/telnyx", "/unsubscribe"}
)


def _is_exempt(path: str) -> bool:
    if (
        path == _HEALTHZ_PATH
        or path in _DOCS_PATHS
        or path.startswith(_INTERNAL_PREFIX)
    ):
        return True
    unprefixed = path[len(_V1_PREFIX) - 1 :] if path.startswith(_V1_PREFIX) else path
    return unprefixed in _EXEMPT_PATHS


class GeneralRateLimitMiddleware(BaseHTTPMiddleware):
    """Enforces ``rate_limit_string()`` per caller on every non-exempt route.

    Reads ``settings.api_rate_limit_per_minute`` fresh on every request
    (via ``rate_limit_string()``), not once at import/startup time, so a
    runtime change to the setting takes effect immediately — including in
    tests that monkeypatch ``settings.api_rate_limit_per_minute``.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if _is_exempt(request.url.path):
            return await call_next(request)

        key = _rate_limit_key(request)
        item: RateLimitItem = parse(rate_limit_string())
        # Single atomic incr() instead of hit()+get_window_stats(): the
        # latter is two separate storage calls, so a concurrent request's
        # hit() could land between them and the returned Remaining/Reset
        # headers would reflect that other caller's increment, not this
        # request's own. incr() returns the post-increment count directly.
        storage_key = item.key_for(key)
        count = limiter.limiter.storage.incr(storage_key, item.get_expiry())
        allowed = count <= item.amount
        remaining = max(0, item.amount - count)
        reset_in = max(
            int(limiter.limiter.storage.get_expiry(storage_key) - time.time()), 0
        )

        if not allowed:
            return JSONResponse(
                {
                    "detail": (
                        "Rate limit exceeded. Retry after the Retry-After "
                        "header (seconds)."
                    )
                },
                status_code=429,
                headers={
                    "Retry-After": str(max(reset_in, 1)),
                    "RateLimit-Limit": str(item.amount),
                    "RateLimit-Remaining": str(remaining),
                    "RateLimit-Reset": str(reset_in),
                },
            )

        response = await call_next(request)
        response.headers["RateLimit-Limit"] = str(item.amount)
        response.headers["RateLimit-Remaining"] = str(remaining)
        response.headers["RateLimit-Reset"] = str(reset_in)
        return response


# OpenAPI doc for the 429 this middleware can return. FastAPI does not infer
# statuses from a middleware short-circuit any more than it does from
# `raise HTTPException`, so every customer route decorator must declare this
# (`responses=GENERAL_RATE_LIMITED_RESPONSES`) for the generated spec — and
# the CLI codegen from it — to reflect the rate limit. Regenerate
# openapi/openapi.yaml after touching this (see docs/public/contributing.md).
GENERAL_RATE_LIMITED_RESPONSES: dict[int | str, dict[str, Any]] = {
    429: {
        "description": (
            "Rate limited. This caller exceeded the general request-rate "
            "ceiling. Retry after the Retry-After header (seconds)."
        ),
        "headers": {
            "Retry-After": {
                "description": "Seconds to wait before retrying.",
                "schema": {"type": "integer"},
            },
            "RateLimit-Limit": {
                "description": "The request ceiling for the current window.",
                "schema": {"type": "integer"},
            },
            "RateLimit-Remaining": {
                "description": "Requests remaining in the current window.",
                "schema": {"type": "integer"},
            },
            "RateLimit-Reset": {
                "description": "Seconds until the current window resets.",
                "schema": {"type": "integer"},
            },
        },
    }
}


def merge_rate_limited_responses(
    *response_dicts: dict[int | str, dict[str, Any]],
) -> dict[int | str, dict[str, Any]]:
    """Merge several ``responses=`` 429 docs into one, without either
    clobbering the other's description/headers.

    Used on the calls/sms/emails create routes, which already document a
    distinct 429 cause (the agent-abuse velocity cap, ``agent_gate.py``'s
    ``RATE_LIMITED_RESPONSES``) — both 429 reasons are real and independently
    reachable on those routes, so both need documenting on the same status
    code rather than one silently replacing the other.
    """
    descriptions: list[str] = []
    headers: dict[str, Any] = {}
    for d in response_dicts:
        entry = d[429]
        descriptions.append(entry["description"])
        headers.update(entry.get("headers", {}))
    return {429: {"description": " ".join(descriptions), "headers": headers}}
