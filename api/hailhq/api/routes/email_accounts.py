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

import html
import logging
from typing import Annotated, Callable
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.audit import write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.pagination import fetch_cursor_page
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.models import Email, EmailAccount
from hailhq.core.providers.email.gmail import GmailApiError, GmailAuthError, GmailClient
from hailhq.core.providers.email.gmail_oauth import (
    GmailOAuthError,
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
    MailboxMessageDetail,
    MailboxMessageListResponse,
    MailboxMessageSummary,
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
    """Build a ``GmailClient`` from an account row. Overridable in tests.

    Cipher construction is deferred to ``build()`` rather than done eagerly
    here: this dependency is also injected into ``POST /emails``, which
    handles both SES and Gmail sends, so resolving it must not require
    ``HAIL_PROVIDER_SECRET_KEY``/OAuth config on every plain SES send —
    only on sends that actually go through a connected account.
    """

    def build(account: EmailAccount) -> GmailClient:
        cipher = _cipher()
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
        return HTMLResponse(
            f"<h1>Connection cancelled</h1><p>{html.escape(error)}</p>", 400
        )
    if not code or not state:
        return HTMLResponse("<h1>Missing code or state</h1>", 400)
    try:
        organization_id, account_id = verify_state(state)
    except InvalidStateToken:
        return HTMLResponse("<h1>Invalid or expired state token</h1>", 400)

    try:
        grant = await exchange_code(code=code, redirect_uri=_redirect_uri())
    except GmailOAuthError:
        return HTMLResponse(
            "<h1>"
            + html.escape(
                "Connection failed — the authorization may have expired. "
                "Retry the connect link."
            )
            + "</h1>",
            400,
        )
    if not grant.refresh_token:
        return HTMLResponse(
            "<h1>Google returned no refresh token</h1>"
            "<p>Remove Hail's access at myaccount.google.com/permissions "
            "and connect again.</p>",
            400,
        )
    try:
        info = await fetch_userinfo(access_token=grant.access_token)
    except GmailOAuthError:
        return HTMLResponse(
            "<h1>"
            + html.escape(
                "Connection failed — the authorization may have expired. "
                "Retry the connect link."
            )
            + "</h1>",
            400,
        )
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
                f"<p>This connection belongs to {html.escape(account.email_address)}; "
                f"you authorized {html.escape(info.email)}. Retry with the right "
                "account.</p>",
                409,
            )
        account.encrypted_refresh_token = encrypted
        account.email_address = info.email
        account.scopes = scopes or account.scopes
        account.status = "active"
        affected = account
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
                    f"<p>{html.escape(info.email)} is connected to a different "
                    "organization.</p>",
                    409,
                )
            # Same org re-connecting the same mailbox — refresh in place.
            existing.encrypted_refresh_token = encrypted
            existing.provider_user_id = info.sub
            existing.scopes = scopes or existing.scopes
            existing.status = "active"
            affected = existing
        else:
            affected = EmailAccount(
                organization_id=organization_id,
                provider="gmail",
                email_address=info.email,
                display_name=info.name,
                provider_user_id=info.sub,
                scopes=scopes,
                encrypted_refresh_token=encrypted,
                status="active",
            )
            db.add(affected)
            # Populate the server-generated id before commit expires the row.
            await db.flush()
    affected_id = affected.id
    await db.commit()
    await write_audit_log(
        organization_id=organization_id,
        api_key_id=None,
        action="email_account.connected",
        resource_type="email_account",
        resource_id=affected_id,
        payload={"email_address": info.email},
    )
    if settings.hail_email_connect_success_url:
        return RedirectResponse(settings.hail_email_connect_success_url, 303)
    return HTMLResponse(_SUCCESS_HTML.format(address=html.escape(info.email)))


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
    if account.status == "reauth_required" and body.status == "active":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"email account {account.email_address!r} needs reauthorization; "
                f"reconnect via POST /email-accounts/{account.id}/reconnect "
                "instead of setting status directly"
            ),
        )
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
    referencing = (
        await db.execute(
            select(func.count(Email.id)).where(Email.email_account_id == account_id)
        )
    ).scalar_one()
    if referencing > 0:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                "email account has sent emails referencing it; disable it "
                'instead (PATCH {"status": "disabled"})'
            ),
        )
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
                'instead (PATCH {"status": "disabled"})'
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


def _gmail_api_error_to_http(exc: GmailApiError) -> HTTPException:
    if exc.status == 429:
        return HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Gmail rate limit exceeded: {exc.detail}",
        )
    if 400 <= exc.status < 500:
        return HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST, detail=exc.detail
        )
    return HTTPException(
        status_code=http_status.HTTP_502_BAD_GATEWAY, detail=exc.detail
    )


_CORRUPT_CREDENTIALS_DETAIL = "stored credentials are corrupted; reconnect"


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
    except GmailApiError as exc:
        raise _gmail_api_error_to_http(exc) from None
    except InvalidToken:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=_CORRUPT_CREDENTIALS_DETAIL,
        ) from None
    return MailboxMessageListResponse(
        items=[MailboxMessageSummary.model_validate(i) for i in items],
        next_page_token=next_token,
    )


@router.get("/{account_id}/messages/{message_id}", response_model=MailboxMessageDetail)
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
    except GmailApiError as exc:
        raise _gmail_api_error_to_http(exc) from None
    except InvalidToken:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=_CORRUPT_CREDENTIALS_DETAIL,
        ) from None
    return MailboxMessageDetail.model_validate(msg)
