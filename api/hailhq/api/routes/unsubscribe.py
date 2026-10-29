"""GET and POST /unsubscribe — public, unauthenticated one-click email opt-out.

Every outbound email with exactly one recipient carries a per-send signed
link (List-Unsubscribe / List-Unsubscribe-Post headers, see
``deliver_email`` in emails.py). No auth: the HMAC-signed ``token`` query
param *is* the credential. Anyone holding a valid token for an address can
suppress it, which is the point of one-click unsubscribe (RFC 8058).

``GET`` is a person clicking the link. ``POST`` is the mailbox provider's
own unsubscribe button (Gmail, Yahoo): RFC 8058 has it POST
``List-Unsubscribe=One-Click`` to the same URL, token in the query string.
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


_INVALID_TOKEN_RESPONSE = {
    400: {
        "description": "The token is invalid or has expired.",
        "content": {"text/html": {"schema": {"type": "string"}}},
    }
}

# RFC 8058 body the mailbox provider sends. Documented only: the handler
# never reads it, so a POST with no body works too.
_ONE_CLICK_BODY = {
    "requestBody": {
        "required": False,
        "content": {
            "application/x-www-form-urlencoded": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "List-Unsubscribe": {
                            "type": "string",
                            "enum": ["One-Click"],
                        }
                    },
                }
            }
        },
    }
}


@router.get(
    "/unsubscribe", response_class=HTMLResponse, responses=_INVALID_TOKEN_RESPONSE
)
@router.post(
    "/unsubscribe",
    response_class=HTMLResponse,
    responses=_INVALID_TOKEN_RESPONSE,
    openapi_extra=_ONE_CLICK_BODY,
)
async def unsubscribe(
    db: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[str, Query()],
) -> HTMLResponse:
    """One-click email opt-out (RFC 8058).

    ``GET`` is a person clicking the link in a sent email. ``POST`` is the
    mailbox provider's unsubscribe button (Gmail, Yahoo), which sends the
    body ``List-Unsubscribe=One-Click``. The body is optional.

    Public and unauthenticated — the signed token query param is the sole
    credential, proving the caller holds a link Hail sent to that address.
    On success, the address is added to the org's suppression list and all
    future outbound email to it is blocked. Returns an HTML page, not JSON.
    """
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
