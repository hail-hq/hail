"""Request-scoped FastAPI dependencies.

Auth dispatches by bearer shape (see ``docs/operations.md``):

* ``HAIL_API_KEY`` (set in env) triggers the shared-key path → sentinel
  Self-hosted org.
* A 3-segment dot-separated token (a JWT) triggers Better Auth OAuth
  verification — only when ``HAIL_AUTH_URL`` and ``HAIL_AUTH_AUDIENCES``
  are configured (the JWKS endpoint is derived from ``HAIL_AUTH_URL``).
  The JWT ``sub`` is resolved to an organization through the ``members`` join.
* Everything else is looked up in the website-owned ``api_keys`` table and
  resolved through the same ``members`` join.
"""

from __future__ import annotations

import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

import jwt as _pyjwt
from fastapi import Depends, Header, HTTPException, status
from hailhq.api.auth import (
    JWKSFetchError,
    get_jwks_cache,
    hash_key,
    verify_jwt,
)
from hailhq.core.config import settings
from hailhq.core.db import get_session, session_scope
from hailhq.core.models import ApiKey, OrganizationMember
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.urls import url_variants
from pydantic import BaseModel
from sqlalchemy import cast, func, select, text, update
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

# Throttle ``lastRequest`` writes so chatty agent traffic doesn't generate one
# no-op UPDATE per request.
_LAST_USED_THROTTLE = timedelta(seconds=60)

# Nil UUID — sentinel organization for every shared-key (HAIL_API_KEY) request.
# No row in ``organizations``; the balance gate is short-circuited for it.
SELF_HOSTED_ORG_ID = uuid.UUID(int=0)


@dataclass
class _Caches:
    # True iff the auth backend's apikey table exists. Self-host operators
    # don't run the auth backend's migrations, so this stays False there.
    apikey_table_present: bool | None = None

    def reset(self) -> None:
        self.apikey_table_present = None


_caches = _Caches()


def reset_caches() -> None:
    """Public hook for tests to clear process-wide caches between runs."""
    _caches.reset()


class Principal(BaseModel):
    """The authenticated caller, exposed to route handlers.

    ``auth_kind`` is the authentication path that produced this principal.
    Prefer it over ``api_key_id is None`` for "is this a billed caller?"
    questions: ``api_key_id`` is ``None`` on **both** the shared-key and JWT
    paths, so it cannot tell a real (billable) console/website session apart
    from the unbilled self-hosted master key. Only ``"shared"`` is exempt from
    billing; ``"apikey"`` and ``"jwt"`` are both billed principals.

    ``api_key_id`` is ``None`` on shared-key (``HAIL_API_KEY``) and JWT
    requests — neither corresponds to a row in ``api_keys``.

    ``user_id`` is the caller's user id: the api-key owner's user uuid on the
    api-key path, the JWT ``sub`` on the JWT path, and ``None`` on the
    shared-key (``HAIL_API_KEY``) path, which carries no caller identity.
    """

    auth_kind: Literal["apikey", "jwt", "shared"]
    api_key_id: uuid.UUID | None
    user_id: uuid.UUID | None
    organization_id: uuid.UUID
    scopes: list[str]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _parse_bearer(authorization: str | None) -> str:
    if not authorization:
        raise _unauthorized("missing Authorization header; expected 'Bearer <api-key>'")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise _unauthorized("invalid Authorization header; expected 'Bearer <api-key>'")
    return parts[1].strip()


def _scopes_from_permissions(perms_json: str | None) -> list[str]:
    """Turn the auth backend's ``permissions`` JSON into a flat scopes list.

    Missing/unparseable permissions imply full-wildcard scope (the auth
    backend stores NULL when the key wasn't created with explicit scopes).
    A *parsed but empty* permissions object means "no scopes" — return [].
    """
    if not perms_json:
        return ["*"]
    try:
        parsed = json.loads(perms_json)
    except json.JSONDecodeError:
        return ["*"]
    if not isinstance(parsed, dict):
        return ["*"]
    out: list[str] = []
    for resource, actions in parsed.items():
        if isinstance(actions, list):
            for a in actions:
                out.append(f"{resource}:{a}")
    return out


