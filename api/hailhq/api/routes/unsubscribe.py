"""GET /unsubscribe — public, unauthenticated one-click email opt-out.

Every outbound email carries a per-send signed link (List-Unsubscribe /
List-Unsubscribe-Post headers — see ``create_email`` in emails.py). No
auth: the HMAC-signed ``token`` query param *is* the credential. Anyone
holding a valid token for an address can suppress it, which is the point
of one-click unsubscribe (RFC 8058).
"""

from __future__ import annotations

import logging
from html import escape
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from hailhq.core.compliance_gate import add_suppression
from hailhq.core.db import get_session
from hailhq.core.unsubscribe import InvalidUnsubscribeToken, verify_unsubscribe_token
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(tags=["unsubscribe"])


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(
    db: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[str, Query()],
) -> HTMLResponse:
    try:
        email, organization_id = verify_unsubscribe_token(token)
    except InvalidUnsubscribeToken:
        logger.info("unsubscribe: invalid or expired token")
        return HTMLResponse(
            "<p>This unsubscribe link is invalid or has expired.</p>",
            status_code=400,
        )

    await add_suppression(
        db,
        organization_id=organization_id,
        recipient=email,
        channel="email",
        reason="recipient_unsubscribed",
        source="unsubscribe_link",
    )
    await db.commit()

    return HTMLResponse(
        f"<p>{escape(email)} has been unsubscribed and will not receive "
        "further emails from this sender.</p>"
    )


__all__ = ["router"]
