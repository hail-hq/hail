# API JWT Validation Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Better Auth JWT validation path to `api/hailhq/api/deps.py` alongside the existing shared-key and API-key paths, so the API can resolve an `organization_id` from an OAuth-issued JWT (used by MCP web Connectors later in Phase 1).

**Architecture:** A small `JWKSCache` + a stateless `verify_jwt()` live in `api/hailhq/api/auth.py` (the natural home — `hash_key` is already there). `api/hailhq/api/deps.py` gets a `_principal_from_jwt()` and a token-shape dispatcher inside `get_current_principal`: a 3-segment dot-separated token routes to the JWT path; everything else routes to the existing API-key path. JWT path is opt-in: if `BETTER_AUTH_ISSUER` / `BETTER_AUTH_JWKS_URL` / `ALLOWED_AUDIENCES` are empty (self-host default), the path is disabled — both existing paths are byte-for-byte unchanged.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy async, Pydantic v2 (`pydantic-settings`), **PyJWT 2.x with `[crypto]` extra** (brings `cryptography` for RS256), httpx (already a dep) for JWKS fetch, `pytest-asyncio` (`asyncio_mode = auto`).

**Spec:** [`2026-05-28-mcp-multi-tenant-auth-design.md`](../specs/2026-05-28-mcp-multi-tenant-auth-design.md) — see the §"1a — API gains a JWT path" section.

---

## Background the implementer needs

- `api/hailhq/api/deps.py:195-211` already implements `get_current_principal` with two paths: shared-key (`_check_shared_key` HMAC compare vs `settings.hail_api_key`) and per-user API-key (`_principal_from_apikey_table`, hashes the bearer with `hash_key`, joins `api_keys` ⨝ `members` to resolve `organization_id`). The `Principal` model has `api_key_id: UUID | None`, `organization_id: UUID`, `scopes: list[str]`.
- `api/hailhq/api/auth.py` currently holds only `hash_key`. The new JWKS cache + verifier belong here.
- Settings are read from env via `pydantic-settings`. Fields in `core/hailhq/core/config.py` map to UPPER_SNAKE env vars (e.g. `hail_api_key` ← `HAIL_API_KEY`). `.env.example` documents them.
- Tests live in `api/tests/`. `test_auth.py` covers the API-key path; `test_auth_shared.py` covers shared-key. A new `test_auth_jwt.py` mirrors that style. The conftest at `api/tests/conftest.py:218-220` already calls `deps.reset_caches()` between tests — extend it to also reset the JWKS cache.
- `asyncio_mode = "auto"` in `api/pyproject.toml`, so `async def test_*` functions run automatically.
- Run the suite from `api/`: `cd api && uv run pytest -q`. A husky pre-commit hook runs ruff/black on staged `*.py`.

**Better Auth JWT shape** (verified against the Better Auth OAuth Provider docs):

