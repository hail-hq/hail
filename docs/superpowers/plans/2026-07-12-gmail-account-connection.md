# Gmail Account Connection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an org connect Gmail accounts so agents send email as the user's real address (billed at the standard email rate) and read the inbox live without storing anything.

**Architecture:** New `email_accounts` table holds Fernet-encrypted OAuth refresh tokens. An API-hosted OAuth flow mints rows. `POST /emails` grows a Gmail branch behind the existing sender-resolution step; a `GmailClient` (async httpx, no Google SDK) does sends and live reads. Receive is ephemeral: proxy endpoints, no persisted inbound rows, no workers, no webhooks.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + Alembic (api/), shared lib in core/ (`hailhq.core.*`), FastMCP (mcp/), pytest with `httpx.ASGITransport` + dependency overrides.

**Spec:** `docs/superpowers/specs/2026-07-12-gmail-account-connection-design.md`. Two deviations, both intentional: the migration is **0029** (head is 0028; the spec guessed 0021), and the spec's `google_user_id` column is named `provider_user_id` (provider-neutral, matches the `provider` column's Outlook-later story).

## Global Constraints

- Provider adapters live in `core/hailhq/core/providers/email/`; `api/` never imports provider SDKs or calls Google URLs directly (CLAUDE.md invariant).
- New env vars must land in `.env.example` in the same commit.
- Regenerate `openapi/openapi.yaml` in the same PR as any route change (Task 8).
- URLs are not strings: build the OAuth redirect URI with `hailhq.core.urls.join_url`, never f-strings.
- Python style: ruff + black, type-hinted, Pydantic v2, async handlers. Conventional Commits. Do NOT add a Co-Authored-By trailer.
- Run migrations for tests the way existing tests do (`core.testing.fixtures` provides `async_session`/`db`; models' `__table_args__` must mirror the migration exactly so `Base.metadata.create_all` matches).
- Run tests from `api/` with `uv run pytest tests/<file> -v` (venv is the uv workspace root; never `uv sync --extra dev` inside a subpackage).

---

### Task 1: `email_accounts` table — model + migration 0029

**Files:**

- Modify: `core/hailhq/core/models.py` (add `EmailAccount` after `EmailDomain` ~line 703; extend `Email` columns + `__table_args__`)
- Create: `api/migrations/versions/0029_email_accounts.py`
- Test: `core/tests/test_email_account_model.py` (model tests live in core — mirror the fixture usage of `core/tests/test_email_domain_model.py`)

**Interfaces:**

- Produces: `hailhq.core.models.EmailAccount` (columns below), `Email.email_account_id: UUID | None`, `Email.provider_thread_id: str | None`. Later tasks import `EmailAccount` from `hailhq.core.models`.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_email_account_model.py
"""Schema-level tests for the email_accounts table and emails columns."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import Email, EmailAccount


async def _mk_account(session: AsyncSession, org_id, address="alice@gmail.com"):
    acct = EmailAccount(
        organization_id=org_id,
        provider="gmail",
        email_address=address,
        display_name="Alice",
        provider_user_id="google-sub-123",
        scopes=[
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
        ],
        encrypted_refresh_token="gAAAAAB-ciphertext",
    )
    session.add(acct)
    await session.commit()
    await session.refresh(acct)
    return acct


async def test_email_account_defaults(async_session: AsyncSession) -> None:
    acct = await _mk_account(async_session, uuid.uuid4())
    assert acct.status == "active"
    assert acct.provider == "gmail"
    assert acct.created_at is not None


async def test_email_address_globally_unique(async_session: AsyncSession) -> None:
    await _mk_account(async_session, uuid.uuid4())
    with pytest.raises(IntegrityError):
        await _mk_account(async_session, uuid.uuid4())  # same address, other org
    await async_session.rollback()


async def test_email_row_via_account(async_session: AsyncSession) -> None:
    org = uuid.uuid4()
    acct = await _mk_account(async_session, org)
    email = Email(
        organization_id=org,
        email_account_id=acct.id,
        from_address="alice@gmail.com",
        to_addresses=["bob@example.com"],
        subject="hi",
        body_text="hello",
        provider="gmail",
        provider_thread_id="thread-abc",
    )
    async_session.add(email)
    await async_session.commit()
    await async_session.refresh(email)
    assert email.email_domain_id is None
    assert email.provider_thread_id == "thread-abc"


async def test_outbound_requires_some_sender(async_session: AsyncSession) -> None:
    email = Email(
        organization_id=uuid.uuid4(),
        from_address="x@y.com",
        to_addresses=["b@c.com"],
        subject="hi",
        body_text="hello",
    )
    async_session.add(email)
    with pytest.raises(IntegrityError):
        await async_session.commit()
    await async_session.rollback()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_email_account_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'EmailAccount'`

- [ ] **Step 3: Add the model + Email columns**

In `core/hailhq/core/models.py`, add after `EmailDomain` (before `class Email`):

```python
class EmailAccount(Base):
    """A tenant-connected external mailbox (Gmail in v1).

    Unlike ``EmailDomain`` (a DNS identity verified with SES), a row here is
    an OAuth grant: Hail sends *as* the user's own address through the
    provider's API and reads the mailbox live; nothing received is ever
    persisted. ``encrypted_refresh_token`` is Fernet ciphertext under
    ``HAIL_PROVIDER_SECRET_KEY`` (see ``hailhq.core.secret_cipher``) — same
    at-rest posture as ``OrgProviderConfig.encrypted_api_key``. OAuth
    secrets live only in this table so ``email_domains`` serializers can
    never leak them.
    """

    __tablename__ = "email_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, server_default="gmail", nullable=False)
    email_address: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # OIDC ``sub`` from the provider — stable across address renames; used to
    # reject a reconnect performed with a different Google account.
    provider_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, server_default="active", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("provider IN ('gmail')", name="email_accounts_provider_check"),
        CheckConstraint(
            "status IN ('active','reauth_required','disabled')",
            name="email_accounts_status_check",
        ),
        # One connection per mailbox across the whole deployment — two orgs
        # sharing one inbox would let either read the other's threads.
        Index(
            "email_accounts_address_global_uq", "email_address", unique=True
        ),
        Index("email_accounts_org_idx", "organization_id"),
    )
```

In `class Email`, add after `email_domain_id` (models.py:727-731):

```python
    email_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_accounts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Gmail threadId for rows sent through a connected account. Replying
    # resolves the thread from ``in_reply_to`` at send time, so this is
    # informational/audit — but cheap to keep and needed if persisted
    # inbound ever lands.
    provider_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
```

In `Email.__table_args__`, REPLACE the `emails_outbound_has_domain` CheckConstraint with:

```python
        CheckConstraint(
            "direction = 'inbound' OR email_domain_id IS NOT NULL "
            "OR email_account_id IS NOT NULL",
            name="emails_outbound_has_sender",
        ),
        # A row is sent through a domain identity XOR a connected account.
        CheckConstraint(
            "email_domain_id IS NULL OR email_account_id IS NULL",
            name="emails_one_sender_kind",
        ),
        Index("emails_email_account_id_idx", "email_account_id"),
```

- [ ] **Step 4: Write migration 0029**

```python
# api/migrations/versions/0029_email_accounts.py
"""email_accounts: tenant-connected Gmail mailboxes + emails linkage."""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_accounts",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), server_default="gmail", nullable=False),
        sa.Column("email_address", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("provider_user_id", sa.Text(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Text()), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('gmail')", name="email_accounts_provider_check"
        ),
        sa.CheckConstraint(
            "status IN ('active','reauth_required','disabled')",
            name="email_accounts_status_check",
        ),
    )
    op.create_index(
        "email_accounts_address_global_uq",
        "email_accounts",
        ["email_address"],
        unique=True,
    )
    op.create_index(
        "email_accounts_org_idx", "email_accounts", ["organization_id"]
    )

    op.add_column(
        "emails", sa.Column("email_account_id", UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "emails", sa.Column("provider_thread_id", sa.Text(), nullable=True)
    )
    op.create_foreign_key(
        "emails_email_account_id_fkey",
        "emails",
        "email_accounts",
        ["email_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("emails_email_account_id_idx", "emails", ["email_account_id"])
    op.drop_constraint("emails_outbound_has_domain", "emails", type_="check")
    op.create_check_constraint(
        "emails_outbound_has_sender",
        "emails",
        "direction = 'inbound' OR email_domain_id IS NOT NULL "
        "OR email_account_id IS NOT NULL",
    )
    op.create_check_constraint(
        "emails_one_sender_kind",
        "emails",
        "email_domain_id IS NULL OR email_account_id IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint("emails_one_sender_kind", "emails", type_="check")
    op.drop_constraint("emails_outbound_has_sender", "emails", type_="check")
    op.create_check_constraint(
        "emails_outbound_has_domain",
        "emails",
        "direction = 'inbound' OR email_domain_id IS NOT NULL",
    )
    op.drop_index("emails_email_account_id_idx", table_name="emails")
    op.drop_constraint("emails_email_account_id_fkey", "emails", type_="foreignkey")
    op.drop_column("emails", "provider_thread_id")
    op.drop_column("emails", "email_account_id")
    op.drop_index("email_accounts_org_idx", table_name="email_accounts")
    op.drop_index("email_accounts_address_global_uq", table_name="email_accounts")
    op.drop_table("email_accounts")
```

- [ ] **Step 5: Run tests**

Run: `cd core && uv run pytest tests/test_email_account_model.py -v && cd ../api && uv run pytest tests/test_migrations.py -v`
Expected: PASS (both the new tests and the migration round-trip suite)

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/models.py api/migrations/versions/0029_email_accounts.py core/tests/test_email_account_model.py
git commit -m "feat(core): email_accounts table for connected Gmail mailboxes"
```

---

### Task 2: Google OAuth helpers + config

**Files:**

- Create: `core/hailhq/core/providers/email/gmail_oauth.py`
- Modify: `core/hailhq/core/config.py` (add 3 settings after the AWS/email block, ~line 115)
- Modify: `.env.example` (new "Gmail account connection" section)
- Test: `core/tests/providers/test_gmail_oauth.py` (provider tests live here — see `core/tests/providers/test_ses_email.py`)

**Interfaces:**

- Produces (all in `hailhq.core.providers.email.gmail_oauth`):
  - `GMAIL_SCOPES: list[str]`
  - `build_authorization_url(*, state: str, redirect_uri: str) -> str`
  - `async exchange_code(*, code: str, redirect_uri: str, http: httpx.AsyncClient | None = None) -> TokenGrant` where `TokenGrant` is a pydantic model: `access_token: str`, `refresh_token: str | None`, `expires_in: int`, `scope: str`
  - `async fetch_userinfo(*, access_token: str, http=None) -> Userinfo` (`sub: str`, `email: str`, `name: str | None`)
  - `async refresh_access_token(*, refresh_token: str, http=None) -> tuple[str, int]` — `(access_token, expires_in)`; raises `GmailReauthRequired` on `invalid_grant`
  - `async revoke_token(*, token: str, http=None) -> None` — idempotent (Google 400 on already-revoked is swallowed)
  - `mint_state(organization_id: UUID, account_id: UUID | None) -> str` / `verify_state(token: str) -> tuple[UUID, UUID | None]` (raises `InvalidStateToken`); HMAC-SHA256 keyed on `settings.google_oauth_client_secret`, 600 s TTL — same wire format as `hailhq.core.unsubscribe`
  - Exceptions: `GmailOAuthError(Exception)`, `GmailReauthRequired(GmailOAuthError)`, `InvalidStateToken(GmailOAuthError)`
- Consumes: `settings.google_oauth_client_id` / `settings.google_oauth_client_secret` (added this task).

- [ ] **Step 1: Add settings**

In `core/hailhq/core/config.py` after `hail_provider_secret_key` (~line 114):

```python
    # Gmail account connection (docs/superpowers/specs/
    # 2026-07-12-gmail-account-connection-design.md). OAuth client
    # credentials of the operator's Google Cloud app; the hosted app is
    # verified for the gmail.send + gmail.readonly restricted scopes.
    # Self-hosters register their own client (an "internal"-type Workspace
    # app needs no Google review). Empty = the /email-accounts connect
    # endpoints return 503.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # Where the browser lands after a successful connect. Empty = the API
    # renders a minimal HTML success page (fine for CLI/MCP-driven flows).
    hail_email_connect_success_url: str = ""
```

Add matching entries to `.env.example` under a `# --- Gmail account connection ---` section with the same comments condensed.

- [ ] **Step 2: Write the failing tests**

```python
# core/tests/providers/test_gmail_oauth.py
"""Unit tests for gmail_oauth helpers — Google is mocked at the httpx layer."""

from __future__ import annotations

import uuid

import httpx
import pytest

from hailhq.core.providers.email import gmail_oauth
from hailhq.core.providers.email.gmail_oauth import (
    GmailReauthRequired,
    InvalidStateToken,
    build_authorization_url,
    exchange_code,
    mint_state,
    refresh_access_token,
    verify_state,
)


@pytest.fixture(autouse=True)
def _oauth_settings(monkeypatch):
    monkeypatch.setattr(
        gmail_oauth.settings, "google_oauth_client_id", "cid.apps.googleusercontent.com"
    )
    monkeypatch.setattr(gmail_oauth.settings, "google_oauth_client_secret", "csecret")


def test_authorization_url_carries_scopes_and_state() -> None:
    url = build_authorization_url(state="st4te", redirect_uri="https://api.example/cb")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=st4te" in url
    assert "gmail.send" in url and "gmail.readonly" in url


def test_state_roundtrip_and_tamper() -> None:
    org = uuid.uuid4()
    acct = uuid.uuid4()
    token = mint_state(org, acct)
    assert verify_state(token) == (org, acct)
    assert verify_state(mint_state(org, None)) == (org, None)
    with pytest.raises(InvalidStateToken):
        verify_state(token[:-2] + "xx")


async def test_exchange_code_parses_grant() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://oauth2.googleapis.com/token")
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3599,
                "scope": "openid email https://www.googleapis.com/auth/gmail.send",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        grant = await exchange_code(
            code="c0de", redirect_uri="https://api.example/cb", http=http
        )
    assert grant.access_token == "at"
    assert grant.refresh_token == "rt"


async def test_refresh_invalid_grant_raises_reauth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(GmailReauthRequired):
            await refresh_access_token(refresh_token="rt", http=http)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd core && uv run pytest tests/providers/test_gmail_oauth.py -v`
Expected: FAIL — `ModuleNotFoundError: hailhq.core.providers.email.gmail_oauth`

- [ ] **Step 4: Implement `gmail_oauth.py`**

```python
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


async def revoke_token(
    *, token: str, http: httpx.AsyncClient | None = None
) -> None:
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd core && uv run pytest tests/providers/test_gmail_oauth.py -v`
Expected: PASS (all 4)

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/providers/email/gmail_oauth.py core/hailhq/core/config.py .env.example core/tests/providers/test_gmail_oauth.py
git commit -m "feat(core): Google OAuth helpers for Gmail account connection"
```

---

### Task 3: `GmailClient` + `GmailEmailProvider` (send, live reads, thread resolution)

**Files:**

- Create: `core/hailhq/core/providers/email/gmail.py`
- Create: `core/hailhq/core/providers/email/mime.py` (extract `_build_raw_mime` from `ses.py`, add optional `bcc`)
- Modify: `core/hailhq/core/providers/email/ses.py` (import the extracted builder; delete the local copy)
- Modify: `core/hailhq/core/providers/email/base.py` (split `EmailSender` out of `EmailProvider`; add `provider_thread_id` to `ProviderSendResult`)
- Modify: `core/hailhq/core/providers/email/__init__.py` (export `EmailSender`, `GmailEmailProvider`, `GmailClient`)
- Test: `core/tests/providers/test_gmail.py`

**Interfaces:**

- Consumes: `refresh_access_token`, `GmailReauthRequired` from Task 2.
- Produces (in `hailhq.core.providers.email.gmail`):
  - `class GmailApiError(Exception)` with `.status: int`, `.detail: str`; `class GmailAuthError(GmailApiError)` (raised on `GmailReauthRequired` or Gmail 401)
  - `class GmailClient`:
    - `__init__(self, *, refresh_token: str, http: httpx.AsyncClient | None = None)`
    - `async get_profile(self) -> dict` — `{"emailAddress": ...}`
    - `async list_messages(self, *, q: str | None = None, max_results: int = 25, page_token: str | None = None) -> tuple[list[dict], str | None]` — parsed summaries (shape below) + next page token
    - `async get_message(self, message_id: str) -> dict` — parsed detail (shape below)
    - `async find_thread_id(self, rfc822_message_id: str) -> str | None`
    - `async send_message(self, *, raw: bytes, thread_id: str | None = None) -> tuple[str, str]` — `(message_id, thread_id)`
  - Parsed message dict shape (both summary & detail): `{"id", "thread_id", "from_address", "to_addresses": list, "cc_addresses": list, "subject", "date", "snippet", "message_id"}`; detail adds `{"body_text", "body_html", "in_reply_to", "attachments": [{"filename", "content_type", "size_bytes", "attachment_id"}]}`
  - `class GmailEmailProvider(EmailSender)` — `__init__(self, client: GmailClient)`; conforms to the `send_email` protocol; when `headers` contains `In-Reply-To`, resolves the thread via `find_thread_id` and returns `ProviderSendResult(provider_message_id=..., provider_thread_id=...)`
- Produces (in `base.py`): `class EmailSender(ABC)` with the existing `send_email` signature; `class EmailProvider(EmailSender)` keeps `create_identity`/`get_identity`/`delete_identity`; `ProviderSendResult` gains `provider_thread_id: str | None = None`
- Produces (in `mime.py`): `build_raw_mime(*, from_address, to_addresses, subject, body_text, body_html, cc, bcc=None, reply_to, headers, attachments) -> bytes` — identical to today's `_build_raw_mime` plus an optional `Bcc` header (Gmail derives recipients from MIME headers; SES call sites keep passing no `bcc` because SES carries Bcc in `Destination`).

- [ ] **Step 1: Write the failing tests**

```python
# core/tests/providers/test_gmail.py
"""GmailClient / GmailEmailProvider against a mocked Gmail REST API."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from hailhq.core.providers.email.gmail import (
    GmailAuthError,
    GmailClient,
    GmailEmailProvider,
)