async def _apikey_table_exists(db: AsyncSession) -> bool:
    """Probe ``information_schema`` once and cache the result."""
    if _caches.apikey_table_present is None:
        result = await db.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'api_keys' LIMIT 1"
            )
        )
        _caches.apikey_table_present = result.scalar_one_or_none() is not None
    return _caches.apikey_table_present


async def _stamp_last_used(api_key_id: uuid.UUID, ts: datetime) -> None:
    """Update ``lastRequest`` and bump ``requestCount`` in a fresh session.

    ``requestCount`` is nullable in the apikey schema; treat NULL as 0 so the
    first hit becomes 1 instead of staying NULL forever.
    """
    async with session_scope() as session:
        await session.execute(
            update(ApiKey)
            .where(ApiKey.id == api_key_id)
            .values(
                last_request=ts,
                request_count=func.coalesce(ApiKey.request_count, 0) + 1,
            )
        )
        await session.commit()


def _check_shared_key(token: str) -> bool:
    """Constant-time compare against ``HAIL_API_KEY``. Empty config never matches."""
    configured = settings.hail_api_key
    if not configured:
        return False
    return hmac.compare_digest(configured, token)


async def _principal_from_apikey_table(token: str, db: AsyncSession) -> Principal:
    hashed = hash_key(token)
    # api_keys.reference_id is TEXT (opaque upstream); members.user_id is UUID.
    stmt = (
        select(ApiKey, OrganizationMember.organization_id)
        .outerjoin(
            OrganizationMember,
            OrganizationMember.user_id == cast(ApiKey.reference_id, PG_UUID),
        )
        .where(ApiKey.key == hashed)
        # Deterministic org for a multi-org user (api keys carry no active-org
        # hint) — mirrors the JWT path's fallback ordering.
        .order_by(OrganizationMember.organization_id)
    )
    row = (await db.execute(stmt)).first()
    # The auth backend stores ``enabled`` as nullable with an application-level
    # default of true, so NULL means "allowed" — only reject when explicitly False.
    if row is None or row[0].enabled is False:
        raise _unauthorized("invalid API key")

    api_key, organization_id = row

    now = datetime.now(timezone.utc)
    if api_key.expires_at is not None and api_key.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key has expired",
        )

    if organization_id is None:
        # The website's signup hook creates a `members` row alongside every new
        # user; if it's missing here, signup either failed or hasn't run for
        # this user yet. Send them to the dashboard rather than fabricating one.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "user not provisioned with an organization; "
                "sign in to the dashboard to complete setup"
            ),
        )

    if api_key.last_request is None or now - api_key.last_request > _LAST_USED_THROTTLE:
        await _stamp_last_used(api_key.id, now)

    try:
        user_id = uuid.UUID(api_key.reference_id)
    except ValueError:
        # Defense-in-depth only: reference_id is opaque TEXT upstream, but
        # the members-join in the stmt above already CASTs it to PG_UUID in
        # SQL — a non-UUID reference_id 500s there before this parse ever
        # runs (pre-existing, outside this branch). This guard would only
        # bite if that cast is ever loosened (e.g. an outerjoin condition
        # rewritten to tolerate non-UUID reference_ids).
        user_id = None

    return Principal(
        auth_kind="apikey",
        api_key_id=api_key.id,
        user_id=user_id,
        organization_id=organization_id,
        scopes=_scopes_from_permissions(api_key.permissions),
    )


def _looks_like_jwt(token: str) -> bool:
    """Header.Payload.Signature shape — three non-empty dot-separated parts."""
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def _jwt_configured() -> bool:
    return bool(settings.hail_auth_url and settings.hail_auth_audiences)


