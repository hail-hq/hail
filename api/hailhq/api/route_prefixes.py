"""Shared path-prefix constants for the /v1-vs-legacy dual mount.

Customer routers are dual-mounted (main.py) at ``/v1/<resource>`` (canonical)
and again at the unprefixed legacy path. Several independent modules
(``deprecation.py``, ``ratelimit.py``, ``idempotency.py``, and the route
handlers that build a ``Location`` header) all need to agree on the same
prefix strings and the same "which mount did this request come in on" logic
— this module is the single source of truth so they don't drift apart.
"""

from __future__ import annotations

from starlette.requests import Request

V1_PREFIX = "/v1/"
INTERNAL_PREFIX = "/internal/"


def request_mount_prefix(request: Request) -> str:
    """Returns the /v1 mount prefix if this request used it, else "" (legacy).

    Use to build a self-referential URL (e.g. a ``Location`` header) that
    reflects whichever mount the request actually arrived on, rather than
    hardcoding one.
    """
    return "/v1" if request.url.path.startswith(V1_PREFIX) else ""


__all__ = ["INTERNAL_PREFIX", "V1_PREFIX", "request_mount_prefix"]