def _token_ok(request: httpx.Request) -> httpx.Response | None:
    if request.url.host == "oauth2.googleapis.com":
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
    return None


def _client(handler) -> GmailClient:
    def routed(request: httpx.Request) -> httpx.Response:
        return _token_ok(request) or handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(routed))
    return GmailClient(refresh_token="rt", http=http)


async def test_send_message_posts_base64_raw_and_thread() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/messages/send")
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "m1", "threadId": "t1"})

    provider = GmailEmailProvider(_client(handler))
    result = await provider.send_email(
        from_address="alice@gmail.com",
        to_addresses=["bob@example.com"],
        subject="hi",
        body_text="hello",
        body_html=None,
        bcc=["quiet@example.com"],
    )
    assert result.provider_message_id == "m1"
    assert result.provider_thread_id == "t1"
    raw = base64.urlsafe_b64decode(seen["raw"] + "==").decode()
    assert "Bcc: quiet@example.com" in raw
    assert "threadId" not in seen  # no reply → no thread pinning


async def test_reply_resolves_thread_from_in_reply_to() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages") and request.method == "GET":
            assert "rfc822msgid" in str(request.url)
            return httpx.Response(
                200, json={"messages": [{"id": "orig", "threadId": "t9"}]}
            )
        body = json.loads(request.content)
        assert body["threadId"] == "t9"
        return httpx.Response(200, json={"id": "m2", "threadId": "t9"})

    provider = GmailEmailProvider(_client(handler))
    result = await provider.send_email(
        from_address="alice@gmail.com",
        to_addresses=["bob@example.com"],
        subject="Re: hi",
        body_text="pong",
        body_html=None,
        headers={"In-Reply-To": "<abc@mail.example>", "References": "<abc@mail.example>"},
    )
    assert result.provider_thread_id == "t9"