- `iss` — the Better Auth base URL (matches `BETTER_AUTH_ISSUER` env exactly).
- `aud` — one of the resources for which the token was issued (string or list of strings). Better Auth's MCP integration sets `aud` to the MCP server URL (`https://mcp.hail.so`); the API also accepts `aud` for itself (`https://api.hail.so`). Configurable via `ALLOWED_AUDIENCES` CSV.
- `sub` — the Better Auth user_id as a UUID string. **Same value as `api_keys.reference_id`** (the existing API-key path casts that to UUID for the `members` join — we reuse the same join).
- `exp` — Unix seconds.
- `kid` (header) — key id, used to look up the right key in the JWKS.
- Optional `scope` (space-separated string) or `scopes` (list) for OAuth scopes. Default to `["*"]` when absent.
- Algorithm: **RS256** (Better Auth's `jwt` plugin default). PyJWT's `[crypto]` extra supplies the verifier.

## File Structure

- **Modify** `core/hailhq/core/config.py` — add three Settings fields (`better_auth_issuer`, `better_auth_jwks_url`, `allowed_audiences`).
- **Modify** `.env.example` — add a new `# ─── Auth backend (cloud) ───` section documenting the three vars.
- **Modify** `api/pyproject.toml` — add `pyjwt[crypto]>=2.9` to `[project].dependencies`.
- **Modify** `api/hailhq/api/auth.py` — add `JWKSCache` class, `verify_jwt()` function, module-level lazy `_jwks_cache` singleton, a `reset_jwks_cache_for_testing()` hook. Existing `hash_key` is left alone.
- **Modify** `api/hailhq/api/deps.py` — add `_looks_like_jwt`, `_jwt_configured`, `_principal_from_jwt`, and extend `get_current_principal` to dispatch by token shape. Existing shared-key / API-key paths unchanged.
- **Create** `api/tests/test_auth_jwt.py` — JWT-path tests (happy + each rejection mode). Reuses an RSA keypair fixture.
- **Modify** `api/tests/conftest.py` — extend the existing `reset_caches` fixture to also reset the JWKS cache.

---

### Task 1: Config fields, .env.example entry, PyJWT dependency

**Files:**

- Modify: `core/hailhq/core/config.py`
- Modify: `.env.example`
- Modify: `api/pyproject.toml`

No behavioral change yet — this lands the wiring so subsequent tasks can consume the config. No tests in this task; behavior arrives in Task 2/3.

- [ ] **Step 1: Add the three Settings fields** to `core/hailhq/core/config.py`, immediately after the `hail_internal_secret` block (so they sit with the other auth-related fields). Add this block verbatim (preserve the existing line above and below):

```python
    # Better Auth (managed cloud) — OAuth/JWT verification alongside the
    # existing Better Auth API-key path. Leave empty in self-host: the JWT
    # path stays disabled and only shared-key + API-key paths are tried.
    # ``allowed_audiences`` is a comma-separated list of accepted ``aud``
    # claims (e.g. "https://api.hail.so,https://mcp.hail.so").
    better_auth_issuer: str = ""
    better_auth_jwks_url: str = ""
    allowed_audiences: str = ""
```

- [ ] **Step 2: Add the matching `.env.example` block.** Append this new section to `.env.example` immediately after the `HAIL_INTERNAL_SECRET=` line (and before the `# Billing posture:` comment block):

```
# ─── Auth backend (cloud) ───────────────────────────────────────────────────
# Set when running against the Better Auth deployment on hail-website. The
# API gains a JWT verification path alongside the existing API-key path:
# tokens with this issuer and an audience in the allow-list are accepted and
# resolved to an organization via the user_id (JWT sub) → members join.
# Leave empty in self-host — the JWT path stays disabled and the existing
# shared-key (HAIL_API_KEY) + API-key (api_keys table) paths are the only
# auth surfaces.
BETTER_AUTH_ISSUER=
BETTER_AUTH_JWKS_URL=
# Comma-separated. Default in cloud: api.${HAIL_DOMAIN},mcp.${HAIL_DOMAIN}.
ALLOWED_AUDIENCES=
```

- [ ] **Step 3: Add the PyJWT dependency** to `api/pyproject.toml`. In the `[project].dependencies` list, append a new line so it reads:

```toml
dependencies = [
    "hailhq-core",
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.30",
    "greenlet>=3.0",
    "psycopg[binary]>=3.2",
    "alembic>=1.13",
    "pyjwt[crypto]>=2.9",
]
```

- [ ] **Step 4: Lock the new dep.** Run from the repo root:

```
uv lock
```

Expected: `uv.lock` updated; `pyjwt` and `cryptography` appear as new entries.

- [ ] **Step 5: Sanity import check**

Run: `cd api && uv run python -c "import jwt; from cryptography.hazmat.primitives.asymmetric import rsa; print(jwt.__version__)"`
Expected: a version like `2.9.0` (or newer); no `ImportError`.

- [ ] **Step 6: Existing API suite is still green** (config addition is a no-op; PyJWT not yet consumed)

Run: `cd api && uv run pytest -q`
Expected: all existing tests pass.

- [ ] **Step 7: Commit**

```bash
git add core/hailhq/core/config.py .env.example api/pyproject.toml uv.lock
git commit -m "$(printf 'feat(api,core): config wiring + PyJWT dep for the JWT auth path\n\nAdd BETTER_AUTH_ISSUER / BETTER_AUTH_JWKS_URL / ALLOWED_AUDIENCES Settings\nfields (and matching .env.example block), and add pyjwt[crypto] to api/\ndependencies. No runtime change yet — consumed in subsequent commits.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

### Task 2: `JWKSCache` + `verify_jwt` in `auth.py`

**Files:**

- Modify: `api/hailhq/api/auth.py`
- Create: `api/tests/test_auth_jwt.py`
- Modify: `api/tests/conftest.py`

TDD this task: each behavior gets a failing test first.

- [ ] **Step 1: Write the conftest fixtures** (RSA keypair, JWKS dict, JWT signer, JWKS-mock client factory) at the END of `api/tests/conftest.py`:

```python
# ----------------------------------------------------------------------
# JWT fixtures — shared across api/tests/test_auth_jwt.py.
# ----------------------------------------------------------------------

import json as _jwt_json
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx as _httpx_for_jwt
import jwt as _jwt_lib
from cryptography.hazmat.primitives import serialization as _serialization
from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
from jwt.algorithms import RSAAlgorithm as _RSAAlgorithm

_TEST_KID = "test-kid-1"


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[bytes, dict[str, Any]]:
    """A throwaway RSA-2048 keypair: (private_pem, public_jwk_dict)."""
    key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=_serialization.Encoding.PEM,
        format=_serialization.PrivateFormat.PKCS8,
        encryption_algorithm=_serialization.NoEncryption(),
    )
    public_jwk = _jwt_json.loads(_RSAAlgorithm.to_jwk(key.public_key()))
    public_jwk["kid"] = _TEST_KID
    public_jwk["use"] = "sig"
    public_jwk["alg"] = "RS256"
    return private_pem, public_jwk


