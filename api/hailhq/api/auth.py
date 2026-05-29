"""Auth helpers — API-key hashing + Better Auth JWT verification.

The auth backend (in hail-website) is the sole producer of `hl_live_*` keys
and Better Auth OAuth JWTs. hail/api is a read-only consumer:

* API-key path: bearer is hashed with the same scheme as the backend and
  looked up in the shared ``api_keys`` table (see ``hash_key`` below).
* JWT path: bearer is verified against Better Auth's public JWKS using
  PyJWT; signature + issuer + audience + expiry are all checked here, and
  the JWT's ``sub`` claim (= user_id) feeds the same ``members`` join the
  API-key path uses to resolve ``organization_id``.

The JWKS cache is process-local and lazy: only created when
``settings.hail_auth_url`` is set (self-host leaves it empty, disabling
the JWT path entirely). The JWKS URL is derived as
``${hail_auth_url}/jwks`` — Better Auth's default mount.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from hailhq.core.config import settings

# --------------------------------------------------------------------------- #
# API-key hashing (unchanged).
# --------------------------------------------------------------------------- #


def hash_key(plain: str) -> str:
    """SHA-256 the bearer; base64url-encode without padding.

    Matches the auth backend's storage format so we can look the bearer up
    directly in the shared ``api_keys`` table.
    """
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# --------------------------------------------------------------------------- #
# JWKS cache.
# --------------------------------------------------------------------------- #

_DEFAULT_TIMEOUT_SECONDS = 5.0

# Don't hit the JWKS endpoint more than once per this window. Caps outbound
# load when bearers carry an unknown ``kid`` (a cheap amplification vector
# otherwise) and when the endpoint is down. Long enough to matter, short
# enough that a key rotation is picked up within a minute.
_DEFAULT_MIN_REFRESH_INTERVAL = timedelta(seconds=60)


class JWKSFetchError(Exception):
    """The JWKS endpoint was unreachable or returned an unusable body.

    Distinct from :class:`jwt.InvalidTokenError`: the *token* may be fine —
    we just couldn't fetch the keys to verify it. Callers turn this into a
    503 (transient, retry later), not a 401 (don't blame the client's token).
    """


class JWKSCache:
    """Process-local JWKS cache with throttled on-demand refresh.

    Keys are looked up by ``kid``. On a miss we refresh (handles key rotation
    transparently), but no more than once per ``min_refresh_interval`` so a
    flood of unknown-``kid`` bearers can't drive 1:1 load against the JWKS
    endpoint. The cache is intentionally minimal — Better Auth issues keys
    infrequently and a single API process can keep them in memory.
    """

    def __init__(
        self,
        jwks_url: str,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        min_refresh_interval: timedelta = _DEFAULT_MIN_REFRESH_INTERVAL,
    ) -> None:
        self._url = jwks_url
        self._client_factory = client_factory or self._default_client
        self._min_refresh_interval = min_refresh_interval
        self._keys: dict[str, PyJWK] = {}
        # Timestamp of the last fetch *attempt* (success or failure) — drives
        # the throttle. ``_last_error`` carries the most recent fetch failure
        # so requests inside the throttle window get a consistent signal.
        self._last_attempt_at: datetime | None = None
        self._last_error: JWKSFetchError | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _default_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS)

    async def get(self, kid: str) -> PyJWK:
        if kid not in self._keys:
            async with self._lock:
                # Double-check under the lock.
                if kid not in self._keys:
                    await self._maybe_refresh()
        if kid not in self._keys:
            raise jwt.InvalidTokenError(f"unknown kid {kid!r}")
        return self._keys[kid]

    async def _maybe_refresh(self) -> None:
        now = datetime.now(timezone.utc)
        if (
            self._last_attempt_at is not None
            and now - self._last_attempt_at < self._min_refresh_interval
        ):
            # Inside the throttle window: don't fetch again. If we have never
            # successfully loaded any keys, the prior failure is the real
            # reason this lookup can't resolve — surface it as such instead of
            # masquerading as an unknown-kid (token) error.
            if not self._keys and self._last_error is not None:
                raise self._last_error
            return
        self._last_attempt_at = now
        try:
            await self._refresh()
        except JWKSFetchError as exc:
            self._last_error = exc
            raise
        else:
            self._last_error = None

    async def _refresh(self) -> None:
        try:
            async with self._client_factory() as client:
                resp = await client.get(self._url)
                resp.raise_for_status()
                jwks = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # ValueError covers a non-JSON / malformed body (json.JSONDecodeError).
            raise JWKSFetchError(f"could not fetch JWKS: {exc}") from exc
        keys = jwks.get("keys") or []
        self._keys = {
            k["kid"]: PyJWK(k) for k in keys if isinstance(k, dict) and "kid" in k
        }


_jwks_cache: JWKSCache | None = None


def jwks_url_from_auth_url(auth_url: str) -> str:
    """Derive the JWKS endpoint from the auth backend's base URL.

    Better Auth mounts its JWKS at ``${baseURL}/jwks``; since the auth
    backend's ``baseURL`` is what we treat as the issuer (``hail_auth_url``),
    the JWKS URL is a trivial suffix. Splitting this out lets tests
    monkeypatch a single value and lets the MCP service share the same
    derivation later.
    """
    return f"{auth_url.rstrip('/')}/jwks"


def get_jwks_cache() -> JWKSCache | None:
    """Lazy module-level cache. Returns ``None`` if the JWT path is disabled."""
    global _jwks_cache
    if _jwks_cache is None and settings.hail_auth_url:
        _jwks_cache = JWKSCache(jwks_url_from_auth_url(settings.hail_auth_url))
    return _jwks_cache


def reset_jwks_cache_for_testing() -> None:
    """Drop the module-level cache so the next ``get_jwks_cache()`` rebuilds it.

    Tests that override env or the cache itself call this between cases.
    """
    global _jwks_cache
    _jwks_cache = None


# --------------------------------------------------------------------------- #
# JWT verification.
# --------------------------------------------------------------------------- #

# Tolerate minor clock skew between this process and the issuer when checking
# ``exp`` (and ``iat``/``nbf``). Small enough that an expired token isn't
# meaningfully usable past its lifetime.
_CLOCK_SKEW_LEEWAY = timedelta(seconds=30)


async def verify_jwt(
    token: str,
    *,
    jwks_cache: JWKSCache,
    issuer: str,
    audiences: list[str],
) -> dict[str, Any]:
    """Verify a Better Auth EdDSA JWT against the JWKS and return its claims.

    Raises :class:`jwt.InvalidTokenError` (or a subclass) for any rejection
    — bad signature, wrong issuer/audience, expired, unknown kid, missing
    required claim — which callers turn into a 401. Raises
    :class:`JWKSFetchError` if the JWKS endpoint is unreachable, which callers
    turn into a 503 (the token may be fine; we just couldn't fetch the keys).
    Algorithm is fixed to EdDSA / Ed25519 to match the Better Auth ``jwt``
    plugin default. A small ``exp`` leeway tolerates minor clock skew.
    """
    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:  # malformed header
        raise jwt.InvalidTokenError(f"invalid jwt header: {exc}") from exc
    kid = unverified_header.get("kid")
    if not kid:
        raise jwt.InvalidTokenError("jwt missing kid header")
    pyjwk = await jwks_cache.get(kid)
    claims = jwt.decode(
        token,
        key=pyjwk.key,
        algorithms=["EdDSA"],
        issuer=issuer,
        audience=audiences,
        leeway=_CLOCK_SKEW_LEEWAY,
        options={"require": ["iss", "aud", "exp", "sub"]},
    )
    return claims


__all__ = [
    "JWKSCache",
    "JWKSFetchError",
    "get_jwks_cache",
    "hash_key",
    "jwks_url_from_auth_url",
    "reset_jwks_cache_for_testing",
    "verify_jwt",
]