async def test_gmail_401_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})

    client = _client(handler)
    with pytest.raises(GmailAuthError):
        await client.get_profile()


async def test_get_message_parses_multipart_body() -> None:
    b64 = lambda s: base64.urlsafe_b64encode(s.encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "m3",
                "threadId": "t3",
                "snippet": "hello there",
                "payload": {
                    "mimeType": "multipart/mixed",
                    "headers": [
                        {"name": "From", "value": "Bob <bob@example.com>"},
                        {"name": "To", "value": "alice@gmail.com"},
                        {"name": "Subject", "value": "hi"},
                        {"name": "Date", "value": "Sat, 12 Jul 2026 10:00:00 +0000"},
                        {"name": "Message-ID", "value": "<xyz@mail.example>"},
                    ],
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": b64("hello there")},
                        },
                        {
                            "mimeType": "application/pdf",
                            "filename": "doc.pdf",
                            "body": {"attachmentId": "att1", "size": 1234},
                        },
                    ],
                },
            },
        )

    msg = await _client(handler).get_message("m3")
    assert msg["body_text"] == "hello there"
    assert msg["message_id"] == "<xyz@mail.example>"
    assert msg["attachments"] == [
        {
            "filename": "doc.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1234,
            "attachment_id": "att1",
        }
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd core && uv run pytest tests/providers/test_gmail.py -v`
Expected: FAIL — `ModuleNotFoundError: hailhq.core.providers.email.gmail`

- [ ] **Step 3: Refactor base + mime**

In `base.py`: rename the ABC body so `EmailSender` holds only the abstract `send_email` (verbatim current signature/docstring); `class EmailProvider(EmailSender)` keeps the three identity methods. Add to `ProviderSendResult`:

```python
    # Gmail threadId for connected-account sends; None for SES.
    provider_thread_id: str | None = None
```

Add `"EmailSender"` to `__all__`. Create `mime.py` with the moved builder:

```python
"""Shared raw-MIME builder for provider adapters."""

from __future__ import annotations

from email.message import EmailMessage

from hailhq.core.providers.email.base import ProviderAttachment

__all__ = ["build_raw_mime"]


def build_raw_mime(
    *,
    from_address: str,
    to_addresses: list[str],
    subject: str,
    body_text: str | None,
    body_html: str | None,
    cc: list[str] | None,
    bcc: list[str] | None = None,
    reply_to: str | None,
    headers: dict[str, str],
    attachments: list[ProviderAttachment],
) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = ", ".join(to_addresses)
    if cc:
        msg["Cc"] = ", ".join(cc)
    # Gmail derives recipients from the MIME headers, so Bcc must be present
    # (Gmail strips it before delivery). SES call sites pass no bcc — SES
    # carries Bcc in the API Destination instead.
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    for name, value in headers.items():
        if value:
            msg[name] = value
    if body_text is not None:
        msg.set_content(body_text)
        if body_html is not None:
            msg.add_alternative(body_html, subtype="html")
    elif body_html is not None:
        msg.set_content(body_html, subtype="html")
    for att in attachments:
        maintype, _, subtype = att.content_type.partition("/")
        msg.add_attachment(
            att.payload,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att.filename,
        )
    return msg.as_bytes()
```

In `ses.py`: delete `_build_raw_mime`, `from hailhq.core.providers.email.mime import build_raw_mime`, and change the one call site (`raw = _build_raw_mime(...)` → `raw = build_raw_mime(...)`, same kwargs, no `bcc`).

- [ ] **Step 4: Implement `gmail.py`**

```python
"""Gmail REST adapter for connected accounts (send + ephemeral reads).

All calls go through httpx against the Gmail v1 REST surface — no Google
SDK. One ``GmailClient`` is built per request from the account row's
decrypted refresh token; the access token is cached on the instance so a
send that also resolves a thread pays for one token refresh, not two.
Nothing here persists anything: reads are pass-through by design (spec
§4 — ephemeral only).
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx

from hailhq.core.providers.email.base import (
    EmailSender,
    ProviderAttachment,
    ProviderSendResult,
)
from hailhq.core.providers.email.gmail_oauth import (
    GmailReauthRequired,
    refresh_access_token,
)
from hailhq.core.providers.email.mime import build_raw_mime

__all__ = ["GmailApiError", "GmailAuthError", "GmailClient", "GmailEmailProvider"]

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

_SUMMARY_HEADERS = "From,To,Cc,Subject,Date,Message-ID,In-Reply-To"


class GmailApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"gmail api error {status}: {detail}")
        self.status = status
        self.detail = detail


class GmailAuthError(GmailApiError):
    """Grant revoked/expired — surface as reauth_required upstream."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class GmailClient:
    def __init__(self, *, refresh_token: str, http: httpx.AsyncClient | None = None) -> None:
        self._refresh_token = refresh_token
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._access_token: str | None = None
        self._token_expiry = 0.0

    async def _token(self) -> str:
        if self._access_token is None or time.time() > self._token_expiry - 60:
            try:
                token, expires_in = await refresh_access_token(
                    refresh_token=self._refresh_token, http=self._http
                )
            except GmailReauthRequired as exc:
                raise GmailAuthError(401, str(exc)) from exc
            self._access_token = token
            self._token_expiry = time.time() + expires_in
        return self._access_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        resp = await self._http.request(
            method, f"{GMAIL_API}{path}", params=params, json=json_body, headers=headers
        )
        if resp.status_code == 401:
            raise GmailAuthError(401, resp.text)
        if resp.status_code >= 400:
            raise GmailApiError(resp.status_code, resp.text)
        return resp.json()

    async def get_profile(self) -> dict[str, Any]:
        return await self._request("GET", "/profile")

    async def send_message(
        self, *, raw: bytes, thread_id: str | None = None
    ) -> tuple[str, str]:
        body: dict[str, Any] = {"raw": _b64url(raw)}
        if thread_id:
            body["threadId"] = thread_id
        data = await self._request("POST", "/messages/send", json_body=body)
        return data["id"], data.get("threadId", "")

    async def find_thread_id(self, rfc822_message_id: str) -> str | None:
        data = await self._request(
            "GET",
            "/messages",
            params={"q": f"rfc822msgid:{rfc822_message_id}", "maxResults": 1},
        )
        messages = data.get("messages") or []
        return messages[0]["threadId"] if messages else None

    async def list_messages(
        self,
        *,
        q: str | None = None,
        max_results: int = 25,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {"maxResults": max_results}
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token
        data = await self._request("GET", "/messages", params=params)
        summaries: list[dict[str, Any]] = []
        for ref in data.get("messages") or []:
            meta = await self._request(
                "GET",
                f"/messages/{ref['id']}",
                params={"format": "metadata", "metadataHeaders": _SUMMARY_HEADERS.split(",")},
            )
            summaries.append(_parse_message(meta, include_body=False))
        return summaries, data.get("nextPageToken")

    async def get_message(self, message_id: str) -> dict[str, Any]:
        data = await self._request(
            "GET", f"/messages/{message_id}", params={"format": "full"}
        )
        return _parse_message(data, include_body=True)


def _headers_map(payload: dict[str, Any]) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"] for h in payload.get("headers") or []
    }


def _split_addresses(value: str | None) -> list[str]:
    return [part.strip() for part in value.split(",")] if value else []


def _walk_parts(part: dict[str, Any], out: dict[str, Any]) -> None:
    mime = part.get("mimeType", "")
    body = part.get("body") or {}
    filename = part.get("filename") or ""
    if filename and body.get("attachmentId"):
        out["attachments"].append(
            {
                "filename": filename,
                "content_type": mime,
                "size_bytes": body.get("size", 0),
                "attachment_id": body["attachmentId"],
            }
        )
    elif mime == "text/plain" and body.get("data") and out["body_text"] is None:
        out["body_text"] = _b64url_decode(body["data"]).decode("utf-8", "replace")
    elif mime == "text/html" and body.get("data") and out["body_html"] is None:
        out["body_html"] = _b64url_decode(body["data"]).decode("utf-8", "replace")
    for child in part.get("parts") or []:
        _walk_parts(child, out)


def _parse_message(data: dict[str, Any], *, include_body: bool) -> dict[str, Any]:
    payload = data.get("payload") or {}
    headers = _headers_map(payload)
    parsed: dict[str, Any] = {
        "id": data["id"],
        "thread_id": data.get("threadId", ""),
        "from_address": headers.get("from", ""),
        "to_addresses": _split_addresses(headers.get("to")),
        "cc_addresses": _split_addresses(headers.get("cc")),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": data.get("snippet", ""),
        "message_id": headers.get("message-id", ""),
    }
    if include_body:
        parsed.update(
            {"body_text": None, "body_html": None, "attachments": [],
             "in_reply_to": headers.get("in-reply-to")}
        )
        _walk_parts(payload, parsed)
    return parsed


class GmailEmailProvider(EmailSender):
    """``EmailSender`` conformance for connected-account sends.

    Threading: an ``In-Reply-To`` entry in ``headers`` triggers a
    ``rfc822msgid:`` lookup so the send lands in the right Gmail thread.
    """

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    async def send_email(
        self,
        *,
        from_address: str,
        to_addresses: list[str],
        subject: str,
        body_text: str | None,
        body_html: str | None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
        attachments: list[ProviderAttachment] | None = None,
    ) -> ProviderSendResult:
        if not to_addresses:
            raise ValueError("send_email requires at least one recipient")
        if body_text is None and body_html is None:
            raise ValueError("send_email requires body_text or body_html")

        clean_headers = {k: v for k, v in (headers or {}).items() if v}
        thread_id: str | None = None
        in_reply_to = clean_headers.get("In-Reply-To")
        if in_reply_to:
            thread_id = await self._client.find_thread_id(in_reply_to)

        raw = build_raw_mime(
            from_address=from_address,
            to_addresses=to_addresses,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            headers=clean_headers,
            attachments=attachments or [],
        )
        message_id, sent_thread = await self._client.send_message(
            raw=raw, thread_id=thread_id
        )
        return ProviderSendResult(
            provider_message_id=message_id,
            provider_thread_id=sent_thread or thread_id,
        )
```

Update `core/hailhq/core/providers/email/__init__.py` to also export `EmailSender`, `GmailClient`, `GmailEmailProvider` (match its existing export style).

- [ ] **Step 5: Run tests — new file plus SES/email regression**

Run: `cd core && uv run pytest tests/providers/ -v && cd ../api && uv run pytest tests/test_emails_api.py tests/test_email_domains_api.py -v`
Expected: PASS (Gmail tests green; SES provider + API suites unaffected by the mime/base refactor)

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/providers/email/ core/tests/providers/test_gmail.py
git commit -m "feat(core): GmailClient + GmailEmailProvider adapter"
```

---

### Task 4: `/email-accounts` routes — connect, callback, list, get, patch, delete, reconnect

**Files:**

- Create: `api/hailhq/api/routes/email_accounts.py`
- Modify: `core/hailhq/core/schemas.py` (add schemas after the EmailDomain group)
- Modify: `api/hailhq/api/main.py` (import + `app.include_router(email_accounts_routes.router)` next to the other routers, lines 27-40 and 236-240)
- Test: `api/tests/test_email_accounts_api.py`

**Interfaces:**

- Consumes: Task 1 model, Task 2 oauth helpers, `SecretCipher` (`hailhq.core.secret_cipher`), `Principal`/`get_current_principal`, `fetch_cursor_page`, `write_audit_log`, `join_url`.
- Produces:
  - Routes: `POST /email-accounts/connect`, `GET /email-accounts/oauth/callback`, `GET /email-accounts`, `GET /email-accounts/{id}`, `PATCH /email-accounts/{id}`, `DELETE /email-accounts/{id}`, `POST /email-accounts/{id}/reconnect`
  - Dependency **`get_gmail_client_builder() -> Callable[[EmailAccount], GmailClient]`** — decrypts the refresh token with `SecretCipher(settings.hail_provider_secret_key)` and returns a `GmailClient`; FastAPI-overridable in tests. Tasks 5 & 6 consume this.
  - Helper **`require_account(db, organization_id, account_id) -> EmailAccount`** — 404 if missing/other-org.
  - Schemas (`core/hailhq/core/schemas.py`): `EmailAccountResponse` (`id`, `provider`, `email_address`, `display_name`, `status`, `scopes`, `created_at`, `updated_at` — **never token material**), `EmailAccountListResponse` (`items`, `next_cursor`), `EmailAccountConnectResponse` (`authorization_url: str`), `EmailAccountPatch` (`status: Literal["active", "disabled"]`).

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_email_accounts_api.py
"""Route tests for /email-accounts. Google is mocked at the gmail_oauth layer."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from hailhq.core.models import Email, EmailAccount
from hailhq.core.providers.email.gmail_oauth import TokenGrant, Userinfo, mint_state


@pytest.fixture(autouse=True)
def _feature_settings(monkeypatch):
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "google_oauth_client_id", "cid")
    monkeypatch.setattr(config.settings, "google_oauth_client_secret", "csecret")
    monkeypatch.setattr(
        config.settings,
        "hail_provider_secret_key",
        # any valid Fernet key works for tests
        __import__("hailhq.core.secret_cipher", fromlist=["generate_key"]).generate_key(),
    )


async def _insert_account(session, org_id, address="alice@gmail.com", status="active"):
    acct = EmailAccount(
        organization_id=org_id,
        email_address=address,
        provider_user_id="sub-1",
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        encrypted_refresh_token="ciphertext",
        status=status,
    )
    session.add(acct)
    await session.commit()
    await session.refresh(acct)
    return acct


async def test_connect_returns_google_url(client, org_and_key):
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-accounts/connect", headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 200
    url = resp.json()["authorization_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "state=" in url


async def test_connect_503_when_unconfigured(client, org_and_key, monkeypatch):
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "google_oauth_client_id", "")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-accounts/connect", headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 503


async def test_callback_creates_account(client, org_and_key, async_session):
    org_id, _, _ = org_and_key
    grant = TokenGrant(access_token="at", refresh_token="rt", expires_in=3599)
    info = Userinfo(sub="sub-1", email="alice@gmail.com", name="Alice")
    with (
        patch(
            "hailhq.api.routes.email_accounts.exchange_code",
            new=AsyncMock(return_value=grant),
        ),
        patch(
            "hailhq.api.routes.email_accounts.fetch_userinfo",
            new=AsyncMock(return_value=info),
        ),
    ):
        resp = await client.get(
            "/email-accounts/oauth/callback",
            params={"code": "c0de", "state": mint_state(org_id, None)},
        )
    assert resp.status_code == 200  # minimal HTML success page
    row = (
        await async_session.execute(
            select(EmailAccount).where(EmailAccount.organization_id == org_id)
        )
    ).scalar_one()
    assert row.email_address == "alice@gmail.com"
    assert row.status == "active"
    assert row.encrypted_refresh_token != "rt"  # stored encrypted, not plaintext


async def test_callback_rejects_bad_state(client):
    resp = await client.get(
        "/email-accounts/oauth/callback", params={"code": "x", "state": "garbage"}
    )
    assert resp.status_code == 400


async def test_list_never_leaks_tokens(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    await _insert_account(async_session, org_id)
    resp = await client.get(
        "/email-accounts", headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["email_address"] == "alice@gmail.com"
    assert "refresh" not in str(resp.json()).lower()
    assert "encrypted" not in str(resp.json()).lower()


async def test_patch_disable_and_enable(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    resp = await client.patch(
        f"/email-accounts/{acct.id}",
        json={"status": "disabled"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"


async def test_delete_revokes_and_deletes(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    with patch(
        "hailhq.api.routes.email_accounts.revoke_token", new=AsyncMock()
    ) as revoke:
        resp = await client.delete(
            f"/email-accounts/{acct.id}", headers={"Authorization": f"Bearer {plain}"}
        )
    assert resp.status_code == 204
    revoke.assert_awaited_once()


async def test_delete_409_when_emails_reference(client, org_and_key, async_session):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    async_session.add(
        Email(
            organization_id=org_id,
            email_account_id=acct.id,
            from_address="alice@gmail.com",
            to_addresses=["b@c.com"],
            subject="s",
            body_text="t",
            provider="gmail",
        )
    )
    await async_session.commit()
    with patch("hailhq.api.routes.email_accounts.revoke_token", new=AsyncMock()):
        resp = await client.delete(
            f"/email-accounts/{acct.id}", headers={"Authorization": f"Bearer {plain}"}
        )
    assert resp.status_code == 409


async def test_reconnect_rejects_different_google_account(
    client, org_and_key, async_session
):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    resp = await client.post(
        f"/email-accounts/{acct.id}/reconnect",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    grant = TokenGrant(access_token="at", refresh_token="rt2", expires_in=3599)
    other = Userinfo(sub="DIFFERENT-sub", email="alice@gmail.com")
    with (
        patch(
            "hailhq.api.routes.email_accounts.exchange_code",
            new=AsyncMock(return_value=grant),
        ),
        patch(
            "hailhq.api.routes.email_accounts.fetch_userinfo",
            new=AsyncMock(return_value=other),
        ),
    ):
        resp = await client.get(
            "/email-accounts/oauth/callback",
            params={"code": "c", "state": mint_state(org_id, acct.id)},
        )
    assert resp.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && uv run pytest tests/test_email_accounts_api.py -v`
Expected: FAIL — 404s (router not registered) / import errors

- [ ] **Step 3: Add schemas**

In `core/hailhq/core/schemas.py`, after the EmailDomain response models:

```python
class EmailAccountResponse(BaseModel):
    """A connected external mailbox. NEVER includes token material."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: str
    email_address: str
    display_name: str | None
    status: Literal["active", "reauth_required", "disabled"]
    scopes: list[str]
    created_at: datetime
    updated_at: datetime


class EmailAccountListResponse(BaseModel):
    items: list[EmailAccountResponse]
    next_cursor: str | None = None


class EmailAccountConnectResponse(BaseModel):
    authorization_url: str


class EmailAccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # reauth_required is server-managed: it is set by send/read failures and
    # cleared only by a successful reconnect callback — PATCH can't fake it.
    status: Literal["active", "disabled"]
```

- [ ] **Step 4: Implement the routes**

```python
# api/hailhq/api/routes/email_accounts.py
"""Routes for connected external mailboxes (Gmail in v1).

POST   /email-accounts/connect        — mint a Google consent URL.
GET    /email-accounts/oauth/callback — OAuth redirect target (unauthenticated;
                                        trust comes from the signed state token).
GET    /email-accounts                — cursor-paginated list (org-scoped).
GET    /email-accounts/{id}           — single account.
PATCH  /email-accounts/{id}           — enable/disable.
DELETE /email-accounts/{id}           — revoke at Google + delete (409 while
                                        emails rows reference it).
POST   /email-accounts/{id}/reconnect — consent URL for an existing row.

Live mailbox reads live here too (Task 5). Design spec:
docs/superpowers/specs/2026-07-12-gmail-account-connection-design.md
"""

from __future__ import annotations

import logging
from typing import Annotated, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.audit import write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.pagination import fetch_cursor_page
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.models import EmailAccount
from hailhq.core.providers.email.gmail import GmailClient
from hailhq.core.providers.email.gmail_oauth import (
    InvalidStateToken,
    build_authorization_url,
    exchange_code,
    fetch_userinfo,
    mint_state,
    revoke_token,
    verify_state,
)
from hailhq.core.schemas import (
    EmailAccountConnectResponse,
    EmailAccountListResponse,
    EmailAccountPatch,
    EmailAccountResponse,
)
from hailhq.core.secret_cipher import SecretCipher, SecretKeyMissing
from hailhq.core.urls import join_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email-accounts", tags=["email-accounts"])

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200


def _require_configured() -> None:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Gmail account connection is not configured on this server; "
                "set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET"
            ),
        )
    if not settings.hail_provider_secret_key:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HAIL_PROVIDER_SECRET_KEY must be set to store OAuth tokens",
        )


