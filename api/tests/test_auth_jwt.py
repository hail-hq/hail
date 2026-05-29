"""Tests for the JWKS cache + JWT verifier in api/hailhq/api/auth.py.

These cover the verifier in isolation; the integration with
``get_current_principal`` is exercised in a later task.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import httpx
import jwt as _jwt_lib
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api import auth
from hailhq.api import deps
from hailhq.api.deps import Principal, get_current_principal
from hailhq.core.db import get_session
from hailhq.core.models import OrganizationMember

_AUTH_URL = "https://issuer.example.com"
_JWKS_URL = (
    "https://issuer.example.com/jwks"  # = auth.jwks_url_from_auth_url(_AUTH_URL)
)


async def test_jwks_cache_fetches_and_returns_pyjwk(jwks_client_factory):
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    pyjwk = await cache.get("test-kid-1")
    assert pyjwk.key_id == "test-kid-1"


async def test_jwks_cache_unknown_kid_after_refresh_raises(jwks_client_factory):
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    with pytest.raises(_jwt_lib.InvalidTokenError):
        await cache.get("not-the-kid")


async def test_jwks_cache_refresh_on_kid_miss(jwks_dict, signing_keypair):
    """A kid that wasn't in cache should trigger a refresh; if it appears
    in the new JWKS, the second lookup succeeds."""
    import httpx as _httpx

    _, public_jwk = signing_keypair
    extra_kid_jwk = dict(public_jwk)
    extra_kid_jwk["kid"] = "rotated-kid"

    call_count = {"n": 0}

    def _handler(_req: _httpx.Request) -> _httpx.Response:
        call_count["n"] += 1
        # First call: only the original kid. Second call: only the rotated kid.
        if call_count["n"] == 1:
            return _httpx.Response(200, json={"keys": [public_jwk]})
        return _httpx.Response(200, json={"keys": [extra_kid_jwk]})

    def _factory() -> _httpx.AsyncClient:
        return _httpx.AsyncClient(transport=_httpx.MockTransport(_handler))

    # min_refresh_interval=0 disables the throttle so the kid-miss refresh
    # fires immediately — this test is about rotation mechanics, not timing.
    cache = auth.JWKSCache(
        _JWKS_URL, client_factory=_factory, min_refresh_interval=timedelta(0)
    )
    # Prime the cache so call #1 happens.
    pyjwk = await cache.get("test-kid-1")
    assert pyjwk.key_id == "test-kid-1"
    # Now ask for a kid we haven't seen — should trigger a refresh.
    rotated = await cache.get("rotated-kid")
    assert rotated.key_id == "rotated-kid"
    assert call_count["n"] == 2


async def test_jwks_cache_throttles_unknown_kid_lookups(jwks_dict):
    """Repeated unknown-kid lookups inside the throttle window hit the JWKS
    endpoint only once — guards against an amplification vector."""
    import httpx as _httpx

    call_count = {"n": 0}

    def _handler(_req: _httpx.Request) -> _httpx.Response:
        call_count["n"] += 1
        return _httpx.Response(200, json=jwks_dict)

    def _factory() -> _httpx.AsyncClient:
        return _httpx.AsyncClient(transport=_httpx.MockTransport(_handler))

    # Default (non-zero) interval so the throttle is active.
    cache = auth.JWKSCache(_JWKS_URL, client_factory=_factory)
    for _ in range(5):
        with pytest.raises(_jwt_lib.InvalidTokenError):
            await cache.get("never-present-kid")
    assert call_count["n"] == 1


async def test_jwks_cache_fetch_failure_raises_jwks_fetch_error():
    """An unreachable / erroring JWKS endpoint surfaces JWKSFetchError, not a
    raw httpx error (which would become a 500 upstream)."""
    import httpx as _httpx

    def _handler(_req: _httpx.Request) -> _httpx.Response:
        return _httpx.Response(503)

    def _factory() -> _httpx.AsyncClient:
        return _httpx.AsyncClient(transport=_httpx.MockTransport(_handler))

    cache = auth.JWKSCache(_JWKS_URL, client_factory=_factory)
    with pytest.raises(auth.JWKSFetchError):
        await cache.get("test-kid-1")


async def test_jwks_cache_fetch_failure_is_consistent_within_window():
    """While the cache has never loaded keys, lookups inside the throttle
    window keep raising JWKSFetchError (not a misleading unknown-kid error)
    and don't re-hit the endpoint."""
    import httpx as _httpx

    call_count = {"n": 0}

    def _handler(_req: _httpx.Request) -> _httpx.Response:
        call_count["n"] += 1
        return _httpx.Response(500)

    def _factory() -> _httpx.AsyncClient:
        return _httpx.AsyncClient(transport=_httpx.MockTransport(_handler))

    cache = auth.JWKSCache(_JWKS_URL, client_factory=_factory)
    for _ in range(3):
        with pytest.raises(auth.JWKSFetchError):
            await cache.get("test-kid-1")
    assert call_count["n"] == 1


