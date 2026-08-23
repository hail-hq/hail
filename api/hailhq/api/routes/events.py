"""Routes for the v1 events stream.

GET /events — cursor-paginated forward stream of call, email, and SMS
events, scoped to the caller's organization. Optional ``id`` (typed
``<type>:<uuid>``) narrows to a single resource; optional ``kind``
narrows to a single event kind.

The endpoint replaced ``GET /calls/{call_id}/events`` when tailing
graduated to a top-level concern (``hail tail``). Hail is a universal
communication platform: every channel surfaces through the same stream,
so the route lives next to the channel-agnostic ``Event`` concept rather
than under ``/calls``. The ``id`` filter mirrors the ``audit_log``
``resource_type`` / ``resource_id`` shape so additional channels join
the surface without another rename.
"""

from __future__ import annotations

from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.core.db import get_session
from hailhq.core.models import Call, CallEvent, Email, EmailEvent, Sms, SmsEvent
from hailhq.core.schemas import (
    CallStatus,
    EventResponse,
    EventStreamResponse,
    parse_resource_id,
)
from sqlalchemy import literal, select, union_all
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/events", tags=["events"])


# Typed NULL for the id columns a source doesn't own. An untyped
# ``literal(None)`` resolves to ``text`` when two NULL arms of the 3-way
# UNION pair up first, which then can't be matched against the uuid arm
# (``UNION types text and uuid cannot be matched``).
def _null_uuid():
    return literal(None, type_=PG_UUID(as_uuid=True))


_DEFAULT_EVENTS_LIMIT = 100
_MAX_EVENTS_LIMIT = 1000


def _call_select(kind: str | None):
    """Per-source select; the optional ``kind`` filter is applied here, before
    the union, so it stays sargable per table."""
    stmt = select(
        CallEvent.id.label("id"),
        literal("call").label("source"),
        CallEvent.call_id.label("call_id"),
        _null_uuid().label("email_id"),
        _null_uuid().label("sms_id"),
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
        _null_uuid().label("call_id"),
        EmailEvent.email_id.label("email_id"),
        _null_uuid().label("sms_id"),
        EmailEvent.kind.label("kind"),
        EmailEvent.payload.label("payload"),
        EmailEvent.occurred_at.label("occurred_at"),
    )
    if kind is not None:
        stmt = stmt.where(EmailEvent.kind == kind)
    return stmt


def _sms_select(kind: str | None):
    stmt = select(
        SmsEvent.id.label("id"),
        literal("sms").label("source"),
        _null_uuid().label("call_id"),
        _null_uuid().label("email_id"),
        SmsEvent.sms_id.label("sms_id"),
        SmsEvent.kind.label("kind"),
        SmsEvent.payload.label("payload"),
        SmsEvent.occurred_at.label("occurred_at"),
    )
    if kind is not None:
        stmt = stmt.where(SmsEvent.kind == kind)
    return stmt


async def _require_owned(
    db: AsyncSession,
    model: Any,
    resource_uuid: UUID,
    principal: Principal,
    label: str,
    *,
    load: Any = None,
) -> Any:
    """Org-scoped existence check for the ``id`` filter, one per channel.

    404 for both unknown-and-cross-org IDs — same shape as the resources'
    own GET routes so we don't leak existence. The ``organization_id``
    predicate lives here, in exactly one place, so a new channel branch
    can't accidentally drop it.

    Selects only the primary key (plus ``load``, e.g. ``Call.status``, when a
    caller needs one extra column) — never the whole ORM row, so a ``hail
    tail`` poll's existence check doesn't hydrate large body columns.
    """
    columns = (model.id,) if load is None else (model.id, load)
    row = (
        await db.execute(
            select(*columns).where(
                model.id == resource_uuid,
                model.organization_id == principal.organization_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"{label} not found",
        )
    return row


# --------------------------------------------------------------------------- #
# GET /events
# --------------------------------------------------------------------------- #


@router.get(
    "",
    response_model=EventStreamResponse,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def list_events(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_EVENTS_LIMIT, ge=1, le=_MAX_EVENTS_LIMIT),
    id: str | None = Query(default=None),
    kind: str | None = Query(default=None),
) -> EventStreamResponse:
    """Cursor-paginated forward stream of call, email, and SMS events.

    Scoped to the caller's organization. Pass id (typed "<type>:<uuid>",
    e.g. "call:<uuid>") to narrow to one resource's events, or kind to
    narrow to one event kind. Walks forward in time — pass the returned
    next_cursor to continue tailing where you left off.
    """
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
            raise unprocessable(str(exc), loc=["query", "id"]) from exc

    if resource_type == "call":
        assert resource_uuid is not None  # narrowed by the parser
        call = await _require_owned(
            db, Call, resource_uuid, principal, "call", load=Call.status
        )
        selects = [_call_select(kind).where(CallEvent.call_id == resource_uuid)]
        call_status = call.status
    elif resource_type == "email":
        assert resource_uuid is not None  # narrowed by the parser
        await _require_owned(db, Email, resource_uuid, principal, "email")
        selects = [_email_select(kind).where(EmailEvent.email_id == resource_uuid)]
    elif resource_type == "sms":
        assert resource_uuid is not None  # narrowed by the parser
        await _require_owned(db, Sms, resource_uuid, principal, "sms")
        selects = [_sms_select(kind).where(SmsEvent.sms_id == resource_uuid)]
    else:
        selects = [
            _call_select(kind)
            .join(Call, Call.id == CallEvent.call_id)
            .where(Call.organization_id == principal.organization_id),
            _email_select(kind).where(
                EmailEvent.organization_id == principal.organization_id
            ),
            _sms_select(kind).where(
                SmsEvent.organization_id == principal.organization_id
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