@pytest.fixture()
def jwks_dict(rsa_keypair) -> dict[str, Any]:
    _, public_jwk = rsa_keypair
    return {"keys": [public_jwk]}


@pytest.fixture()
def sign_jwt(rsa_keypair) -> Callable[..., str]:
    """Returns a function that signs an RS256 JWT with the test key."""
    private_pem, _ = rsa_keypair

    def _sign(
        claims: dict[str, Any], *, kid: str = _TEST_KID, alg: str = "RS256"
    ) -> str:
        return _jwt_lib.encode(claims, private_pem, algorithm=alg, headers={"kid": kid})

    return _sign


@pytest.fixture()
def jwks_client_factory(jwks_dict) -> Callable[[], _httpx_for_jwt.AsyncClient]:
    """A factory returning httpx clients whose MockTransport serves the JWKS."""

    def _factory() -> _httpx_for_jwt.AsyncClient:
        def _handler(_req: _httpx_for_jwt.Request) -> _httpx_for_jwt.Response:
            return _httpx_for_jwt.Response(200, json=jwks_dict)

        return _httpx_for_jwt.AsyncClient(transport=_httpx_for_jwt.MockTransport(_handler))

    return _factory


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


@pytest.fixture()
def base_claims() -> Callable[..., dict[str, Any]]:
    """Builds a minimal valid claim set, allowing per-test overrides."""

    def _make(
        *,
        sub: str | None = None,
        iss: str = "https://issuer.example.com",
        aud: str | Iterable[str] = "https://api.example.com",
        exp_offset_seconds: int = 300,
        scope: str | None = None,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        import uuid

        claims: dict[str, Any] = {
            "sub": sub or str(uuid.uuid4()),
            "iss": iss,
            "aud": list(aud) if not isinstance(aud, str) else aud,
            "exp": _now_ts() + exp_offset_seconds,
            "iat": _now_ts(),
        }
        if scope is not None:
            claims["scope"] = scope
        if scopes is not None:
            claims["scopes"] = scopes
        return claims

    return _make
```

Also locate the existing `reset_caches` fixture in `conftest.py` (the one referenced at lines 218-220) and add a JWKS reset call. The fixture currently looks like (find and modify):

```python
@pytest.fixture(autouse=True)
def _reset_deps_caches():
    deps.reset_caches()
    yield
    deps.reset_caches()
```

Change it to also reset the JWKS cache (add the import at the top of `conftest.py` if needed: `from hailhq.api import auth as _auth_module`):

```python
@pytest.fixture(autouse=True)
def _reset_deps_caches():
    deps.reset_caches()
    _auth_module.reset_jwks_cache_for_testing()
    yield
    deps.reset_caches()
    _auth_module.reset_jwks_cache_for_testing()
```

- [ ] **Step 2: Write the failing `JWKSCache` tests** (create `api/tests/test_auth_jwt.py`):

```python
"""Tests for the JWKS cache + JWT verifier in api/hailhq/api/auth.py.

These cover the verifier in isolation; the integration with
``get_current_principal`` is exercised in test_auth.py / test_auth_shared.py
style via test_auth_jwt's later "from the dependency" tests.
"""

from __future__ import annotations

import asyncio
import pytest

import jwt as _jwt_lib

from hailhq.api import auth


_JWKS_URL = "https://issuer.example.com/jwks"


async def test_jwks_cache_fetches_and_returns_pyjwk(jwks_client_factory):
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    pyjwk = await cache.get("test-kid-1")
    assert pyjwk.key_id == "test-kid-1"


async def test_jwks_cache_unknown_kid_after_refresh_raises(jwks_client_factory):
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    with pytest.raises(_jwt_lib.InvalidTokenError):
        await cache.get("not-the-kid")


async def test_jwks_cache_refresh_on_kid_miss(jwks_dict, rsa_keypair):
    """A kid that wasn't in cache should trigger a refresh; if it appears
    in the new JWKS, the second lookup succeeds."""
    import httpx as _httpx

    _, public_jwk = rsa_keypair
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

    cache = auth.JWKSCache(_JWKS_URL, client_factory=_factory)
    # Prime the cache so call #1 happens.
    pyjwk = await cache.get("test-kid-1")
    assert pyjwk.key_id == "test-kid-1"
    # Now ask for a kid we haven't seen — should trigger a refresh.
    rotated = await cache.get("rotated-kid")
    assert rotated.key_id == "rotated-kid"
    assert call_count["n"] == 2


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


async def test_verify_jwt_rejects_expired(
    jwks_client_factory, base_claims, sign_jwt
):
    cache = auth.JWKSCache(_JWKS_URL, client_factory=jwks_client_factory)
    token = sign_jwt(base_claims(exp_offset_seconds=-10))
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
    assert claims["aud"] == "https://mcp.example.com" or "https://mcp.example.com" in claims["aud"]
```

- [ ] **Step 3: Run the new tests; they must FAIL with `AttributeError: module 'hailhq.api.auth' has no attribute 'JWKSCache'`** (and `verify_jwt`, `reset_jwks_cache_for_testing`).

Run: `cd api && uv run pytest tests/test_auth_jwt.py -v`
Expected: collection errors (or each test failing on the missing symbol). This proves the tests are wired before we implement.

- [ ] **Step 4: Implement `auth.py`.** Replace the entire contents of `api/hailhq/api/auth.py` with:

```python
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
``settings.better_auth_jwks_url`` is set (self-host leaves it empty,
disabling the JWT path entirely).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
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


class JWKSCache:
    """Process-local JWKS cache with on-demand refresh.

    Keys are looked up by ``kid``. On a miss we refresh once (handles key
    rotation transparently). The cache is intentionally minimal — Better
    Auth issues keys infrequently and a single API process can keep them
    in memory.
    """

    def __init__(
        self,
        jwks_url: str,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._url = jwks_url
        self._client_factory = client_factory or self._default_client
        self._keys: dict[str, PyJWK] = {}
        self._refreshed_at: datetime | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _default_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS)

    async def get(self, kid: str) -> PyJWK:
        if kid not in self._keys:
            async with self._lock:
                # Double-check under the lock.
                if kid not in self._keys:
                    await self._refresh()
        if kid not in self._keys:
            raise jwt.InvalidTokenError(f"unknown kid {kid!r}")
        return self._keys[kid]

    async def _refresh(self) -> None:
        async with self._client_factory() as client:
            resp = await client.get(self._url)
            resp.raise_for_status()
            jwks = resp.json()
        keys = jwks.get("keys") or []
        self._keys = {
            k["kid"]: PyJWK(k) for k in keys if isinstance(k, dict) and "kid" in k
        }
        self._refreshed_at = datetime.now(timezone.utc)


_jwks_cache: JWKSCache | None = None


def get_jwks_cache() -> JWKSCache | None:
    """Lazy module-level cache. Returns ``None`` if the JWT path is disabled."""
    global _jwks_cache
    if _jwks_cache is None and settings.better_auth_jwks_url:
        _jwks_cache = JWKSCache(settings.better_auth_jwks_url)
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

async def verify_jwt(
    token: str,
    *,
    jwks_cache: JWKSCache,
    issuer: str,
    audiences: list[str],
) -> dict[str, Any]:
    """Verify a Better Auth RS256 JWT against the JWKS and return its claims.

    Raises :class:`jwt.InvalidTokenError` (or a subclass) for any rejection
    — bad signature, wrong issuer/audience, expired, unknown kid, missing
    required claim. Callers turn that into a 401.
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
        algorithms=["RS256"],
        issuer=issuer,
        audience=audiences,
        options={"require": ["iss", "aud", "exp", "sub"]},
    )
    return claims


__all__ = [
    "JWKSCache",
    "get_jwks_cache",
    "hash_key",
    "reset_jwks_cache_for_testing",
    "verify_jwt",
]
```

- [ ] **Step 5: Re-run the JWT tests; they must PASS.**

Run: `cd api && uv run pytest tests/test_auth_jwt.py -v`
Expected: 8 passed.

- [ ] **Step 6: Full API suite is still green.**

Run: `cd api && uv run pytest -q`
Expected: all tests pass (the existing shared-key + API-key tests are untouched).

- [ ] **Step 7: Lint + type check the changed files.**

Run: `cd api && uv run ruff check hailhq/api/auth.py tests/test_auth_jwt.py tests/conftest.py && uv run mypy --namespace-packages --explicit-package-bases hailhq/api/auth.py`
Expected: ruff "All checks passed!"; mypy "Success" (or only pre-existing namespace noise on `hailhq.core.*` — see the 0b precedent).

- [ ] **Step 8: Commit**

```bash
git add api/hailhq/api/auth.py api/tests/test_auth_jwt.py api/tests/conftest.py
git commit -m "$(printf 'feat(api): JWKS cache + RS256 JWT verifier in auth.py\n\nVerifies a Better Auth JWT against the configured JWKS, checking\nsignature/issuer/audience/expiry and requiring iss/aud/exp/sub. The\ncache refreshes on kid miss so key rotation is transparent. Lazy and\nopt-in: ``get_jwks_cache()`` returns None when BETTER_AUTH_JWKS_URL is\nempty, which keeps the path disabled in self-host. Tests cover happy\npath + every rejection mode plus the kid-miss refresh.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

### Task 3: Wire the JWT path into `get_current_principal`

**Files:**

- Modify: `api/hailhq/api/deps.py`
- Modify: `api/tests/test_auth_jwt.py` (extend with integration tests)

- [ ] **Step 1: Write failing integration tests** at the END of `api/tests/test_auth_jwt.py`. These exercise `get_current_principal` through a mounted route, mirroring how `test_auth.py` and `test_auth_shared.py` work.

First, look at `api/tests/test_auth.py:29-65` to see the existing pattern (an in-test FastAPI app mounting a `/whoami` route that depends on `get_current_principal` and returns the principal). Use the same shape — quoted here so you don't need to flip files:

```python
# Add to test_auth_jwt.py
import uuid
import json as _json
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from hailhq.api import deps
from hailhq.api.deps import Principal, get_current_principal


def _whoami_app() -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(
        principal: Principal = Depends(get_current_principal),
    ) -> dict[str, Any]:
        return {
            "api_key_id": str(principal.api_key_id) if principal.api_key_id else None,
            "organization_id": str(principal.organization_id),
            "scopes": principal.scopes,
        }

    return app


def _install_test_jwks(monkeypatch, jwks_client_factory) -> None:
    """Replace the module-level cache with one wired to the test JWKS."""
    from hailhq.api import auth as _auth

    test_cache = _auth.JWKSCache(
        "https://issuer.example.com/jwks", client_factory=jwks_client_factory
    )
    monkeypatch.setattr(_auth, "_jwks_cache", test_cache)


def _configure_env(monkeypatch) -> None:
    monkeypatch.setattr(deps.settings, "better_auth_issuer", "https://issuer.example.com")
    monkeypatch.setattr(deps.settings, "better_auth_jwks_url", "https://issuer.example.com/jwks")
    monkeypatch.setattr(
        deps.settings, "allowed_audiences", "https://api.example.com,https://mcp.example.com"
    )


@pytest.fixture()
def organisation_member_for_jwt(db_session, async_db_factory):
    """Insert a (user_id, organization_id) pair into ``members`` so the JWT
    sub resolves to a real org. Returns (user_id, organization_id) UUIDs.

    The existing test_auth.py uses the same helper pattern; copy/adapt
    whichever fixture you find there for the API-key path. If no helper
    exists, insert two rows directly via the db session: an
    ``OrganizationMember`` row with the user_id/organization_id you choose
    is sufficient (no parent ``Organization`` row is required because the
    API only reads through the join).
    """
    # NB: the test_auth.py fixture name in this repo for the equivalent
    # API-key setup tells you what fixture to depend on. If `db_session`
    # exposes an async SQLAlchemy session, use it directly; otherwise
    # depend on whatever conftest exposes (see the api-key tests).
    raise NotImplementedError("Match this to the api-key test's org-setup fixture.")


async def test_jwt_path_happy_resolves_to_org(
    monkeypatch, jwks_client_factory, base_claims, sign_jwt, organisation_member_for_jwt
):
    _configure_env(monkeypatch)
    _install_test_jwks(monkeypatch, jwks_client_factory)

    user_id, organization_id = organisation_member_for_jwt
    token = sign_jwt(base_claims(sub=str(user_id), aud="https://api.example.com"))

    with TestClient(_whoami_app()) as client:
        resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["organization_id"] == str(organization_id)
    assert body["api_key_id"] is None  # JWT path has no api_keys row
    assert body["scopes"] == ["*"]


async def test_jwt_path_unknown_sub_returns_403(
    monkeypatch, jwks_client_factory, base_claims, sign_jwt
):
    _configure_env(monkeypatch)
    _install_test_jwks(monkeypatch, jwks_client_factory)
    # A sub that doesn't exist in members.
    token = sign_jwt(base_claims(sub=str(uuid.uuid4()), aud="https://api.example.com"))

    with TestClient(_whoami_app()) as client:
        resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert "user not provisioned" in resp.json()["detail"]


async def test_jwt_path_bad_signature_returns_401(
    monkeypatch, jwks_client_factory, base_claims, sign_jwt
):
    _configure_env(monkeypatch)
    _install_test_jwks(monkeypatch, jwks_client_factory)
    token = sign_jwt(base_claims(aud="https://api.example.com"))
    head, body, sig = token.split(".")
    bad = head + "." + body + "." + ("A" + sig[1:] if sig[0] != "A" else "B" + sig[1:])

    with TestClient(_whoami_app()) as client:
        resp = client.get("/whoami", headers={"Authorization": f"Bearer {bad}"})
    assert resp.status_code == 401


async def test_non_jwt_token_falls_through_to_api_key_path(monkeypatch):
    _configure_env(monkeypatch)
    # A non-JWT-shaped token (no dots) reaches the API-key path; with no
    # api_keys table in this in-process test app, the API-key path raises
    # 401 — the test asserts we get there at all (i.e. the JWT path didn't
    # try to verify a non-JWT token and 500).
    with TestClient(_whoami_app()) as client:
        resp = client.get("/whoami", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


async def test_jwt_token_without_jwt_config_returns_401(
    monkeypatch, jwks_client_factory, base_claims, sign_jwt
):
    """JWT path is disabled when env vars are empty (self-host posture)."""
    # Explicitly clear config.
    monkeypatch.setattr(deps.settings, "better_auth_issuer", "")
    monkeypatch.setattr(deps.settings, "better_auth_jwks_url", "")
    monkeypatch.setattr(deps.settings, "allowed_audiences", "")
    token = sign_jwt(base_claims())

    with TestClient(_whoami_app()) as client:
        resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
```

**Note on the `organisation_member_for_jwt` fixture**: inspect `api/tests/test_auth.py` and its conftest at write time. The api-key test inserts an `ApiKey` row + a `members` row; you only need the `members` row for the JWT path (no api_keys row exists for JWT). If `test_auth.py` exposes a helper for inserting a member, reuse it; otherwise write a small async fixture that opens a session via `core.db.session_scope()` and inserts an `OrganizationMember(user_id=..., organization_id=..., role="admin")` row. Return the two UUIDs.

- [ ] **Step 2: Run the failing tests** to confirm they exercise the right code paths.

Run: `cd api && uv run pytest tests/test_auth_jwt.py -v`
Expected: the new integration tests fail (e.g. on `_principal_from_jwt` not existing, or `get_current_principal` not dispatching by token shape). The unit tests from Task 2 still pass.

- [ ] **Step 3: Implement the JWT dispatch in `deps.py`.** Apply these three edits:

(a) Add the import next to the existing `from hailhq.api.auth import hash_key`:

```python
from hailhq.api.auth import (
    JWKSCache,
    get_jwks_cache,
    hash_key,
    verify_jwt,
)
```

(b) Add a `jwt` import at the top with the other stdlib/3rd-party imports:

```python
import jwt as _pyjwt
```

(c) Add these three module-level helpers immediately after `_principal_from_apikey_table` (and before `get_current_principal`):

```python
def _looks_like_jwt(token: str) -> bool:
    """Header.Payload.Signature shape — three non-empty dot-separated parts."""
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def _jwt_configured() -> bool:
    return bool(
        settings.better_auth_issuer
        and settings.better_auth_jwks_url
        and settings.allowed_audiences
    )


def _allowed_audiences() -> list[str]:
    return [a.strip() for a in settings.allowed_audiences.split(",") if a.strip()]


def _scopes_from_jwt(claims: dict) -> list[str]:
    """OAuth ``scope`` (space-separated) or ``scopes`` (list); default ``["*"]``."""
    scope = claims.get("scope")
    if isinstance(scope, str):
        out = [s for s in scope.split() if s]
        return out or ["*"]
    scopes = claims.get("scopes")
    if isinstance(scopes, list):
        out = [str(s) for s in scopes if s]
        return out or ["*"]
    return ["*"]


async def _principal_from_jwt(token: str, db: AsyncSession) -> Principal:
    cache = get_jwks_cache()
    if cache is None:
        # _jwt_configured() should have prevented us getting here, but
        # double-check so we never silently accept an unverified token.
        raise _unauthorized("jwt auth not configured on this deployment")
    try:
        claims = await verify_jwt(
            token,
            jwks_cache=cache,
            issuer=settings.better_auth_issuer,
            audiences=_allowed_audiences(),
        )
    except _pyjwt.InvalidTokenError as exc:
        raise _unauthorized(f"invalid jwt: {exc}") from exc

    sub = str(claims.get("sub") or "")
    try:
        user_uuid = uuid.UUID(sub)
    except ValueError as exc:
        raise _unauthorized("jwt sub is not a valid user id") from exc

    stmt = select(OrganizationMember.organization_id).where(
        OrganizationMember.user_id == user_uuid
    )
    organization_id = (await db.execute(stmt)).scalar_one_or_none()
    if organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "user not provisioned with an organization; "
                "sign in to the dashboard to complete setup"
            ),
        )

    return Principal(
        api_key_id=None,
        organization_id=organization_id,
        scopes=_scopes_from_jwt(claims),
    )
```

(d) Replace `get_current_principal` with the dispatching version:

```python
async def get_current_principal(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_session),
) -> Principal:
    token = _parse_bearer(authorization)

    if _check_shared_key(token):
        return Principal(
            api_key_id=None,
            organization_id=SELF_HOSTED_ORG_ID,
            scopes=["*"],
        )

    if _looks_like_jwt(token):
        if not _jwt_configured():
            raise _unauthorized("invalid API key")
        return await _principal_from_jwt(token, db)

    if await _apikey_table_exists(db):
        return await _principal_from_apikey_table(token, db)

    raise _unauthorized("invalid API key")
```

The unused `JWKSCache` import is removed if mypy complains; otherwise it's fine to keep (small re-export surface).

- [ ] **Step 4: Re-run the JWT tests; they must PASS.**

Run: `cd api && uv run pytest tests/test_auth_jwt.py -v`
Expected: all JWT tests pass (unit + integration). If the `organisation_member_for_jwt` fixture isn't wired yet, use the same approach as `test_auth.py`'s API-key setup; this is the only place in the plan that requires reading a sibling test for an existing helper.

- [ ] **Step 5: Existing shared-key + API-key tests are still green** (regression).

Run: `cd api && uv run pytest -q`
Expected: full suite passes (the dispatch only adds a new branch — the shared-key and API-key branches are byte-for-byte unchanged).

- [ ] **Step 6: Lint + type check.**

Run: `cd api && uv run ruff check hailhq/api/deps.py tests/test_auth_jwt.py && uv run mypy --namespace-packages --explicit-package-bases hailhq/api/deps.py`
Expected: ruff "All checks passed!"; mypy "Success" (or only pre-existing namespace noise).

- [ ] **Step 7: Commit**

```bash
git add api/hailhq/api/deps.py api/tests/test_auth_jwt.py
git commit -m "$(printf 'feat(api): JWT auth path in get_current_principal\n\nDispatches by token shape: a header.payload.signature token routes to a\nnew _principal_from_jwt that verifies via the auth.py helpers and\nresolves organization_id via the same members join the API-key path\nuses. Both existing paths (shared-key, API-key) are byte-for-byte\nunchanged; the JWT path is opt-in via BETTER_AUTH_* env vars.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

## Self-Review

- **Spec coverage** (every item in §"1a — API gains a JWT path"):
  - `_principal_from_jwt` alongside `_principal_from_apikey_table` ✓ Task 3.
  - Try shared-key → API-key → JWT (corrected to shape-dispatch, which is cleaner and the spec's "Decode the bearer; if it doesn't look like a JWT, skip the JWT path" intent) ✓ Task 3 Step 3(d).
  - JWKS fetched on demand, cached, refreshed on kid miss ✓ Task 2 `JWKSCache.get` + the refresh-on-miss test.
  - Signature + issuer + `aud ∈ ALLOWED_AUDIENCES` + expiry verification ✓ Task 2 `verify_jwt` + rejection tests.
  - JWT `sub` → `members.user_id` join ✓ Task 3 `_principal_from_jwt`.
  - 403 "user not provisioned" if no member row ✓ Task 3 + integration test.
  - Scopes parsed (`scope` or `scopes`, default `["*"]`) ✓ Task 3 `_scopes_from_jwt`.
  - PyJWT as the JWT lib ✓ Task 1 Step 3.
  - New env vars + `.env.example` block ✓ Task 1.
  - JWT path disabled when env empty (self-host) ✓ Task 3 `_jwt_configured` + the `test_jwt_token_without_jwt_config_returns_401` test.
  - JWKS fetch failure tolerated: `get_jwks_cache()` returns the cache lazily; if the upstream JWKS is down on first call, the first request 401s and subsequent requests retry (no startup crash). This matches the spec's "if JWKS fetch fails on startup, the JWT path is disabled until the next refresh succeeds; API-key path still works" — though the implementation defers the failure to first use rather than startup, which is functionally equivalent and avoids a startup ordering bug.

- **Placeholder scan:** none — full code in every step, with the one explicit "look at the sibling test for the org-setup fixture" instruction in Task 3 Step 1 (which is a _read_, not a placeholder — the engineer must inspect existing test infra to reuse it cleanly rather than fabricate a duplicate).

- **Type/name consistency:** `JWKSCache`, `get_jwks_cache`, `verify_jwt`, `reset_jwks_cache_for_testing` are all defined in Task 2 and consumed in Task 3 with matching names. `_principal_from_jwt` returns the same `Principal` shape as the API-key path. The dispatch in `get_current_principal` preserves the existing shared-key / API-key return shapes byte-for-byte.

- **Known follow-ups (NOT in this plan):**
  - MCP-side forwarder + per-request `HailClient` (a separate plan).
  - `hail-website` Better Auth `oauth-provider` / `mcp` / `jwt` plugins (a separate plan).
  - Scope enforcement at routes (deferred to Phase 2).
