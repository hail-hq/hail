"""CRUD for org-wide webhook subscriptions and per-attempt delivery audit.

POST     /webhooks                              create + return plaintext secret once
POST     /webhooks/test                         send one test event, return the answer
GET      /webhooks                              list paginated
GET      /webhooks/event-types                  supported names and display metadata
GET      /webhooks/{id}                         detail (no secret)
PATCH    /webhooks/{id}                         update target_url / event_types / status
DELETE   /webhooks/{id}
POST     /webhooks/{id}/rotate-secret           rotate, return new plaintext once
GET      /webhooks/{id}/deliveries              audit log, paginated
POST     /webhooks/{id}/deliveries/{did}/redeliver

The plaintext secret is returned **only** at create + rotate-secret;
subsequent GETs omit it. At rest it's Fernet-encrypted (see
``hailhq.core.secret_cipher``); the delivery worker decrypts the
ciphertext off the row to sign each outbound POST.
"""

from __future__ import annotations

import asyncio
import secrets as _secrets
import time
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from hailhq.api.audit import actor_of, write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.http_post import (
    PrivateNetworkBlockedError,
    httpx_post,
    validate_webhook_target,
)
from hailhq.core.models import WebhookDelivery, WebhookSubscription
from hailhq.core.schemas import (
    WebhookDeliveryListResponse,
    WebhookDeliveryResponse,
    WebhookEventCatalogResponse,
    WebhookEventDefinition,
    WebhookSubscriptionCreate,
    WebhookSubscriptionListResponse,
    WebhookSubscriptionPatch,
    WebhookSubscriptionResponse,
    WebhookTestRequest,
    WebhookTestResponse,
)
from hailhq.core.secret_cipher import SecretCipher
from hailhq.core.urls import same_request_url
from hailhq.core.webhook_events import WebhookEventType
from hailhq.core.webhook_worker import RESPONSE_BODY_CAP, redact_credentials
from hailhq.core.webhooks import build_event_payload, sign_payload
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/webhooks", tags=["webhooks"], responses=GENERAL_RATE_LIMITED_RESPONSES
)

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _new_secret() -> str:
    return "whs_" + _secrets.token_urlsafe(24)


def _authorization_value(value: SecretStr | None) -> str | None:
    if value is None:
        return None
    plain = value.get_secret_value()
    if len(plain) > 2000 or any(not " " <= c <= "~" for c in plain):
        raise unprocessable(
            "Authorization header must use printable ASCII and at most 2000 characters.",
            loc=["body", "authorization"],
        )
    if plain and not plain.strip():
        # Only an empty string removes a saved header (see the PATCH docs).
        raise unprocessable(
            'Authorization header cannot be only spaces. Send "" to remove it.',
            loc=["body", "authorization"],
        )
    plain = plain.strip()
    if plain.lower() in {"basic", "bearer"}:
        # A bare scheme sends no credential, and the worker would then redact
        # that word from every receiver reply.
        raise unprocessable(
            "Authorization header needs a credential after the scheme.",
            loc=["body", "authorization"],
        )
    return plain


def _to_response(
    sub: WebhookSubscription, *, secret: str | None = None
) -> WebhookSubscriptionResponse:
    resp = WebhookSubscriptionResponse.model_validate(sub)
    if secret is not None:
        resp = resp.model_copy(update={"secret": secret})
    return resp


async def _load_owned(
    db: AsyncSession, sub_id: UUID, org_id: UUID, *, lock: bool = False
) -> WebhookSubscription:
    stmt = select(WebhookSubscription).where(
        WebhookSubscription.id == sub_id,
        WebhookSubscription.organization_id == org_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    sub = (await db.execute(stmt)).scalar_one_or_none()
    if sub is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="subscription not found",
        )
    return sub


