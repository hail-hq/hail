"""Routes for the v1 outbound calls API.

POST /calls - originate an outbound call (provisions a LiveKit room,
dispatches the voicebot, and places the SIP outbound through the trunk).
GET /calls/{id} - read a single call (org-scoped).
GET /calls - cursor-paginated list (org-scoped, optional status / to filters).
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.db import get_session, session_scope
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.idempotency import IdempotencyContext, idempotency_for_post_calls
from hailhq.core.config import settings
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import AuditLog, Call, CallEvent, PhoneNumber
from hailhq.core.schemas import (
    CallCreate,
    CallListResponse,
    CallResponse,
    CallStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calls", tags=["calls"])

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200


# --------------------------------------------------------------------------- #
# LiveKit client dependency (overridable in tests).
# --------------------------------------------------------------------------- #


_livekit_singleton: LiveKitClient | None = None


def get_livekit() -> LiveKitClient:
    """Return a process-wide ``LiveKitClient``.

    Built lazily on first request so import-time settings/env aren't
    required. Tests override this via ``app.dependency_overrides``.
    """
    global _livekit_singleton
    if _livekit_singleton is None:
        _livekit_singleton = LiveKitClient()
    return _livekit_singleton


# --------------------------------------------------------------------------- #
# Cursor encoding helpers.
# --------------------------------------------------------------------------- #


def _encode_cursor(created_at: datetime, call_id: UUID) -> str:
    raw = f"{created_at.isoformat()}|{call_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), UUID(id_str)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"invalid cursor: {exc}",
        ) from exc


# --------------------------------------------------------------------------- #
# Audit logging — runs in a fresh session so failures don't roll back the call.
# --------------------------------------------------------------------------- #


async def _write_audit_log(
    organization_id: UUID,
    api_key_id: UUID,
    action: str,
    resource_type: str,
    resource_id: UUID,
    payload: dict[str, Any],
) -> None:
    try:
        async with session_scope() as session:
            session.add(
                AuditLog(
                    organization_id=organization_id,
                    api_key_id=api_key_id,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    payload=payload,
                )
            )
            await session.commit()
    except Exception:  # pragma: no cover - logged, never re-raised
        logger.warning(
            "audit_log write failed for action=%s resource_id=%s",
            action,
            resource_id,
            exc_info=True,
        )


# --------------------------------------------------------------------------- #
# POST /calls
# --------------------------------------------------------------------------- #


@router.post(
    "",
    response_model=CallResponse,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_call(
    body: CallCreate,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient, Depends(get_livekit)],
    idem: Annotated[
        IdempotencyContext | None, Depends(idempotency_for_post_calls)
    ] = None,
) -> CallResponse:
    # Replay before any DB or LiveKit work — a retry must not re-dispatch.
    if idem is not None and idem.is_replay:
        cached = idem.cached_response or {}
        if idem.cached_status and idem.cached_status >= 400:
            raise HTTPException(
                status_code=idem.cached_status,
                detail=cached.get("detail", "cached failure"),
                headers={"Idempotency-Replay": "true"},
            )
        cached_id = UUID(cached["id"])
        await _write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="call.create.replayed",
            resource_type="call",
            resource_id=cached_id,
            payload={"to": cached.get("to_e164"), "from": cached.get("from_e164")},
        )
        response.headers["Idempotency-Replay"] = "true"
        response.headers["Location"] = f"/calls/{cached_id}"
        return CallResponse.model_validate(cached)

    # 1. Resolve the from-number for this org.
    if body.from_ is not None:
        stmt = select(PhoneNumber).where(
            PhoneNumber.organization_id == principal.organization_id,
            PhoneNumber.e164 == body.from_,
        )
        from_number = (await db.execute(stmt)).scalar_one_or_none()
        if from_number is None:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"phone number {body.from_} is not registered to this organization",
            )
    else:
        stmt = (
            select(PhoneNumber)
            .where(
                PhoneNumber.organization_id == principal.organization_id,
                PhoneNumber.provisioning_state == "active",
            )
            .order_by(PhoneNumber.created_at.asc())
            .limit(1)
        )
        from_number = (await db.execute(stmt)).scalar_one_or_none()
        if from_number is None:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="no active phone number on this organization; provision one or pass `from`",
            )

    voice_config = body.voice_config.model_dump()

    # 2. Insert the Call row and commit (separate from audit + LiveKit).
    call = Call(
        organization_id=principal.organization_id,
        conversation_id=body.conversation_id,
        from_number_id=from_number.id,
        from_e164=from_number.e164,
        to_e164=body.to,
        direction="outbound",
        status="queued",
        voice_config=voice_config,
        initial_prompt=body.system_prompt,
        metadata_=body.metadata,
    )
    db.add(call)
    await db.commit()
    await db.refresh(call)

    # 3. Audit log in a separate transaction; failures must not unwind the call.
    await _write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="call.create",
        resource_type="call",
        resource_id=call.id,
        payload={"to": call.to_e164, "from": call.from_e164},
    )

    # 4. External calls — best-effort with status reconciliation.
    try:
        room_name = await lk.create_room(call.id)
        await lk.dispatch_agent(
            room_name=room_name,
            agent_name="hail-voicebot",
            metadata={
                "call_id": str(call.id),
                "voice_config": voice_config,
                "system_prompt": body.system_prompt,
                "llm": body.llm.model_dump() if body.llm else None,
                "first_message": body.first_message,
            },
        )
        participant = await lk.create_sip_participant(
            room_name=room_name,
            to_e164=call.to_e164,
            from_e164=call.from_e164,
            sip_trunk_id=settings.livekit_sip_trunk_id,
            participant_identity=f"caller-{call.id}",
        )
    except Exception as exc:
        now = datetime.now(timezone.utc)
        await db.execute(
            update(Call)
            .where(Call.id == call.id)
            .values(
                status="failed",
                end_reason=str(exc)[:200],
                ended_at=now,
            )
        )
        db.add(
            CallEvent(
                call_id=call.id,
                kind="state_change",
                payload={
                    "from": "queued",
                    "to": "failed",
                    "error": str(exc)[:200],
                },
            )
        )
        await db.commit()
        failure_detail = f"livekit dispatch failed: {exc}"
        if idem is not None:
            # Cache failures too — Stripe-style retries replay rather than
            # re-dispatching. A fresh attempt requires a new Idempotency-Key.
            await idem.store(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                body={"detail": failure_detail},
            )
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=failure_detail,
        ) from exc

    # Success: update Call to dialing + insert state-change CallEvent.
    now = datetime.now(timezone.utc)
    sip_call_id = getattr(participant, "sip_call_id", None)
    await db.execute(
        update(Call)
        .where(Call.id == call.id)
        .values(
            livekit_room=room_name,
            provider_call_sid=sip_call_id,
            status="dialing",
            started_at=now,
        )
    )
    db.add(
        CallEvent(
            call_id=call.id,
            kind="state_change",
            payload={"from": "queued", "to": "dialing"},
        )
    )
    await db.commit()
    await db.refresh(call)

    response.headers["Location"] = f"/calls/{call.id}"
    call_response = CallResponse.model_validate(call)

    if idem is not None:
        # ``mode="json"`` matches what FastAPI is about to serialize, so a
        # later replay produces the byte-identical body.
        await idem.store(
            status_code=http_status.HTTP_201_CREATED,
            body=call_response.model_dump(mode="json"),
        )

    return call_response


# --------------------------------------------------------------------------- #
# GET /calls/{id}
# --------------------------------------------------------------------------- #


@router.get("/{call_id}", response_model=CallResponse)
async def get_call(
    call_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> CallResponse:
    stmt = select(Call).where(
        Call.id == call_id,
        Call.organization_id == principal.organization_id,
    )
    call = (await db.execute(stmt)).scalar_one_or_none()
    if call is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="call not found",
        )
    return CallResponse.model_validate(call)


# --------------------------------------------------------------------------- #
# GET /calls
# --------------------------------------------------------------------------- #


@router.get("", response_model=CallListResponse)
async def list_calls(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
    status: CallStatus | None = Query(default=None),
    to: str | None = Query(default=None),
) -> CallListResponse:
    stmt = select(Call).where(Call.organization_id == principal.organization_id)
    if status is not None:
        stmt = stmt.where(Call.status == status)
    if to is not None:
        stmt = stmt.where(Call.to_e164 == to)
    if cursor is not None:
        cur_ts, cur_id = _decode_cursor(cursor)
        stmt = stmt.where(tuple_(Call.created_at, Call.id) < tuple_(cur_ts, cur_id))

    stmt = stmt.order_by(Call.created_at.desc(), Call.id.desc()).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())

    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(last.created_at, last.id)
        rows = rows[:limit]

    return CallListResponse(
        items=[CallResponse.model_validate(c) for c in rows],
        next_cursor=next_cursor,
    )


__all__ = ["router", "get_livekit"]