async def test_get_jwks_cache_builds_from_settings(monkeypatch):
    """The production lazy-singleton path: get_jwks_cache() builds a cache
    pointed at the configured URL, and returns None when unconfigured."""
    auth.reset_jwks_cache_for_testing()
    monkeypatch.setattr(auth.settings, "hail_auth_url", "")
    assert auth.get_jwks_cache() is None

    auth.reset_jwks_cache_for_testing()
    monkeypatch.setattr(auth.settings, "hail_auth_url", _AUTH_URL)
    cache = auth.get_jwks_cache()
    assert cache is not None
    assert cache._url == _JWKS_URL
    # Cached: a second call returns the same instance.
    assert auth.get_jwks_cache() is cache


async def test_verify_jwt_happy_path(jwks_client_factory, base_claims, sign_jwt):
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    token = sign_jwt(base_claims())
    claims = await auth.verify_jwt(
        token,
        jwks_cache=cache,
        issuer="https://issuer.example.com",
        audiences=["https://api.example.com"],
    )
    assert claims["iss"] == "https://issuer.example.com"


async def test_verify_jwt_rejects_bad_signature(
    jwks_client_factory, base_claims, sign_jwt
):
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    token = sign_jwt(base_claims())
    # Tamper: flip a byte in the signature segment.
    head, body, sig = token.split(".")
    bad = head + "." + body + "." + ("A" + sig[1:] if sig[0] != "A" else "B" + sig[1:])
    with pytest.raises(_jwt_lib.InvalidTokenError):
        await auth.verify_jwt(
            bad,
            jwks_cache=cache,
            issuer="https://issuer.example.com",
            audiences=["https://api.example.com"],
        )


async def test_verify_jwt_rejects_wrong_issuer(
    jwks_client_factory, base_claims, sign_jwt
):
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    token = sign_jwt(base_claims(iss="https://other-issuer.example.com"))
    with pytest.raises(_jwt_lib.InvalidTokenError):
        await auth.verify_jwt(
            token,
            jwks_cache=cache,
            issuer="https://issuer.example.com",
            audiences=["https://api.example.com"],
        )


async def test_verify_jwt_rejects_wrong_audience(
    jwks_client_factory, base_claims, sign_jwt
):
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    token = sign_jwt(base_claims(aud="https://elsewhere.example.com"))
    with pytest.raises(_jwt_lib.InvalidTokenError):
        await auth.verify_jwt(
            token,
            jwks_cache=cache,
            issuer="https://issuer.example.com",
            audiences=["https://api.example.com"],
        )


async def test_verify_jwt_rejects_expired(jwks_client_factory, base_claims, sign_jwt):
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    # Comfortably past the clock-skew leeway (30s) so this stays a clean reject.
    token = sign_jwt(base_claims(exp_offset_seconds=-300))
    with pytest.raises(_jwt_lib.ExpiredSignatureError):
        await auth.verify_jwt(
            token,
            jwks_cache=cache,
            issuer="https://issuer.example.com",
            audiences=["https://api.example.com"],
        )


async def test_verify_jwt_accepts_either_audience_in_allow_list(
    jwks_client_factory, base_claims, sign_jwt
):
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    token = sign_jwt(base_claims(aud="https://mcp.example.com"))
    claims = await auth.verify_jwt(
        token,
        jwks_cache=cache,
        issuer="https://issuer.example.com",
        audiences=["https://api.example.com", "https://mcp.example.com"],
    )
    assert (
        claims["aud"] == "https://mcp.example.com"
        or "https://mcp.example.com" in claims["aud"]
    )


async def test_verify_jwt_rejects_missing_kid_header(
    jwks_client_factory, base_claims, signing_keypair
):
    """A token whose header lacks ``kid`` is rejected before signature check."""
    private_pem, _ = signing_keypair
    token = _jwt_lib.encode(
        base_claims(),
        private_pem,
        algorithm="EdDSA",
        # no headers kwarg => no kid header
    )
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    with pytest.raises(_jwt_lib.InvalidTokenError):
        await auth.verify_jwt(
            token,
            jwks_cache=cache,
            issuer="https://issuer.example.com",
            audiences=["https://api.example.com"],
        )


async def test_verify_jwt_rejects_token_missing_required_claim(
    jwks_client_factory, base_claims, sign_jwt
):
    """Dropping a required claim (sub) must be rejected even with a valid
    signature; guards against a future refactor weakening the require list."""
    claims = base_claims()
    claims.pop("sub")
    token = sign_jwt(claims)
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    with pytest.raises(_jwt_lib.InvalidTokenError):
        await auth.verify_jwt(
            token,
            jwks_cache=cache,
            issuer="https://issuer.example.com",
            audiences=["https://api.example.com"],
        )


# --------------------------------------------------------------------------- #
# Integration tests: JWT dispatch in get_current_principal.
# --------------------------------------------------------------------------- #


def _whoami_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(
        principal: Principal = Depends(get_current_principal),
    ) -> dict:
        return {
            "api_key_id": str(principal.api_key_id) if principal.api_key_id else None,
            "organization_id": str(principal.organization_id),
            "scopes": principal.scopes,
        }

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_get_session
    return app


