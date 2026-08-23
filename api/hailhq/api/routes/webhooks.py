"""CRUD for org-wide webhook subscriptions and per-attempt delivery audit.

POST     /webhooks                              create + return plaintext secret once
GET      /webhooks                              list paginated
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

import secrets as _secrets
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from hailhq.api.audit import write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.http_post import validate_webhook_target
from hailhq.core.models import WebhookDelivery, WebhookSubscription
from hailhq.core.schemas import (
    WebhookDeliveryListResponse,
    WebhookDeliveryResponse,
    WebhookSubscriptionCreate,
    WebhookSubscriptionListResponse,
    WebhookSubscriptionPatch,
    WebhookSubscriptionResponse,
)
from hailhq.core.secret_cipher import SecretCipher
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _new_secret() -> str:
    return "whs_" + _secrets.token_urlsafe(24)


def _to_response(
    sub: WebhookSubscription, *, secret: str | None = None
) -> WebhookSubscriptionResponse:
    resp = WebhookSubscriptionResponse.model_validate(sub)
    if secret is not None:
        resp = resp.model_copy(update={"secret": secret})
    return resp


async def _load_owned(
    db: AsyncSession, sub_id: UUID, org_id: UUID
) -> WebhookSubscription:
    stmt = select(WebhookSubscription).where(
        WebhookSubscription.id == sub_id,
        WebhookSubscription.organization_id == org_id,
    )
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
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def create_subscription(
    body: WebhookSubscriptionCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookSubscriptionResponse:
    """Create a webhook subscription for one or more event types.

    The response includes the plaintext signing secret — this is the only
    time it is ever returned; store it now. Every later GET omits it. Use
    POST /webhooks/{sub_id}/rotate-secret to get a new plaintext secret if
    it is lost or compromised.
    """
    try:
        validate_webhook_target(
            body.target_url,
            allow_private_networks=settings.hail_webhook_allow_private_networks,
        )
    except ValueError as exc:
        raise unprocessable(str(exc), loc=["body", "target_url"]) from exc
    secret = _new_secret()
    cipher = SecretCipher(settings.hail_webhook_secret_key)
    sub = WebhookSubscription(
        organization_id=principal.organization_id,
        target_url=body.target_url,
        secret_encrypted=cipher.encrypt(secret),
        event_types=list(body.event_types),
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="webhook.create",
        resource_type="webhook_subscription",
        resource_id=sub.id,
        payload={"target_url": sub.target_url},
    )
    return _to_response(sub, secret=secret)


@router.get(
    "",
    response_model=WebhookSubscriptionListResponse,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def list_subscriptions(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> WebhookSubscriptionListResponse:
    """List webhook subscriptions for the caller's organization.

    Cursor-paginated, newest first. The signing secret is never included —
    only POST /webhooks and POST /webhooks/{sub_id}/rotate-secret return it.
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


@router.get(
    "/{sub_id}",
    response_model=WebhookSubscriptionResponse,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
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
    responses=GENERAL_RATE_LIMITED_RESPONSES,
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
    sub = await _load_owned(db, sub_id, principal.organization_id)
    updates: dict = {}
    if body.target_url is not None:
        try:
            validate_webhook_target(
                body.target_url,
                allow_private_networks=settings.hail_webhook_allow_private_networks,
            )
        except ValueError as exc:
            raise unprocessable(str(exc), loc=["body", "target_url"]) from exc
        updates["target_url"] = body.target_url
    if body.event_types is not None:
        updates["event_types"] = list(body.event_types)
    if body.status is not None:
        updates["status"] = body.status
        # Re-enabling a subscription resets the failure counter so the
        # 50-strike auto-disable doesn't immediately fire again.
        if body.status == "active":
            updates["consecutive_failures"] = 0
    if updates:
        updates["updated_at"] = datetime.now(timezone.utc)
        await db.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == sub.id)
            .values(**updates)
        )
        await db.commit()
        await db.refresh(sub)
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="webhook.patch",
            resource_type="webhook_subscription",
            resource_id=sub.id,
            payload={"target_url": sub.target_url, "status": sub.status},
        )
    return _to_response(sub)


@router.delete(
    "/{sub_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
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
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="webhook.delete",
        resource_type="webhook_subscription",
        resource_id=sub_id,
        payload={},
    )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post(
    "/{sub_id}/rotate-secret",
    response_model=WebhookSubscriptionResponse,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
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
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    await db.refresh(sub)
    # Log the rotation only — NEVER the secret itself.
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="webhook.rotate_secret",
        resource_type="webhook_subscription",
        resource_id=sub.id,
        payload={},
    )
    return _to_response(sub, secret=secret)


@router.get(
    "/{sub_id}/deliveries",
    response_model=WebhookDeliveryListResponse,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
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
    and response body Hail recorded — use POST /webhooks/{sub_id}/
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
    responses=GENERAL_RATE_LIMITED_RESPONSES,
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
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="webhook.redeliver",
        resource_type="webhook_subscription",
        resource_id=sub_id,
        payload={"delivery_id": str(delivery_id)},
    )
    return WebhookDeliveryResponse.model_validate(row)