@router.post(
    "",
    response_model=WebhookSubscriptionResponse,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_subscription(
    body: WebhookSubscriptionCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookSubscriptionResponse:
    """Create a webhook subscription for one or more event types.

    The response includes the plaintext signing secret — this is the only
    time it is ever returned; store it now. Every later GET omits it. Use
    POST /v1/webhooks/{sub_id}/rotate-secret to get a new plaintext secret if
    it is lost or compromised.
    """
    authorization = _authorization_value(body.authorization)
    try:
        # DNS lookup: keep it off the event loop.
        await asyncio.to_thread(
            validate_webhook_target,
            body.target_url,
            allow_private_networks=settings.hail_webhook_allow_private_networks,
        )
    except ValueError as exc:
        raise unprocessable(str(exc), loc=["body", "target_url"]) from exc
    secret = _new_secret()
    cipher = SecretCipher(settings.hail_webhook_secret_key)
    # The INSERT's own time, not the column default now(): now() is the
    # transaction start, which usually comes before the target URL check:
    # auth and telemetry statements on the request session often leave the
    # transaction open. call.summarized compares updated_at with call end
    # times (hailhq.core.call_analysis). statement_timestamp() is one value
    # for the whole statement, so both columns get the same time.
    stamp = func.statement_timestamp()
    sub = WebhookSubscription(
        organization_id=principal.organization_id,
        target_url=body.target_url,
        secret_encrypted=cipher.encrypt(secret),
        authorization_encrypted=(
            cipher.encrypt(authorization) if authorization else None
        ),
        event_types=list(body.event_types),
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="webhook.create",
        resource_type="webhook_subscription",
        resource_id=sub.id,
        payload={
            "target_url": sub.target_url,
            "has_authorization": sub.has_authorization,
        },
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return _to_response(sub, secret=secret)


@router.get(
    "",
    response_model=WebhookSubscriptionListResponse,
)
async def list_subscriptions(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> WebhookSubscriptionListResponse:
    """List webhook subscriptions for the caller's organization.

    Cursor-paginated, newest first. The signing secret is never included —
    only POST /v1/webhooks and POST /v1/webhooks/{sub_id}/rotate-secret return it.
    """
    stmt = select(WebhookSubscription).where(
        WebhookSubscription.organization_id == principal.organization_id
    )
    rows, next_cursor = await fetch_cursor_page(
        db,
        stmt,
        WebhookSubscription.created_at,
        WebhookSubscription.id,
        cursor=cursor,
        limit=limit,
        newest_first=True,
    )
    return WebhookSubscriptionListResponse(
        items=[_to_response(s) for s in rows], next_cursor=next_cursor
    )


@router.get("/event-types", response_model=WebhookEventCatalogResponse)
async def list_event_types() -> WebhookEventCatalogResponse:
    """List event names, labels, and channels accepted by subscriptions.

    This public catalog contains no organization data. It uses the same
    registry as create and patch validation.
    """
    return WebhookEventCatalogResponse(
        items=[
            WebhookEventDefinition(type=event, label=event.label, channel=event.channel)
            for event in WebhookEventType
        ]
    )


_TEST_EVENT = "webhook.test"
_TEST_TIMEOUT_SECONDS = 10.0


def _test_error(exc: Exception) -> str:
    """A fixed category, never the exception text: it can name internal hosts."""
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return f"No answer within {_TEST_TIMEOUT_SECONDS:.0f} seconds."
    if isinstance(exc, PrivateNetworkBlockedError):
        return "target_url is not allowed: it does not resolve to a public address."
    if isinstance(exc, httpx.ConnectError):
        if "CERTIFICATE" in str(exc).upper() or "SSL" in str(exc).upper():
            return "The TLS certificate check failed."
        return "Could not connect to your endpoint."
    if isinstance(exc, httpx.RemoteProtocolError):
        return "Your endpoint closed the connection without a valid answer."
    return "Could not reach your endpoint."


@router.post("/test", response_model=WebhookTestResponse)
async def test_subscription(
    body: WebhookTestRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookTestResponse:
    """Send one ``webhook.test`` event to a URL and return the receiver's answer.

    Use it before saving a subscription or a change to one. It sends the
    same headers as a real delivery. Nothing is stored or retried, and a
    failed test does not count toward auto-disable.

    With ``subscription_id`` the subscription's secret signs the test, and an
    omitted ``authorization`` sends the saved header. Without it the test is
    not signed: a new subscription gets its secret when it is created.
    """
    authorization = _authorization_value(body.authorization)
    delivery_id = uuid4()
    headers = {
        "Content-Type": "application/json",
        "X-Hail-Event": _TEST_EVENT,
        "X-Hail-Delivery": str(delivery_id),
    }
    secret: str | None = None
    if body.subscription_id is not None:
        sub = await _load_owned(db, body.subscription_id, principal.organization_id)
        cipher = SecretCipher(settings.hail_webhook_secret_key)
        secret = cipher.decrypt(sub.secret_encrypted)
        headers["X-Hail-Subscription"] = str(sub.id)
        if authorization is None and sub.authorization_encrypted is not None:
            # A saved credential goes only to the URL it was saved for.
            if not same_request_url(body.target_url, sub.target_url):
                raise unprocessable(
                    "Enter the Authorization header again or remove it when changing the target URL.",
                    loc=["body", "authorization"],
                )
            authorization = cipher.decrypt(sub.authorization_encrypted)
    # The auth lookup also opened a transaction. End it so neither the DNS
    # check nor a slow receiver holds a pooled connection.
    await db.commit()

    try:
        # DNS lookup: keep it off the event loop.
        await asyncio.to_thread(
            validate_webhook_target,
            body.target_url,
            allow_private_networks=settings.hail_webhook_allow_private_networks,
        )
    except ValueError as exc:
        raise unprocessable(str(exc), loc=["body", "target_url"]) from exc

    payload = build_event_payload(
        delivery_id=delivery_id,
        event_type=_TEST_EVENT,
        organization_id=principal.organization_id,
        data={"message": "Test event from Hail. No action needed."},
    )
    if secret is not None:
        headers["X-Hail-Signature"] = sign_payload(payload, secret)
    if authorization:
        headers["Authorization"] = authorization

    started = time.monotonic()
    status: int | None = None
    text: str | None = None
    error: str | None = None
    try:
        # One deadline for the whole request: httpx times each phase alone.
        status, text = await asyncio.wait_for(
            httpx_post(
                body.target_url,
                payload,
                headers,
                allow_private_networks=settings.hail_webhook_allow_private_networks,
                timeout_seconds=_TEST_TIMEOUT_SECONDS,
                # UTF-8 uses up to 4 bytes a character.
                max_body_bytes=RESPONSE_BODY_CAP * 4,
            ),
            _TEST_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # the answer is the receiver's problem to show
        error = _test_error(exc)
    duration_ms = round((time.monotonic() - started) * 1000)
    text = redact_credentials(text, authorization or "")
    return WebhookTestResponse(
        ok=status is not None and 200 <= status < 300,
        status_code=status,
        duration_ms=duration_ms,
        response_body=text[:RESPONSE_BODY_CAP] if text else None,
        error=redact_credentials(error, authorization or ""),
        signed=secret is not None,
    )


@router.get(
    "/{sub_id}",
    response_model=WebhookSubscriptionResponse,
)
async def get_subscription(
    sub_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookSubscriptionResponse:
    """Fetch one webhook subscription by id. The signing secret is omitted."""
    sub = await _load_owned(db, sub_id, principal.organization_id)
    return _to_response(sub)


@router.patch(
    "/{sub_id}",
    response_model=WebhookSubscriptionResponse,
)
async def patch_subscription(
    sub_id: UUID,
    body: WebhookSubscriptionPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookSubscriptionResponse:
    """Update a webhook subscription's target_url, event_types, and/or status.

    Only fields present in the request body are changed. Setting
    status="active" resets the consecutive-failure counter, so a
    subscription that was auto-disabled after 50 straight delivery
    failures does not immediately re-disable itself.
    """
    authorization = _authorization_value(body.authorization)
    if body.target_url is not None:
        try:
            # DNS lookup: keep it off the event loop, and run it before the
            # row lock so a slow resolver does not block delivery updates.
            await asyncio.to_thread(
                validate_webhook_target,
                body.target_url,
                allow_private_networks=settings.hail_webhook_allow_private_networks,
            )
        except ValueError as exc:
            raise unprocessable(str(exc), loc=["body", "target_url"]) from exc
    sub = await _load_owned(db, sub_id, principal.organization_id, lock=True)
    updates: dict = {}
    if body.target_url is not None:
        updates["target_url"] = body.target_url
        if (
            sub.authorization_encrypted is not None
            and authorization is None
            and not same_request_url(body.target_url, sub.target_url)
        ):
            raise unprocessable(
                "Enter the Authorization header again or remove it when changing the target URL.",
                loc=["body", "authorization"],
            )
    if authorization is not None:
        updates["authorization_encrypted"] = (
            SecretCipher(settings.hail_webhook_secret_key).encrypt(authorization)
            if authorization
            else None
        )
    if body.event_types is not None:
        updates["event_types"] = list(body.event_types)
    if body.status is not None:
        updates["status"] = body.status
        # Re-enabling a subscription resets the failure counter so the
        # 50-strike auto-disable doesn't immediately fire again.
        if body.status == "active":
            updates["consecutive_failures"] = 0
    if updates:
        # The UPDATE's own time on the DB clock, as on create: call.summarized
        # compares it with call end times (hailhq.core.call_analysis).
        updates["updated_at"] = func.statement_timestamp()
        await db.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == sub.id)
            .values(**updates)
        )
        await db.commit()
        await db.refresh(sub)
        actor_user_id, actor_kind = actor_of(principal)
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="webhook.patch",
            resource_type="webhook_subscription",
            resource_id=sub.id,
            payload={
                "target_url": sub.target_url,
                "status": sub.status,
                "has_authorization": sub.has_authorization,
            },
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
        )
    return _to_response(sub)


@router.delete(
    "/{sub_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
async def delete_subscription(
    sub_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Permanently remove a webhook subscription.

    Irreversible — no further events are delivered to it, and its
    delivery history is deleted with it.
    """
    sub = await _load_owned(db, sub_id, principal.organization_id)
    await db.delete(sub)
    await db.commit()
    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="webhook.delete",
        resource_type="webhook_subscription",
        resource_id=sub_id,
        payload={},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post(
    "/{sub_id}/rotate-secret",
    response_model=WebhookSubscriptionResponse,
)
async def rotate_secret(
    sub_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookSubscriptionResponse:
    """Generate a new signing secret and immediately invalidate the old one.

    The response includes the new plaintext secret — this is the only
    time it is returned; update your verification code with it right away,
    since the old secret stops validating new deliveries immediately.
    """
    sub = await _load_owned(db, sub_id, principal.organization_id)
    secret = _new_secret()
    cipher = SecretCipher(settings.hail_webhook_secret_key)
    await db.execute(
        update(WebhookSubscription)
        .where(WebhookSubscription.id == sub.id)
        .values(
            secret_encrypted=cipher.encrypt(secret),
            updated_at=func.statement_timestamp(),
        )
    )
    await db.commit()
    await db.refresh(sub)
    # Log the rotation only — NEVER the secret itself.
    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="webhook.rotate_secret",
        resource_type="webhook_subscription",
        resource_id=sub.id,
        payload={},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return _to_response(sub, secret=secret)


@router.get(
    "/{sub_id}/deliveries",
    response_model=WebhookDeliveryListResponse,
)
async def list_deliveries(
    sub_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> WebhookDeliveryListResponse:
    """List delivery attempts for one webhook subscription, newest first.

    Cursor-paginated. Each entry shows the attempt count, response status,
    and response body Hail recorded — use POST /v1/webhooks/{sub_id}/
    deliveries/{delivery_id}/redeliver to retry a failed one.
    """
    await _load_owned(db, sub_id, principal.organization_id)  # auth gate
    stmt = select(WebhookDelivery).where(WebhookDelivery.subscription_id == sub_id)
    rows, next_cursor = await fetch_cursor_page(
        db,
        stmt,
        WebhookDelivery.created_at,
        WebhookDelivery.id,
        cursor=cursor,
        limit=limit,
        newest_first=True,
    )
    return WebhookDeliveryListResponse(
        items=[WebhookDeliveryResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.post(
    "/{sub_id}/deliveries/{delivery_id}/redeliver",
    response_model=WebhookDeliveryResponse,
)
async def redeliver(
    sub_id: UUID,
    delivery_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookDeliveryResponse:
    """Retry one webhook delivery attempt.

    Resets it to pending with a fresh attempt counter — the delivery
    worker picks it up and re-sends the same event payload to target_url
    shortly after. Useful after fixing an endpoint that was returning
    errors.
    """
    await _load_owned(db, sub_id, principal.organization_id)
    stmt = select(WebhookDelivery).where(
        WebhookDelivery.id == delivery_id,
        WebhookDelivery.subscription_id == sub_id,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="delivery not found",
        )
    await db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == row.id)
        .values(
            status="pending",
            attempt=0,
            next_attempt_at=datetime.now(timezone.utc),
            response_status=None,
            response_body=None,
            succeeded_at=None,
        )
    )
    await db.commit()
    await db.refresh(row)
    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="webhook.redeliver",
        resource_type="webhook_subscription",
        resource_id=sub_id,
        payload={"delivery_id": str(delivery_id)},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return WebhookDeliveryResponse.model_validate(row)
