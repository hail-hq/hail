"""Routes for the v1 events stream.

GET /events — cursor-paginated forward stream of CallEvents, scoped to the
caller's organization. Optional ``id`` (typed ``<type>:<uuid>``) narrows
to a single resource; optional ``kind`` narrows to a single event kind.

The endpoint replaced ``GET /calls/{call_id}/events`` when tailing
graduated to a top-level concern (``hail tail``). Hail is a universal
communication platform: as SMS / email channels land they will surface
through the same stream, so the route lives next to the channel-agnostic
``Event`` concept rather than under ``/calls``. The ``id`` filter mirrors
the ``audit_log`` ``resource_type`` / ``resource_id`` shape so additional
channels join the surface without another rename.
"""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.db import get_session
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.pagination import fetch_cursor_page
from hailhq.core.models import Call, CallEvent, Email, EmailEvent
from hailhq.core.schemas import (
    CallStatus,
    EventResponse,
    EventStreamResponse,
    parse_resource_id,
)

router = APIRouter(prefix="/events", tags=["events"])

_DEFAULT_EVENTS_LIMIT = 100
_MAX_EVENTS_LIMIT = 1000


def _call_select(kind: str | None):
    """Per-source select; the optional ``kind`` filter is applied here, before
    the union, so it stays sargable per table."""
    stmt = select(
        CallEvent.id.label("id"),
        literal("call").label("source"),
        CallEvent.call_id.label("call_id"),
        literal(None).label("email_id"),
        CallEvent.kind.label("kind"),
        CallEvent.payload.label("payload"),
        CallEvent.occurred_at.label("occurred_at"),
    )
    if kind is not None:
        stmt = stmt.where(CallEvent.kind == kind)
    return stmt


def _email_select(kind: str | None):
    stmt = select(
        EmailEvent.id.label("id"),
        literal("email").label("source"),
        literal(None).label("call_id"),
        EmailEvent.email_id.label("email_id"),
        EmailEvent.kind.label("kind"),
        EmailEvent.payload.label("payload"),
        EmailEvent.occurred_at.label("occurred_at"),
    )
    if kind is not None:
        stmt = stmt.where(EmailEvent.kind == kind)
    return stmt


# --------------------------------------------------------------------------- #
# GET /events
# --------------------------------------------------------------------------- #


@router.get("", response_model=EventStreamResponse)
async def list_events(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_EVENTS_LIMIT, ge=1, le=_MAX_EVENTS_LIMIT),
    id: str | None = Query(default=None),
    kind: str | None = Query(default=None),
) -> EventStreamResponse:
    # Org scoping is the security-critical bit. We always join through Call so
    # the principal can never see another org's events, even by guessing a
    # call_id. The kind filter is a passthrough (no enum validation): event
    # kinds evolve as we add channels.
    #
    # Perf note: the org-wide query path filters via the Call.organization_id
    # JOIN. The current `idx_call_events_call (call_id, occurred_at)` index
    # doesn't help here. For v1 the table is small and the JOIN+filter is
    # fine; if traffic grows, denormalize organization_id onto call_events
    # and add (organization_id, occurred_at).
    call_status = None
    resource_type: str | None = None
    resource_uuid: UUID | None = None
    if id is not None:
        try:
            resource_type, resource_uuid = parse_resource_id(id)
        except ValueError as exc:
            # 422 with the specific issue — no silent empty result for typos.
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    if resource_type == "call":
        assert resource_uuid is not None  # narrowed by the parser
        call_stmt = select(Call).where(
            Call.id == resource_uuid,
            Call.organization_id == principal.organization_id,
        )
        call = (await db.execute(call_stmt)).scalar_one_or_none()
        # 404 for both unknown-and-cross-org IDs — same shape as get_call so
        # we don't leak existence.
        if call is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="call not found",
            )
        selects = [_call_select(kind).where(CallEvent.call_id == resource_uuid)]
        call_status = call.status
    elif resource_type == "email":
        assert resource_uuid is not None  # narrowed by the parser
        email_stmt = select(Email.id).where(
            Email.id == resource_uuid,
            Email.organization_id == principal.organization_id,
        )
        email_row = (await db.execute(email_stmt)).scalar_one_or_none()
        # 404 for both unknown-and-cross-org IDs — same shape as the call
        # branch above so we don't leak existence.
        if email_row is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="email not found",
            )
        selects = [_email_select(kind).where(EmailEvent.email_id == resource_uuid)]
    else:
        selects = [
            _call_select(kind)
            .join(Call, Call.id == CallEvent.call_id)
            .where(Call.organization_id == principal.organization_id),
            _email_select(kind).where(
                EmailEvent.organization_id == principal.organization_id
            ),
        ]

    u = union_all(*selects).subquery() if len(selects) > 1 else selects[0].subquery()

    # Forward walk in time: strictly-greater on (occurred_at, id).
    rows, next_cursor = await fetch_cursor_page(
        db,
        select(u),
        u.c.occurred_at,
        u.c.id,
        cursor=cursor,
        limit=limit,
        scalars=False,
    )

    return EventStreamResponse(
        items=[EventResponse.model_validate(r, from_attributes=True) for r in rows],
        next_cursor=next_cursor,
        call_status=cast(CallStatus | None, call_status),
    )


__all__ = ["router"]