def _install_test_jwks(monkeypatch, jwks_client_factory) -> None:
    """Replace the module-level cache with one wired to the test JWKS."""
    from hailhq.api import auth as _auth

    test_cache = _auth.JWKSCache(
        "https://issuer.example.com/jwks", client_factory=jwks_client_factory
    )
    monkeypatch.setattr(_auth, "_jwks_cache", test_cache)


def _configure_env(monkeypatch) -> None:
    monkeypatch.setattr(deps.settings, "hail_auth_url", "https://issuer.example.com")
    monkeypatch.setattr(
        deps.settings,
        "hail_auth_audiences",
        "https://api.example.com,https://mcp.example.com",
    )


@pytest.fixture()
async def jwt_member_pair(
    async_session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert an OrganizationMember row and return (user_id, organization_id).

    Reuses the same insert pattern as ``insert_org_and_key`` in conftest.py
    (lines 73-86) — only the OrganizationMember row is needed for JWT auth
    (no ApiKey row exists for JWT users).
    """
    user_uuid = uuid.uuid4()
    organization_id = uuid.uuid4()

    now = datetime.now(timezone.utc)
    async_session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=user_uuid,
            organization_id=organization_id,
            role="owner",
            created_at=now,
        )
    )
    await async_session.commit()
    return user_uuid, organization_id


async def test_jwt_path_happy_resolves_to_org(
    monkeypatch,
    jwks_client_factory,
    base_claims,
    sign_jwt,
    jwt_member_pair,
    async_session: AsyncSession,
):
    _configure_env(monkeypatch)
    _install_test_jwks(monkeypatch, jwks_client_factory)

    user_id, organization_id = jwt_member_pair
    token = sign_jwt(base_claims(sub=str(user_id), aud="https://api.example.com"))

    transport = httpx.ASGITransport(app=_whoami_app(async_session))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["organization_id"] == str(organization_id)
    assert body["api_key_id"] is None
    assert body["scopes"] == ["*"]


async def test_jwt_path_unknown_sub_returns_403(
    monkeypatch,
    jwks_client_factory,
    base_claims,
    sign_jwt,
    async_session: AsyncSession,
):
    _configure_env(monkeypatch)
    _install_test_jwks(monkeypatch, jwks_client_factory)
    token = sign_jwt(base_claims(sub=str(uuid.uuid4()), aud="https://api.example.com"))

    transport = httpx.ASGITransport(app=_whoami_app(async_session))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert "user not provisioned" in resp.json()["detail"]


async def test_jwt_path_bad_signature_returns_401(
    monkeypatch,
    jwks_client_factory,
    base_claims,
    sign_jwt,
    async_session: AsyncSession,
):
    _configure_env(monkeypatch)
    _install_test_jwks(monkeypatch, jwks_client_factory)
    token = sign_jwt(base_claims(aud="https://api.example.com"))
    head, body, sig = token.split(".")
    bad = head + "." + body + "." + ("A" + sig[1:] if sig[0] != "A" else "B" + sig[1:])

    transport = httpx.ASGITransport(app=_whoami_app(async_session))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/whoami", headers={"Authorization": f"Bearer {bad}"})
    assert resp.status_code == 401


async def test_non_jwt_token_falls_through_to_api_key_path(
    monkeypatch,
    async_session: AsyncSession,
):
    _configure_env(monkeypatch)
    # Non-JWT-shaped token (no dots). The bearer hashes to a value that
    # matches no row in api_keys, so the API-key path 401s — the test
    # asserts we get there (i.e. the JWT path did not try to verify a
    # non-JWT token and 500).
    transport = httpx.ASGITransport(app=_whoami_app(async_session))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get(
            "/whoami", headers={"Authorization": "Bearer not-a-jwt"}
        )
    assert resp.status_code == 401


async def test_jwt_token_without_jwt_config_returns_401(
    monkeypatch,
    jwks_client_factory,
    base_claims,
    sign_jwt,
    async_session: AsyncSession,
):
    """JWT path is disabled when env vars are empty (self-host posture)."""
    monkeypatch.setattr(deps.settings, "hail_auth_url", "")
    monkeypatch.setattr(deps.settings, "hail_auth_audiences", "")
    token = sign_jwt(base_claims())

    transport = httpx.ASGITransport(app=_whoami_app(async_session))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_jwt_path_jwks_unreachable_returns_503(
    monkeypatch,
    base_claims,
    sign_jwt,
    async_session: AsyncSession,
):
    """A JWKS outage is transient and not the client's fault — 503, not 401."""
    _configure_env(monkeypatch)

    def _failing_factory() -> httpx.AsyncClient:
        def _handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        return httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    from hailhq.api import auth as _auth

    test_cache = _auth.JWKSCache(
        "https://issuer.example.com/jwks", client_factory=_failing_factory
    )
    monkeypatch.setattr(_auth, "_jwks_cache", test_cache)
    token = sign_jwt(base_claims(aud="https://api.example.com"))

    transport = httpx.ASGITransport(app=_whoami_app(async_session))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 503
