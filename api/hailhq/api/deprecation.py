"""Marks legacy (unprefixed) customer-API responses as deprecated.

/v1/<resource> is canonical (see main.py's router dual-mount). The
unprefixed path keeps working — no existing integration breaks — but
every response on it carries a Deprecation: true header (the widely
deployed form; RFC 9745 is the current authority for this header) plus a
Link pointing at the /v1 successor, so a client (or an agent reading the
response) can tell the path is being phased out without guessing. The one
exception is a 429 from the outer GeneralRateLimitMiddleware, which
short-circuits before this middleware runs.

Only responses for a matched customer API route are stamped: a request whose
path does NOT start with /v1/ or /internal/ AND that matched a FastAPI route
(``request.scope["route"]`` is set by the router during ``call_next``).
Unmatched paths (404s, bare /v1), the docs/spec endpoints (/openapi.json,
/docs, /redoc — plain Starlette routes) and /healthz have no /v1 twin, so
pointing them at a "successor" would advertise a URL that does not exist.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi.routing import APIRoute
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
        route = request.scope.get("route")
        if (
            not path.startswith(_V1_PREFIX)
            and not path.startswith(_INTERNAL_PREFIX)
            and isinstance(route, APIRoute)
            and route.path != "/healthz"
        ):
            response.headers["Deprecation"] = "true"
            # request.url.path is percent-decoded; header values are latin-1
            # encoded, so a non-latin-1 character would raise
            # UnicodeEncodeError (a 500 after the handler already ran).
            versioned = "/v1" + quote(path, safe="/")
            if request.url.query:
                versioned += "?" + request.url.query
            # append, not assign: never clobber a Link a route set itself.
            response.headers.append("Link", f'<{versioned}>; rel="successor-version"')
        return response
