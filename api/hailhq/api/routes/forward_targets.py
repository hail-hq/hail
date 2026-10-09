"""Forward-target verification routes.

GET  /forward-targets                    — every forward address of the org + status.
POST /forward-targets/{address}/resend   — new confirm link for a pending/stopped address.
GET  /forward-targets/confirm?token=…    — public page with one button (link scanners
                                           prefetch GETs, so a GET never confirms).
POST /forward-targets/confirm            — public, form-posted token: marks verified.

State machine and why: ``hailhq.core.forward_targets``.
"""

from __future__ import annotations

import logging
from html import escape
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi import status as http_status
from fastapi.responses import HTMLResponse
from hailhq.api.audit import actor_of, write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.ratelimit import (
    GENERAL_RATE_LIMITED_RESPONSES,
    merge_rate_limited_responses,
)
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.forward_targets import (
    MAX_CONFIRM_MAILS_PER_DAY,
    AlreadyVerified,
    ConfirmBudgetExceeded,
    ResendTooSoon,
    confirm_target,
    find_by_token,
    list_targets,
    normalize_address,
    reissue_token,
)
from hailhq.core.models import EmailDomain, EmailForwardTarget
from hailhq.core.schemas import ForwardTargetListResponse, ForwardTargetResponse
from hailhq.core.system_email import (
    SYSTEM_KIND_FORWARD_CONFIRM,
    confirm_url,
    enqueue_system_email,
    noreply_address,
    render_forward_confirm,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/forward-targets", tags=["forward-targets"])


def _to_response(t: EmailForwardTarget) -> ForwardTargetResponse:
    return ForwardTargetResponse(
        address=t.address,
        status=t.status,  # type: ignore[arg-type]
        verified_at=t.verified_at,
        token_sent_at=t.token_sent_at if t.status != "verified" else None,
        stopped_at=t.stopped_at,
        stopped_reason=t.stopped_reason,
    )


@router.get(
    "",
    response_model=ForwardTargetListResponse,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def list_forward_targets(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ForwardTargetListResponse:
    """List every forward address of the organization with its status.

    An address appears here once it was ever put in a domain's
    ``forward_to``. Only ``verified`` addresses receive forwards.
    """
    targets = await list_targets(db, principal.organization_id)
    return ForwardTargetListResponse(items=[_to_response(t) for t in targets])


async def _sending_identity(db: AsyncSession, organization_id) -> EmailDomain | None:
    """The row a confirm mail is sent through: the org's hail-mail address
    when it has one, else its first custom domain."""
    stmt = (
        select(EmailDomain)
        .where(EmailDomain.organization_id == organization_id)
        .order_by((EmailDomain.kind != "hail_mail"), EmailDomain.created_at.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


@router.post(
    "/{address}/resend",
    response_model=ForwardTargetResponse,
    responses={
        404: {"description": "No such forward address in this organization."},
        409: {
            "description": "The address is already verified, or the organization has no sending identity."
        },
        **merge_rate_limited_responses(
            {
                429: {
                    "description": "A confirm link was sent less than 10 minutes ago, or the organization reached its daily confirm-mail limit.",
                    "headers": {
                        "Retry-After": {
                            "description": "Seconds to wait before retrying (cooldown case).",
                            "schema": {"type": "integer"},
                        }
                    },
                }
            },
            GENERAL_RATE_LIMITED_RESPONSES,
        ),
    },
)
async def resend_forward_confirm(
    address: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ForwardTargetResponse:
    """Send a new confirm link to a pending or stopped forward address.

    Use this when the first mail got lost, or to restart forwarding after a
    spam complaint stopped it. One link per address every 10 minutes.
    """
    addr = normalize_address(address)
    target = (
        await db.execute(
            select(EmailForwardTarget)
            .where(EmailForwardTarget.organization_id == principal.organization_id)
            .where(EmailForwardTarget.address == addr)
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"no forward target {addr!r}",
        )
    identity = await _sending_identity(db, principal.organization_id)
    if identity is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="organization has no email identity to send the confirm link from",
        )
    try:
        raw = await reissue_token(db, target)
    except AlreadyVerified as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"{addr!r} is already verified",
        ) from exc
    except ResendTooSoon as exc:
        retry = max(1, int(exc.retry_after.total_seconds()))
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"a confirm link was sent recently; retry in {retry}s",
            headers={"Retry-After": str(retry)},
        ) from exc
    except ConfirmBudgetExceeded as exc:
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"confirm-mail limit reached ({MAX_CONFIRM_MAILS_PER_DAY} per "
                "day); try again tomorrow"
            ),
        ) from exc

    sender = noreply_address(
        principal.organization_id,
        identity.local_prefix_org,
        settings.hail_mail_base_domain,
    )
    subject, text, html = render_forward_confirm(
        address=target.address, forwarder=sender, url=confirm_url(raw)
    )
    await enqueue_system_email(
        db,
        organization_id=principal.organization_id,
        email_domain_id=identity.id,
        from_address=sender,
        to=target.address,
        subject=subject,
        body_text=text,
        body_html=html,
        kind=SYSTEM_KIND_FORWARD_CONFIRM,
    )
    await db.commit()
    await db.refresh(target)

    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="forward_target.resend",
        resource_type="forward_target",
        resource_id=target.id,
        payload={"address": target.address, "status": target.status},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return _to_response(target)


def _page(body: str, *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        "<title>Hail email forwarding</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;"
        "margin:3rem auto;padding:0 1rem;color:#111}"
        "button{font:inherit;padding:.6rem 1.2rem;cursor:pointer}</style>"
        f"</head><body>{body}</body></html>",
        status_code=status_code,
    )


_INVALID = (
    "<h1>This link is not valid</h1>"
    "<p>The confirm link is unknown, expired, or already used. "
    "Ask the Hail workspace to send a new one.</p>"
)


@router.get("/confirm", response_class=HTMLResponse, include_in_schema=False)
async def confirm_forward_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[str, Query()],
) -> HTMLResponse:
    """Landing page for the confirm link. Shows one button; the click POSTs.

    A GET never changes state: corporate link scanners prefetch every URL
    in an email, and that must not count as the mailbox owner's consent.
    """
    target = await find_by_token(db, token)
    if target is None:
        return _page(_INVALID, status_code=400)
    return _page(
        "<h1>Confirm email forwarding</h1>"
        f"<p>A Hail workspace wants to forward its inbound email to "
        f"<b>{escape(target.address)}</b>.</p>"
        "<p>Press the button to accept. Nothing is forwarded until you do.</p>"
        f'<form method="post" action="{escape(request.url.path, quote=True)}">'
        f'<input type="hidden" name="token" value="{escape(token, quote=True)}">'
        '<button type="submit">Confirm forwarding</button></form>'
    )


@router.post("/confirm", response_class=HTMLResponse, include_in_schema=False)
async def confirm_forward(
    db: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[str, Form()],
) -> HTMLResponse:
    """Form target of the confirm page: marks the address verified."""
    target = await find_by_token(db, token)
    if target is None:
        logger.info("forward-target confirm: invalid or expired token")
        return _page(_INVALID, status_code=400)
    await confirm_target(db, target)
    await db.commit()
    await write_audit_log(
        organization_id=target.organization_id,
        api_key_id=None,
        action="forward_target.confirm",
        resource_type="forward_target",
        resource_id=target.id,
        payload={"address": target.address},
        actor_kind="recipient",
    )
    return _page(
        "<h1>Forwarding confirmed</h1>"
        f"<p><b>{escape(target.address)}</b> will now receive the workspace's "
        "forwarded email.</p>"
    )


__all__ = ["router"]
