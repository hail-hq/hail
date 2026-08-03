"""MCP auth — boot-time mode selector + pass-through token verifier.

The MCP service has two operator postures, chosen at startup from env:

* **oauth-rs** — ``HAIL_AUTH_URL`` set. FastMCP gets ``AuthSettings`` and
  a pass-through ``TokenVerifier``; protected-resource metadata is auto-
  mounted by FastMCP. Tools forward each request's JWT to the API.
* **static-key** — ``HAIL_API_KEY`` set. FastMCP runs unauthenticated;
  tools use the shared singleton ``HailClient(api_key=HAIL_API_KEY)``.

The two modes are mutually exclusive — both set is a configuration error
we catch at boot rather than at the first request. The pass-through
verifier exists because ``hail/api`` is the single source of JWT-validation
truth (signature + issuer + audience); MCP's job is to surface the 401 +
``WWW-Authenticate`` discovery hint and to thread the bearer onto the
outbound call.
"""

from __future__ import annotations

import enum

from hailhq.core.config import settings

from mcp.server.auth.provider import AccessToken, TokenVerifier


class AuthMode(enum.Enum):
    OAUTH_RS = "oauth-rs"
    STATIC_KEY = "static-key"


def select_auth_mode() -> AuthMode:
    """Pick the MCP auth mode from env. Raises on ambiguous/missing config.

    Called once at startup by ``server._build_app()``. Tests monkey-patch
    ``settings.hail_auth_url`` and ``settings.hail_api_key`` to exercise
    each branch.
    """
    has_oauth = bool(settings.hail_auth_url)
    has_static = bool(settings.hail_api_key)
    if has_oauth and has_static:
        raise RuntimeError(
            "ambiguous MCP auth config — set HAIL_AUTH_URL XOR HAIL_API_KEY"
        )
    if has_oauth:
        return AuthMode.OAUTH_RS
    if has_static:
        return AuthMode.STATIC_KEY
    raise RuntimeError("MCP auth not configured — set HAIL_AUTH_URL or HAIL_API_KEY")


class PassThroughVerifier(TokenVerifier):
    """Accept the bearer without validating its signature.

    The API verifies the JWT (signature, issuer, audience, expiry, JWKS) —
    duplicating that here would create a key-rotation race and a second
    source of truth. The verifier's job is to populate request state so
    FastMCP's auth middleware recognises the call as authenticated and so
    tools can read the bearer off ``ctx.request_context``.

    ``resource_server_url`` is the MCP's audience identity (e.g.
    ``https://mcp.hail.so``). FastMCP uses it both for the
    ``WWW-Authenticate: Bearer resource_metadata=...`` header on 401s and
    as the ``resource`` field on the ``AccessToken`` it hands tools.
    """

    def __init__(self, *, resource_server_url: str) -> None:
        self._resource_server_url = resource_server_url

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        return AccessToken(
            token=token,
            client_id="<opaque>",  # We don't decode the JWT — client_id is unknown.
            scopes=[],
            resource=self._resource_server_url,
        )


__all__ = ["AuthMode", "PassThroughVerifier", "select_auth_mode"]