def _allowed_audiences() -> list[str]:
    """Parse the configured audiences and expand each to its slash variants.

    JWT ``aud`` claims come back to us via tokens minted by hail-website's
    Better Auth oauth-provider, which echoes whatever ``resource=`` Claude
    sent at the token endpoint. Pydantic on the MCP side normalizes that
    resource URL to include a trailing slash, so we tolerate both forms.
    See ``hailhq.core.urls`` for the cross-language rationale.
    """
    raw = [a.strip() for a in settings.hail_auth_audiences.split(",") if a.strip()]
    return [v for aud in raw for v in url_variants(aud)]


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
        # _jwt_configured() should have prevented us getting here; defence
        # in depth — never silently accept an unverified token.
        raise _unauthorized("jwt auth not configured on this deployment")
    try:
        claims = await verify_jwt(
            token,
            jwks_cache=cache,
            issuer=settings.hail_auth_url,
            audiences=_allowed_audiences(),
        )
    except _pyjwt.InvalidTokenError as exc:
        raise _unauthorized(f"invalid jwt: {exc}") from exc
    except JWKSFetchError as exc:
        # The token may be valid — we just couldn't reach the JWKS to check
        # it. Surface a transient 503, not a 401 that tells the client to
        # re-auth. The API-key path (different code path) is unaffected.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth temporarily unavailable; retry shortly",
        ) from exc

    sub = str(claims.get("sub") or "")
    try:
        user_uuid = uuid.UUID(sub)
    except ValueError as exc:
        raise _unauthorized("jwt sub is not a valid user id") from exc

    # Prefer the session's *selected* org (the ``activeOrganizationId`` claim the
    # console mints into its token), validated against membership. A user in
    # several orgs would otherwise resolve to an arbitrary one — and a request
    # could land in the wrong tenant. Fall back to the user's membership only
    # when the claim is absent (e.g. a token minted without an active org).
    stmt = select(OrganizationMember.organization_id).where(
        OrganizationMember.user_id == user_uuid
    )
    active_org_claim = claims.get("activeOrganizationId")
    if active_org_claim:
        try:
            active_org_uuid = uuid.UUID(str(active_org_claim))
        except ValueError as exc:
            raise _unauthorized(
                "jwt activeOrganizationId is not a valid org id"
            ) from exc
        stmt = stmt.where(OrganizationMember.organization_id == active_org_uuid)
        not_member_detail = "user is not a member of the requested organization"
    else:
        # Deterministic single-row pick — never raise MultipleResultsFound for a
        # multi-org user when no active org was supplied.
        stmt = stmt.order_by(OrganizationMember.organization_id)
        not_member_detail = (
            "user not provisioned with an organization; "
            "sign in to the dashboard to complete setup"
        )
    organization_id = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=not_member_detail
        )

    return Principal(
        auth_kind="jwt",
        api_key_id=None,
        user_id=user_uuid,
        organization_id=organization_id,
        scopes=_scopes_from_jwt(claims),
    )


async def get_current_principal(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_session),
) -> Principal:
    token = _parse_bearer(authorization)

    if _check_shared_key(token):
        return Principal(
            auth_kind="shared",
            api_key_id=None,
            user_id=None,
            organization_id=SELF_HOSTED_ORG_ID,
            scopes=["*"],
        )

    # A JWT-shaped bearer is dispatched here before the api_keys lookup. When
    # the JWT path is unconfigured (self-host) we 401 rather than falling
    # through: real ``hl_live_*`` keys never contain dots, so no genuine
    # API key is ever shaped like a JWT and shadowed by this branch.
    if _looks_like_jwt(token):
        if not _jwt_configured():
            raise _unauthorized("invalid API key")
        return await _principal_from_jwt(token, db)

    if await _apikey_table_exists(db):
        return await _principal_from_apikey_table(token, db)

    raise _unauthorized("invalid API key")


def get_s3_mail() -> S3MailClient:
    """Shared mail-bucket S3 client, used by every route touching an
    attachment (inbound reads, outbound uploads, outbound sends)."""
    return S3MailClient(bucket=settings.hail_mail_bucket)