def _cipher() -> SecretCipher:
    try:
        return SecretCipher(settings.hail_provider_secret_key)
    except SecretKeyMissing as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


def _redirect_uri() -> str:
    return join_url(settings.hail_api_url, "email-accounts/oauth/callback")


def get_gmail_client_builder() -> Callable[[EmailAccount], GmailClient]:
    """Build a ``GmailClient`` from an account row. Overridable in tests."""

    cipher = _cipher()

    def build(account: EmailAccount) -> GmailClient:
        return GmailClient(
            refresh_token=cipher.decrypt(account.encrypted_refresh_token)
        )

    return build


async def require_account(
    db: AsyncSession, organization_id: UUID, account_id: UUID
) -> EmailAccount:
    account = (
        await db.execute(
            select(EmailAccount).where(
                EmailAccount.id == account_id,
                EmailAccount.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="email account not found"
        )
    return account


@router.post("/connect", response_model=EmailAccountConnectResponse)
async def connect_email_account(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> EmailAccountConnectResponse:
    _require_configured()
    state = mint_state(principal.organization_id, None)
    return EmailAccountConnectResponse(
        authorization_url=build_authorization_url(
            state=state, redirect_uri=_redirect_uri()
        )
    )


@router.post("/{account_id}/reconnect", response_model=EmailAccountConnectResponse)
async def reconnect_email_account(
    account_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> EmailAccountConnectResponse:
    _require_configured()
    account = await require_account(db, principal.organization_id, account_id)
    state = mint_state(principal.organization_id, account.id)
    return EmailAccountConnectResponse(
        authorization_url=build_authorization_url(
            state=state, redirect_uri=_redirect_uri()
        )
    )


_SUCCESS_HTML = """<!doctype html><meta charset="utf-8">
<title>Mailbox connected</title>
<body style="font-family: system-ui; margin: 4rem auto; max-width: 30rem">
<h1>✅ Mailbox connected</h1>
<p>{address} is now connected to Hail. You can close this tab.</p>
</body>"""


@router.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(
    db: Annotated[AsyncSession, Depends(get_session)],
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> Response:
    """Google's redirect target. Unauthenticated by necessity — the signed
    ``state`` token (org id + optional account id, 10-minute TTL) is what
    binds the browser back to the initiating org."""
    _require_configured()
    if error:
        return HTMLResponse(f"<h1>Connection cancelled</h1><p>{error}</p>", 400)
    if not code or not state:
        return HTMLResponse("<h1>Missing code or state</h1>", 400)
    try:
        organization_id, account_id = verify_state(state)
    except InvalidStateToken:
        return HTMLResponse("<h1>Invalid or expired state token</h1>", 400)

    grant = await exchange_code(code=code, redirect_uri=_redirect_uri())
    if not grant.refresh_token:
        return HTMLResponse(
            "<h1>Google returned no refresh token</h1>"
            "<p>Remove Hail's access at myaccount.google.com/permissions "
            "and connect again.</p>",
            400,
        )
    info = await fetch_userinfo(access_token=grant.access_token)
    cipher = _cipher()
    encrypted = cipher.encrypt(grant.refresh_token)
    scopes = grant.scope.split() if grant.scope else []

    if account_id is not None:
        # Reconnect of a known row — must be the same Google account.
        account = (
            await db.execute(
                select(EmailAccount).where(
                    EmailAccount.id == account_id,
                    EmailAccount.organization_id == organization_id,
                )
            )
        ).scalar_one_or_none()
        if account is None:
            return HTMLResponse("<h1>Unknown account</h1>", 404)
        if account.provider_user_id != info.sub:
            return HTMLResponse(
                "<h1>Wrong Google account</h1>"
                f"<p>This connection belongs to {account.email_address}; you "
                f"authorized {info.email}. Retry with the right account.</p>",
                409,
            )
        account.encrypted_refresh_token = encrypted
        account.email_address = info.email
        account.scopes = scopes or account.scopes
        account.status = "active"
    else:
        existing = (
            await db.execute(
                select(EmailAccount).where(EmailAccount.email_address == info.email)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.organization_id != organization_id:
                return HTMLResponse(
                    "<h1>Already connected elsewhere</h1>"
                    f"<p>{info.email} is connected to a different organization.</p>",
                    409,
                )
            # Same org re-connecting the same mailbox — refresh in place.
            existing.encrypted_refresh_token = encrypted
            existing.provider_user_id = info.sub
            existing.scopes = scopes or existing.scopes
            existing.status = "active"
        else:
            db.add(
                EmailAccount(
                    organization_id=organization_id,
                    provider="gmail",
                    email_address=info.email,
                    display_name=info.name,
                    provider_user_id=info.sub,
                    scopes=scopes,
                    encrypted_refresh_token=encrypted,
                    status="active",
                )
            )
    await db.commit()
    await write_audit_log(
        organization_id=organization_id,
        api_key_id=None,
        action="email_account.connected",
        resource_type="email_account",
        resource_id=account_id,
        payload={"email_address": info.email},
    )
    if settings.hail_email_connect_success_url:
        return RedirectResponse(settings.hail_email_connect_success_url, 303)
    return HTMLResponse(_SUCCESS_HTML.format(address=info.email))


@router.get("", response_model=EmailAccountListResponse)
async def list_email_accounts(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
) -> EmailAccountListResponse:
    rows, next_cursor = await fetch_cursor_page(
        db,
        select(EmailAccount).where(
            EmailAccount.organization_id == principal.organization_id
        ),
        EmailAccount.created_at,
        EmailAccount.id,
        cursor=cursor,
        limit=limit,
    )
    return EmailAccountListResponse(
        items=[EmailAccountResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.get("/{account_id}", response_model=EmailAccountResponse)
async def get_email_account(
    account_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> EmailAccountResponse:
    account = await require_account(db, principal.organization_id, account_id)
    return EmailAccountResponse.model_validate(account)


@router.patch("/{account_id}", response_model=EmailAccountResponse)
async def patch_email_account(
    account_id: UUID,
    body: EmailAccountPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> EmailAccountResponse:
    account = await require_account(db, principal.organization_id, account_id)
    account.status = body.status
    await db.commit()
    await db.refresh(account)
    return EmailAccountResponse.model_validate(account)


@router.delete("/{account_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_email_account(
    account_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    account = await require_account(db, principal.organization_id, account_id)
    cipher = _cipher()
    try:
        await revoke_token(token=cipher.decrypt(account.encrypted_refresh_token))
    except Exception:
        # Best-effort: a Google outage must not strand the delete; the row
        # (and its ciphertext) is gone either way.
        logger.warning("google token revoke failed for %s", account_id, exc_info=True)
    await db.delete(account)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                "email account has sent emails referencing it; disable it "
                "instead (PATCH {\"status\": \"disabled\"})"
            ),
        ) from None
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="email_account.deleted",
        resource_type="email_account",
        resource_id=account_id,
        payload={"email_address": account.email_address},
    )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
```

Register in `main.py`: add `from hailhq.api.routes import email_accounts as email_accounts_routes` beside the other route imports and `app.include_router(email_accounts_routes.router)` beside the other `include_router` calls.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd api && uv run pytest tests/test_email_accounts_api.py -v`
Expected: PASS (all 9)

- [ ] **Step 6: Commit**

```bash
git add api/hailhq/api/routes/email_accounts.py api/hailhq/api/main.py core/hailhq/core/schemas.py api/tests/test_email_accounts_api.py
git commit -m "feat(api): /email-accounts OAuth connect + management routes"
```

---

### Task 5: Live mailbox reads (ephemeral proxy endpoints)

**Files:**

- Modify: `api/hailhq/api/routes/email_accounts.py` (append two routes)
- Modify: `core/hailhq/core/schemas.py` (mailbox message schemas)
- Test: `api/tests/test_mailbox_reads_api.py`

**Interfaces:**

- Consumes: `get_gmail_client_builder`, `require_account` (Task 4); `GmailClient.list_messages` / `get_message` / `GmailAuthError` (Task 3).
- Produces:
  - `GET /email-accounts/{id}/messages?q=&max_results=&page_token=` → `MailboxMessageListResponse`
  - `GET /email-accounts/{id}/messages/{message_id}` → `MailboxMessageDetail`
  - Schemas: `MailboxMessageSummary` (`id`, `thread_id`, `from_address`, `to_addresses`, `cc_addresses`, `subject`, `date`, `snippet`, `message_id`), `MailboxMessageDetail` (summary + `body_text`, `body_html`, `in_reply_to`, `attachments: list[MailboxAttachment]`), `MailboxAttachment` (`filename`, `content_type`, `size_bytes`, `attachment_id`), `MailboxMessageListResponse` (`items`, `next_page_token`).
  - Shared helper `_account_gmail_or_409(db, account, builder)`-style handling: account `status != 'active'` → 409 with reconnect hint; `GmailAuthError` during the call → set `status='reauth_required'`, commit, raise 409.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_mailbox_reads_api.py
"""Ephemeral mailbox reads — nothing is persisted; Gmail is faked."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from hailhq.api.main import app
from hailhq.api.routes.email_accounts import get_gmail_client_builder
from hailhq.core.models import Email, EmailAccount
from hailhq.core.providers.email.gmail import GmailAuthError

from tests.test_email_accounts_api import _insert_account  # reuse row helper


class FakeGmail:
    def __init__(self, *, fail_auth: bool = False) -> None:
        self.fail_auth = fail_auth

    async def list_messages(self, *, q=None, max_results=25, page_token=None):
        if self.fail_auth:
            raise GmailAuthError(401, "revoked")
        summary = {
            "id": "m1",
            "thread_id": "t1",
            "from_address": "Bob <bob@example.com>",
            "to_addresses": ["alice@gmail.com"],
            "cc_addresses": [],
            "subject": "hi",
            "date": "Sat, 12 Jul 2026 10:00:00 +0000",
            "snippet": "hello",
            "message_id": "<xyz@mail.example>",
        }
        return [summary], None

    async def get_message(self, message_id):
        return {
            "id": message_id,
            "thread_id": "t1",
            "from_address": "Bob <bob@example.com>",
            "to_addresses": ["alice@gmail.com"],
            "cc_addresses": [],
            "subject": "hi",
            "date": "Sat, 12 Jul 2026 10:00:00 +0000",
            "snippet": "hello",
            "message_id": "<xyz@mail.example>",
            "body_text": "hello",
            "body_html": None,
            "in_reply_to": None,
            "attachments": [],
        }


@pytest.fixture()
def fake_gmail():
    fake = FakeGmail()
    app.dependency_overrides[get_gmail_client_builder] = lambda: (lambda account: fake)
    yield fake
    app.dependency_overrides.pop(get_gmail_client_builder, None)


async def test_list_messages_proxies_and_stores_nothing(
    client, org_and_key, async_session, fake_gmail
):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    resp = await client.get(
        f"/email-accounts/{acct.id}/messages",
        params={"q": "in:inbox"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["subject"] == "hi"
    count = (await async_session.execute(select(func.count(Email.id)))).scalar_one()
    assert count == 0  # ephemeral: no rows written


async def test_get_message_detail(client, org_and_key, async_session, fake_gmail):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    resp = await client.get(
        f"/email-accounts/{acct.id}/messages/m1",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    assert resp.json()["body_text"] == "hello"
    assert resp.json()["message_id"] == "<xyz@mail.example>"


async def test_disabled_account_409(client, org_and_key, async_session, fake_gmail):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id, status="disabled")
    resp = await client.get(
        f"/email-accounts/{acct.id}/messages",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 409


async def test_auth_error_flags_reauth_required(
    client, org_and_key, async_session
):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    fake = FakeGmail(fail_auth=True)
    app.dependency_overrides[get_gmail_client_builder] = lambda: (lambda a: fake)
    try:
        resp = await client.get(
            f"/email-accounts/{acct.id}/messages",
            headers={"Authorization": f"Bearer {plain}"},
        )
    finally:
        app.dependency_overrides.pop(get_gmail_client_builder, None)
    assert resp.status_code == 409
    await async_session.refresh(acct)
    assert acct.status == "reauth_required"


async def test_other_org_account_404(client, org_and_key, async_session, fake_gmail):
    _, _, plain = org_and_key
    other = await _insert_account(async_session, uuid.uuid4(), address="x@gmail.com")
    resp = await client.get(
        f"/email-accounts/{other.id}/messages",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && uv run pytest tests/test_mailbox_reads_api.py -v`
Expected: FAIL — 404 (routes missing) / schema import errors

- [ ] **Step 3: Add schemas + routes**

Schemas (in `core/hailhq/core/schemas.py`):

```python
class MailboxAttachment(BaseModel):
    filename: str
    content_type: str
    size_bytes: int
    attachment_id: str


class MailboxMessageSummary(BaseModel):
    """A live-read Gmail message. Never persisted (ephemeral by design)."""

    id: str
    thread_id: str
    from_address: str
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    date: str
    snippet: str
    # RFC 2822 Message-ID — pass as ``in_reply_to`` on POST /emails to reply
    # inside this thread.
    message_id: str


class MailboxMessageDetail(MailboxMessageSummary):
    body_text: str | None
    body_html: str | None
    in_reply_to: str | None
    attachments: list[MailboxAttachment]


class MailboxMessageListResponse(BaseModel):
    items: list[MailboxMessageSummary]
    next_page_token: str | None = None
```

Routes (append to `email_accounts.py`; extend its imports with `GmailAuthError`, the new schemas, and `Callable` usage from Task 4):

```python
def _require_active(account: EmailAccount) -> None:
    if account.status != "active":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"email account {account.email_address!r} is "
                f"{account.status}; reconnect via POST "
                f"/email-accounts/{account.id}/reconnect"
            ),
        )


async def _flag_reauth(db: AsyncSession, account: EmailAccount) -> HTTPException:
    account.status = "reauth_required"
    await db.commit()
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail=(
            f"Google rejected the stored credentials for "
            f"{account.email_address!r}; reconnect via POST "
            f"/email-accounts/{account.id}/reconnect"
        ),
    )


@router.get("/{account_id}/messages", response_model=MailboxMessageListResponse)
async def list_mailbox_messages(
    account_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    builder: Annotated[
        Callable[[EmailAccount], GmailClient], Depends(get_gmail_client_builder)
    ],
    q: str | None = Query(default=None, max_length=1000),
    max_results: int = Query(default=25, ge=1, le=100),
    page_token: str | None = Query(default=None),
) -> MailboxMessageListResponse:
    """Live Gmail search/list — proxied, never persisted (spec §4)."""
    account = await require_account(db, principal.organization_id, account_id)
    _require_active(account)
    try:
        items, next_token = await builder(account).list_messages(
            q=q, max_results=max_results, page_token=page_token
        )
    except GmailAuthError:
        raise await _flag_reauth(db, account) from None
    return MailboxMessageListResponse(
        items=[MailboxMessageSummary.model_validate(i) for i in items],
        next_page_token=next_token,
    )


@router.get(
    "/{account_id}/messages/{message_id}", response_model=MailboxMessageDetail
)
async def get_mailbox_message(
    account_id: UUID,
    message_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    builder: Annotated[
        Callable[[EmailAccount], GmailClient], Depends(get_gmail_client_builder)
    ],
) -> MailboxMessageDetail:
    account = await require_account(db, principal.organization_id, account_id)
    _require_active(account)
    try:
        msg = await builder(account).get_message(message_id)
    except GmailAuthError:
        raise await _flag_reauth(db, account) from None
    return MailboxMessageDetail.model_validate(msg)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && uv run pytest tests/test_mailbox_reads_api.py tests/test_email_accounts_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/email_accounts.py core/hailhq/core/schemas.py api/tests/test_mailbox_reads_api.py
git commit -m "feat(api): ephemeral live mailbox reads for connected accounts"
```

---

### Task 6: Gmail branch in `POST /emails` (sender resolution, threading, billing)

**Files:**

- Modify: `api/hailhq/api/routes/emails.py` (`_resolve_sender`, `create_email`)
- Modify: `core/hailhq/core/schemas.py` (`EmailCreate.in_reply_to`; `EmailSummary.email_account_id`)
- Test: `api/tests/test_emails_gmail_send.py`

**Interfaces:**

- Consumes: `EmailAccount` (Task 1), `GmailEmailProvider`/`GmailClient`/`GmailAuthError` (Task 3), `get_gmail_client_builder` (Task 4).
- Produces:
  - `_resolve_sender(db, organization_id, explicit_from) -> EmailDomain | EmailAccount` — an explicit `from` exactly matching an org-owned `email_accounts.email_address` wins **before** the domain lookup; the account must be `status='active'` (a `reauth_required`/`disabled` match → 409 with reconnect hint). The no-`from` default path is untouched (domains only).
  - `EmailCreate.in_reply_to: str | None` — RFC 2822 Message-ID; stored on the row; passed as `In-Reply-To` + `References` headers to the provider for BOTH providers (SES benefits too), thread resolution happens only inside `GmailEmailProvider`.
  - `EmailSummary` gains `email_account_id: UUID | None` (rides into `EmailResponse`).
  - Billing/usage: unchanged code path — the existing `_write_usage_event` fires for Gmail sends exactly as for SES (decision: same 0.2¢ rate).
  - Wire treatment (footer, AI disclosure, List-Unsubscribe headers) is identical for both providers — compliance applies to the message, not the transport.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_emails_gmail_send.py
"""POST /emails through a connected Gmail account."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from hailhq.api.main import app
from hailhq.api.routes.email_accounts import get_gmail_client_builder
from hailhq.core.models import Email, UsageEvent
from hailhq.core.providers.email.gmail import GmailAuthError

from tests.test_email_accounts_api import _insert_account


class FakeGmail:
    """Stands in for GmailClient — captures what the provider sends."""

    def __init__(self, *, fail_auth: bool = False) -> None:
        self.fail_auth = fail_auth
        self.sent: list[dict] = []

    async def find_thread_id(self, rfc822_message_id: str) -> str | None:
        return "t42" if rfc822_message_id == "<orig@mail.example>" else None

    async def send_message(self, *, raw: bytes, thread_id=None):
        if self.fail_auth:
            raise GmailAuthError(401, "revoked")
        self.sent.append({"raw": raw, "thread_id": thread_id})
        return "gm-1", thread_id or "t-new"


@pytest.fixture()
def fake_gmail():
    fake = FakeGmail()
    app.dependency_overrides[get_gmail_client_builder] = lambda: (lambda a: fake)
    yield fake
    app.dependency_overrides.pop(get_gmail_client_builder, None)


def _send_body(**over):
    body = {
        "from": "alice@gmail.com",
        "to": ["bob@example.com"],
        "subject": "hi",
        "body_text": "hello",
        "recipient_consent": True,
    }
    body.update(over)
    return body


async def test_send_via_connected_account(
    client, org_and_key, async_session, fake_gmail
):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    resp = await client.post(
        "/emails", json=_send_body(), headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "sent"
    assert data["email_account_id"] == str(acct.id)
    assert data["email_domain_id"] is None
    row = (
        await async_session.execute(select(Email).where(Email.id == data["id"]))
    ).scalar_one()
    assert row.provider == "gmail"
    assert row.provider_message_id == "gm-1"
    assert row.provider_thread_id == "t-new"
    assert len(fake_gmail.sent) == 1


async def test_gmail_send_writes_usage_event(
    client, org_and_key, async_session, fake_gmail
):
    org_id, _, plain = org_and_key
    await _insert_account(async_session, org_id)
    resp = await client.post(
        "/emails", json=_send_body(), headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 201
    events = (
        await async_session.execute(
            select(UsageEvent).where(
                UsageEvent.organization_id == org_id, UsageEvent.channel == "email"
            )
        )
    ).scalars().all()
    assert len(events) == 1  # billed at the standard email rate


async def test_reply_threads_into_gmail(client, org_and_key, async_session, fake_gmail):
    org_id, _, plain = org_and_key
    await _insert_account(async_session, org_id)
    resp = await client.post(
        "/emails",
        json=_send_body(in_reply_to="<orig@mail.example>", subject="Re: hi"),
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201
    assert fake_gmail.sent[0]["thread_id"] == "t42"
    assert resp.json()["provider_thread_id"] == "t42"
    raw = fake_gmail.sent[0]["raw"].decode()
    assert "In-Reply-To: <orig@mail.example>" in raw


async def test_reauth_required_account_409(client, org_and_key, async_session, fake_gmail):
    org_id, _, plain = org_and_key
    await _insert_account(async_session, org_id, status="reauth_required")
    resp = await client.post(
        "/emails", json=_send_body(), headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 409
    assert "reconnect" in resp.json()["detail"]


async def test_auth_failure_marks_account_and_email(
    client, org_and_key, async_session
):
    org_id, _, plain = org_and_key
    acct = await _insert_account(async_session, org_id)
    fake = FakeGmail(fail_auth=True)
    app.dependency_overrides[get_gmail_client_builder] = lambda: (lambda a: fake)
    try:
        resp = await client.post(
            "/emails", json=_send_body(), headers={"Authorization": f"Bearer {plain}"}
        )
    finally:
        app.dependency_overrides.pop(get_gmail_client_builder, None)
    assert resp.status_code == 409
    await async_session.refresh(acct)
    assert acct.status == "reauth_required"
    row = (
        await async_session.execute(
            select(Email).where(Email.organization_id == org_id)
        )
    ).scalar_one()
    assert row.status == "failed"


async def test_ses_default_path_untouched(client, org_and_key, async_session):
    """No connected account matches → the existing domain/hail-mail flow runs."""
    _, _, plain = org_and_key
    body = _send_body()
    del body["from"]  # default path never considers email_accounts
    resp = await client.post(
        "/emails", json=body, headers={"Authorization": f"Bearer {plain}"}
    )
    # conftest's email provider mock + hail-mail settings handle the rest;
    # assert only that resolution did not error out on the new branch.
    assert resp.status_code in (201, 422, 503)
```

Note for the implementer: check `UsageEvent` import — the model name is in `hailhq.core.models` (used by `write_usage_event`); if the attribute for channel differs, mirror `api/tests/test_usage_helper.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && uv run pytest tests/test_emails_gmail_send.py -v`
Expected: FAIL — 422 "not a verified sender" (resolution doesn't know accounts yet), missing `in_reply_to`/`email_account_id` fields

- [ ] **Step 3: Schema changes**

In `EmailCreate` (schemas.py:531), after `reply_to`:

```python
    # RFC 2822 Message-ID this send replies to. For connected-account sends
    # the Gmail thread is resolved from it so the reply lands in-thread.
    in_reply_to: str | None = Field(default=None, max_length=998)
```

In `EmailSummary` (schemas.py:~670), after `email_domain_id`:

```python
    email_account_id: UUID | None = None
    provider_thread_id: str | None = None
```

- [ ] **Step 4: Wire `_resolve_sender` + `create_email`**

In `emails.py` — imports: add `EmailAccount` to the `hailhq.core.models` import; add

```python
from hailhq.api.routes.email_accounts import get_gmail_client_builder
from hailhq.core.providers.email.gmail import GmailAuthError, GmailClient, GmailEmailProvider
```

At the top of `_resolve_sender` (return type becomes `EmailDomain | EmailAccount`), insert before the domain lookup inside the `if explicit_from is not None:` branch:

```python
        account = (
            await db.execute(
                select(EmailAccount).where(
                    EmailAccount.organization_id == organization_id,
                    EmailAccount.email_address == explicit_from,
                )
            )
        ).scalar_one_or_none()
        if account is not None:
            if account.status != "active":
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail=(
                        f"connected account {explicit_from!r} is "
                        f"{account.status}; reconnect via POST "
                        f"/email-accounts/{account.id}/reconnect"
                    ),
                )
            return account
```

In `create_email`:

1. Add the builder dependency parameter:

```python
    gmail_builder: Annotated[
        Callable[[EmailAccount], GmailClient], Depends(get_gmail_client_builder)
    ],
```

(`from typing import Callable` — extend the existing `typing` import.)

2. After `sd = await _resolve_sender(...)`:

```python
    is_account = isinstance(sd, EmailAccount)
    from_address = sd.email_address if is_account else _from_address_for(sd, body.from_)
```

3. Email row construction — replace the two id kwargs and provider:

```python
        email_domain_id=None if is_account else sd.id,
        email_account_id=sd.id if is_account else None,
        in_reply_to=body.in_reply_to,
        ...
        provider="gmail" if is_account else "ses",
```

4. Headers: extend the existing send headers dict:

```python
            headers={
                "List-Unsubscribe": f"<{unsubscribe_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                **(
                    {"In-Reply-To": body.in_reply_to, "References": body.in_reply_to}
                    if body.in_reply_to
                    else {}
                ),
            },
```

5. Provider dispatch — before the `try:` around `email_provider.send_email`:

```python
    send_provider: EmailSender = (
        GmailEmailProvider(gmail_builder(sd)) if is_account else email_provider
    )
```

and call `send_provider.send_email(...)` instead of `email_provider.send_email(...)`. (Import `EmailSender` from `hailhq.core.providers.email`.)

6. Auth-failure special case — inside the existing `except Exception as exc:` block, FIRST branch on Gmail auth:

```python
        if isinstance(exc, GmailAuthError) and is_account:
            sd.status = "reauth_required"
            # fall through to the shared failed-row bookkeeping below, but
            # answer 409 (actionable: reconnect) instead of 502.
```

Concretely: keep the shared "mark row failed + audit log" code, then raise 409 with detail `f"Google rejected the stored credentials; reconnect via POST /email-accounts/{sd.id}/reconnect"` when `isinstance(exc, GmailAuthError) and is_account`, else the existing 502. Make sure `sd.status` mutation commits with the same `db.commit()` that persists the failed status.

7. Success bookkeeping — extend the `update(Email).values(...)` after a successful send:

```python
            provider_thread_id=result.provider_thread_id,
```

(`None` for SES — harmless.) Leave `record_sent_event` and `_write_usage_event` exactly as they are: Gmail sends get a `sent` event and bill one email unit like SES sends.

- [ ] **Step 5: Run the full email test surface**

Run: `cd api && uv run pytest tests/test_emails_gmail_send.py tests/test_emails_api.py tests/test_emails_inbound_reads.py tests/test_email_domains_api.py -v`
Expected: PASS — new suite green, zero regressions in existing email suites

- [ ] **Step 6: Commit**

```bash
git add api/hailhq/api/routes/emails.py core/hailhq/core/schemas.py api/tests/test_emails_gmail_send.py
git commit -m "feat(api): send emails through connected Gmail accounts"
```

---

### Task 7: MCP tools — `list_email_accounts`, `search_mailbox`, `read_mailbox_message`, `send_email.in_reply_to`

**Files:**

- Modify: `mcp/hailhq/mcp/hail_client.py` (3 new methods + `in_reply_to` on `send_email`)
- Modify: `mcp/hailhq/mcp/tools.py` (3 helpers + 3 `@mcp_app.tool` registrations in `register_tools`; `in_reply_to` passthrough on the send_email tool)
- Test: `mcp/tests/test_mailbox_tools.py` (copy the fixture/assertion style of `mcp/tests/test_tools.py`)

**Interfaces:**

- Consumes: Task 4/5 REST endpoints.
- Produces `HailClient` methods (same `_decode`/`HailAPIError` conventions as `list_emails`):
  - `async list_email_accounts(self) -> dict[str, Any]` — `GET /email-accounts`
  - `async search_mailbox(self, account_id: str, q: str | None = None, max_results: int = 25, page_token: str | None = None) -> dict[str, Any]` — `GET /email-accounts/{account_id}/messages`
  - `async read_mailbox_message(self, account_id: str, message_id: str) -> dict[str, Any]` — `GET /email-accounts/{account_id}/messages/{message_id}`
  - `send_email(..., in_reply_to: str | None = None)` → body key `in_reply_to`
- Produces MCP tools (names are provider-neutral for Outlook later): `list_email_accounts`, `search_mailbox`, `read_mailbox_message`; `send_email` gains an `in_reply_to` parameter.

- [ ] **Step 1: Write the failing tests** — follow the existing mcp test style exactly (they stub `HailClient` HTTP with `httpx.MockTransport`). Cover: (a) `search_mailbox` hits `/email-accounts/{id}/messages` with `q` and returns the payload; (b) `read_mailbox_message` hits the detail path; (c) `list_email_accounts` hits `/email-accounts`; (d) `send_email` includes `"in_reply_to"` in the POST body when provided. Complete test code should mirror an existing test in `mcp/tests/` for, e.g., `list_emails` — same fixtures, same assertion shape, new paths.

- [ ] **Step 2: Run to verify failure**

Run: `cd mcp && uv run pytest tests/test_mailbox_tools.py -v`
Expected: FAIL — `AttributeError: 'HailClient' object has no attribute 'search_mailbox'`

- [ ] **Step 3: Implement `HailClient` methods** (copy the `list_emails` request/`_decode` pattern):

```python
    async def list_email_accounts(self) -> dict[str, Any]:
        resp = await self._client.get("/email-accounts")
        return _decode(resp)

    async def search_mailbox(
        self,
        account_id: str,
        q: str | None = None,
        max_results: int = 25,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"max_results": max_results}
        if q:
            params["q"] = q
        if page_token:
            params["page_token"] = page_token
        resp = await self._client.get(
            f"/email-accounts/{account_id}/messages", params=params
        )
        return _decode(resp)

    async def read_mailbox_message(
        self, account_id: str, message_id: str
    ) -> dict[str, Any]:
        resp = await self._client.get(
            f"/email-accounts/{account_id}/messages/{message_id}"
        )
        return _decode(resp)
```

(Adapt the exact request call to whatever `list_emails` uses — same error handling, same auth injection. Add `in_reply_to` to `send_email`'s signature and JSON body, key `"in_reply_to"`, omitted when `None`.)

- [ ] **Step 4: Implement tools.py helpers + registrations** — three module-level helpers following the `list_emails` helper pattern (try/except → `_format_api_error`), then inside `register_tools`:

```python
    @mcp_app.tool(name="list_email_accounts")
    async def list_email_accounts_tool(ctx: Context) -> dict[str, Any]:
        """List the org's connected mailboxes (Gmail accounts).

        Each item's ``email_address`` can be used as ``from`` in send_email;
        each ``id`` is the account_id for search_mailbox /
        read_mailbox_message. Accounts with status='reauth_required' need
        the user to reconnect before use.
        """
        async with await _client_for(ctx) as client:
            return await tools_list_email_accounts(client=client)

    @mcp_app.tool(name="search_mailbox")
    async def search_mailbox_tool(
        ctx: Context,
        account_id: str,
        q: str | None = None,
        max_results: int = 25,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Live-search a connected mailbox (nothing is stored in Hail).

        ``q`` uses Gmail query syntax, e.g. 'in:inbox newer_than:2d' for
        "check my last emails", or 'from:bob@example.com' for a sender.
        Returns message summaries; fetch a body with read_mailbox_message.
        """
        async with await _client_for(ctx) as client:
            return await tools_search_mailbox(
                client=client,
                account_id=account_id,
                q=q,
                max_results=max_results,
                page_token=page_token,
            )

    @mcp_app.tool(name="read_mailbox_message")
    async def read_mailbox_message_tool(
        ctx: Context, account_id: str, message_id: str
    ) -> dict[str, Any]:
        """Read one mailbox message (live from Gmail, not stored).

        The response's ``message_id`` (RFC 2822) can be passed as
        ``in_reply_to`` to send_email to reply inside the same thread.
        """
        async with await _client_for(ctx) as client:
            return await tools_read_mailbox_message(
                client=client, account_id=account_id, message_id=message_id
            )
```

(Match `_client_for` usage to how the existing tools acquire a client — copy `list_emails_tool` verbatim as the template. Add `in_reply_to: str | None = None` to `send_email_tool` and thread it through the helper.)

- [ ] **Step 5: Run tests**

Run: `cd mcp && uv run pytest tests/ -v`
Expected: PASS (new tests + no regressions)

- [ ] **Step 6: Commit**

```bash
git add mcp/hailhq/mcp/hail_client.py mcp/hailhq/mcp/tools.py mcp/tests/test_mailbox_tools.py
git commit -m "feat(mcp): mailbox tools for connected Gmail accounts"
```

---

### Task 8: OpenAPI regen, docs, changelog, full-suite verification

**Files:**

- Modify: `openapi/openapi.yaml` (regenerated)
- Create: `docs/setup/gmail-accounts.md`
- Modify: `CHANGELOG.md` (Unreleased/next section, match existing entry style)
- Verify: `.env.example` already carries the Task-2 vars

**Interfaces:** none produced — release chores.

- [ ] **Step 1: Regenerate openapi.yaml** (API must be running locally)

```bash
cd api && uv run uvicorn hailhq.api.main:app --port 8080 &
sleep 3
curl -s http://localhost:8080/openapi.json \
  | python -c "import json, sys, yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" \
  > ../openapi/openapi.yaml
kill %1
```

Verify: `grep -c "email-accounts" ../openapi/openapi.yaml` returns > 0.

- [ ] **Step 2: Write `docs/setup/gmail-accounts.md`** (agent-first: lead with runnable example)

```markdown
# Connect a Gmail account

Send as your real address and read your inbox live — no DNS setup.

    # 1. Get a consent URL and open it in a browser
    curl -X POST https://api.hail.so/email-accounts/connect \
      -H "Authorization: Bearer $HAIL_API_KEY"
    # → {"authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?..."}

    # 2. After consenting, send as yourself
    curl -X POST https://api.hail.so/emails \
      -H "Authorization: Bearer $HAIL_API_KEY" -H "Content-Type: application/json" \
      -d '{"from": "you@gmail.com", "to": ["bob@example.com"],
           "subject": "hi", "body_text": "hello", "recipient_consent": true}'

    # 3. Check your inbox (live read — Hail stores nothing)
    curl "https://api.hail.so/email-accounts/{id}/messages?q=in:inbox" \
      -H "Authorization: Bearer $HAIL_API_KEY"

Replies: pass a message's `message_id` as `in_reply_to` on `POST /emails`
to answer inside the same Gmail thread.

MCP tools: `list_email_accounts`, `search_mailbox`, `read_mailbox_message`.

Notes:

- Sends bill at the standard email rate and appear in `GET /emails`;
  received mail is never stored (read it live).
- No delivery/bounce events for Gmail sends — bounces arrive as
  mailer-daemon replies in the thread.
- Self-hosting: create a Google Cloud OAuth client (Web application type,
  redirect URI `<HAIL_API_URL>/email-accounts/oauth/callback`), then set
  `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`. A Workspace
  "internal" app needs no Google review.
- Endpoint reference: the OpenAPI spec (`openapi/openapi.yaml`) under
  `/email-accounts`.
```

- [ ] **Step 3: CHANGELOG entry** — one bullet under the unreleased section: `- Connect Gmail accounts: send as your own address (POST /emails with a connected `from`), live inbox reads via /email-accounts/{id}/messages, MCP tools list_email_accounts / search_mailbox / read_mailbox_message.`

- [ ] **Step 4: Full verification**

```bash
cd api && uv run pytest -q && uv run ruff check . && uv run black --check .
cd ../mcp && uv run pytest -q
cd ../core && uv run ruff check . && uv run black --check .
```

Expected: all green. Also confirm `.env.example` contains `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `HAIL_EMAIL_CONNECT_SUCCESS_URL`.

- [ ] **Step 5: Commit**

```bash
git add openapi/openapi.yaml docs/setup/gmail-accounts.md CHANGELOG.md
git commit -m "docs(api): openapi + setup docs for Gmail account connection"
```
