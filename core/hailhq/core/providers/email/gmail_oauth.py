"""Google OAuth plumbing for connected Gmail accounts.

Endpoints per https://developers.google.com/identity/protocols/oauth2/web-server.
All HTTP goes through httpx (async); no Google SDK. ``state`` tokens reuse
the unsubscribe-token wire format (base64url payload|HMAC), keyed on the
OAuth client secret — no extra env var, and the secret is always present
when the feature is enabled.
"""

from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256
from urllib.parse import urlencode
from uuid import UUID

import httpx
from pydantic import BaseModel

from hailhq.core.config import settings

__all__ = [
    "GMAIL_SCOPES",
    "GmailOAuthError",
    "GmailReauthRequired",
    "InvalidStateToken",
    "TokenGrant",
    "Userinfo",
    "build_authorization_url",
    "exchange_code",
    "fetch_userinfo",
    "mint_state",
    "refresh_access_token",
    "revoke_token",
    "verify_state",
]

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "openid",
    "email",
]

_STATE_TTL_SECONDS = 600


class GmailOAuthError(Exception):
    """Base for OAuth-layer failures against Google."""


class GmailReauthRequired(GmailOAuthError):
    """The refresh token is revoked/expired — the user must reconnect."""


class InvalidStateToken(GmailOAuthError):
    """State param is missing, expired, or tampered."""


class TokenGrant(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_in: int
    scope: str = ""


class Userinfo(BaseModel):
    sub: str
    email: str
    name: str | None = None


def build_authorization_url(*, state: str, redirect_uri: str) -> str:
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        # offline + consent guarantees a refresh_token on every connect,
        # not just the first one for this Google account.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def _post(
    url: str, data: dict[str, str], http: httpx.AsyncClient | None
) -> httpx.Response:
    if http is not None:
        return await http.post(url, data=data)
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await client.post(url, data=data)


async def exchange_code(
    *, code: str, redirect_uri: str, http: httpx.AsyncClient | None = None
) -> TokenGrant:
    resp = await _post(
        GOOGLE_TOKEN_URL,
        {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        http,
    )
    if resp.status_code != 200:
        raise GmailOAuthError(f"code exchange failed: {resp.status_code} {resp.text}")
    return TokenGrant.model_validate(resp.json())


async def fetch_userinfo(
    *, access_token: str, http: httpx.AsyncClient | None = None
) -> Userinfo:
    headers = {"Authorization": f"Bearer {access_token}"}
    if http is not None:
        resp = await http.get(GOOGLE_USERINFO_URL, headers=headers)
    else:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(GOOGLE_USERINFO_URL, headers=headers)
    if resp.status_code != 200:
        raise GmailOAuthError(f"userinfo failed: {resp.status_code} {resp.text}")
    return Userinfo.model_validate(resp.json())


async def refresh_access_token(
    *, refresh_token: str, http: httpx.AsyncClient | None = None
) -> tuple[str, int]:
    resp = await _post(
        GOOGLE_TOKEN_URL,
        {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        http,
    )
    if resp.status_code == 400 and "invalid_grant" in resp.text:
        raise GmailReauthRequired("refresh token revoked or expired")
    if resp.status_code != 200:
        raise GmailOAuthError(f"token refresh failed: {resp.status_code} {resp.text}")
    payload = resp.json()
    return payload["access_token"], int(payload.get("expires_in", 3600))


async def revoke_token(*, token: str, http: httpx.AsyncClient | None = None) -> None:
    resp = await _post(GOOGLE_REVOKE_URL, {"token": token}, http)
    # 400 = already revoked/unknown — the outcome we wanted; stay idempotent.
    if resp.status_code not in (200, 400):
        raise GmailOAuthError(f"revoke failed: {resp.status_code} {resp.text}")


def _sign(payload: str) -> str:
    mac = hmac.new(
        settings.google_oauth_client_secret.encode("utf-8"),
        payload.encode("utf-8"),
        sha256,
    ).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def mint_state(organization_id: UUID, account_id: UUID | None) -> str:
    expiry = int(time.time()) + _STATE_TTL_SECONDS
    payload = f"{organization_id}|{account_id or ''}|{expiry}"
    raw = f"{payload}|{_sign(payload)}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def verify_state(token: str) -> tuple[UUID, UUID | None]:
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        org_s, acct_s, expiry_s, sig = decoded.rsplit("|", 3)
        payload = f"{org_s}|{acct_s}|{expiry_s}"
        if not hmac.compare_digest(sig, _sign(payload)):
            raise InvalidStateToken("bad signature")
        if int(expiry_s) < time.time():
            raise InvalidStateToken("expired")
        return UUID(org_s), UUID(acct_s) if acct_s else None
    except InvalidStateToken:
        raise
    except Exception as exc:  # malformed base64 / uuid / int
        raise InvalidStateToken("malformed state token") from exc
