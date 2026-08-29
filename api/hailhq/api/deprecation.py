"""Marks legacy (unprefixed) customer-API responses as deprecated.

/v1/<resource> is canonical (see main.py's router dual-mount). The
unprefixed path keeps working — no existing integration breaks — but
every response on it carries a Deprecation: true header (the widely
deployed form; RFC 9745 is the current authority for this header) plus a
Link pointing at the /v1 successor, so a client (or an agent reading the
response) can tell the path is being phased out without guessing.

Matches on path shape, not a route allowlist: any request whose path does
NOT start with /v1/ and does NOT start with /internal/ is presumed to be
hitting a legacy-mounted customer route. /healthz and unmatched 404s also
pass through this middleware harmlessly (a Deprecation header on a 404 or
a healthcheck is inert, not incorrect) — this stays simple rather than
duplicating the router list here too.
"""

from __future__ import annotations

from hailhq.api.route_prefixes import INTERNAL_PREFIX as _INTERNAL_PREFIX
from hailhq.api.route_prefixes import V1_PREFIX as _V1_PREFIX
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class DeprecationHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        if not path.startswith(_V1_PREFIX) and not path.startswith(_INTERNAL_PREFIX):
            response.headers["Deprecation"] = "true"
            versioned = "/v1" + path
            response.headers["Link"] = f'<{versioned}>; rel="successor-version"'
        return response
