"""Routes for the v1 outbound calls API.

POST /calls - originate an outbound call (provisions a LiveKit room,
dispatches the voicebot, and places the SIP outbound through the trunk).
GET /calls/{id} - read a single call (org-scoped).
GET /calls - cursor-paginated list (org-scoped, optional status / to filters).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from hailhq.api.agent_gate import (
    RATE_LIMITED_RESPONSES,
    require_agent_send_allowed,
)
from hailhq.api.audit import write_audit_log
from hailhq.api.consent import enforce_consent, isoformat_or_none
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.funds import require_funds
from hailhq.api.idempotency import (
    IdempotencyContext,
    cache_failure,
    idempotency_dep,
    replay_cached,
)
from hailhq.api.numbers import resolve_org_number
from hailhq.api.pagination import fetch_cursor_page
from hailhq.core.agent_tools.registry import all_tools
from hailhq.core.billing import CALL_META_BILLED
from hailhq.core.call_end_reasons import CallEndReason
from hailhq.core.compliance_gate import check_call_allowed
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.internal_webhook import fetch_organization_name
from hailhq.core.languages import SUPPORTED_LANGUAGES
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import Call, CallEvent, PhoneNumber
from hailhq.core.pool import (
    CALL_META_FROM_POOL,
    claim_pool_number,
    release_pool_reservation,
)
from hailhq.core.provider_config import load_org_provider_configs, provider_cipher
from hailhq.core.schemas import (
    TERMINAL_CALL_STATUSES,
    CallCreate,
    CallListResponse,
    CallResponse,
    CallStatus,
)
from hailhq.core.secret_cipher import SecretKeyMissing
from hailhq.core.url_guard import UnsafeUrlError, assert_public_https_url
from hailhq.core.webhook_fanout import fanout_call_event
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calls", tags=["calls"])

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200
_CALL_SETUP_FAILED_DETAIL = "call setup failed"


# --------------------------------------------------------------------------- #
# LiveKit client dependency (overridable in tests).
# --------------------------------------------------------------------------- #


_livekit_singleton: LiveKitClient | None = None


async def get_livekit() -> LiveKitClient:
    """Return a process-wide ``LiveKitClient``.

    Built lazily on first request so import-time settings/env aren't
    required. Tests override this via ``app.dependency_overrides``.

    Must be ``async``: ``LiveKitClient()`` constructs an
    ``aiohttp.ClientSession`` which calls ``asyncio.get_running_loop()``.
    A sync FastAPI dep runs in a threadpool worker thread with no loop;
    the async form keeps us on the main event loop.
    """
    global _livekit_singleton
    if _livekit_singleton is None:
        _livekit_singleton = LiveKitClient()
    return _livekit_singleton


async def close_livekit_singleton() -> None:
    """Close the process-wide ``LiveKitClient`` if one was constructed.

    Wired into ``app.lifespan`` shutdown so the underlying aiohttp session
    is released cleanly. No-op when tests override ``get_livekit``.
    """
    global _livekit_singleton
    if _livekit_singleton is not None:
        await _livekit_singleton.aclose()
        _livekit_singleton = None


async def _cleanup_partial_livekit(
    lk: LiveKitClient,
    room_name: str | None,
    dispatch_id: str | None,
) -> None:
    """Best-effort cleanup for a partially-provisioned LiveKit call."""
    if room_name is None:
        return

    if dispatch_id is not None:
        try:
            await lk.delete_dispatch(dispatch_id, room_name)
        except Exception:  # pragma: no cover - logged, never re-raised
            logger.warning(
                "livekit dispatch cleanup failed for room=%s dispatch_id=%s",
                room_name,
                dispatch_id,
                exc_info=True,
            )

    try:
        await lk.delete_room(room_name)
    except Exception:  # pragma: no cover - logged, never re-raised
        logger.warning(
            "livekit room cleanup failed for room=%s",
            room_name,
            exc_info=True,
        )


# --------------------------------------------------------------------------- #
# POST /calls
# --------------------------------------------------------------------------- #


@router.post(
    "",
    response_model=CallResponse,
    status_code=http_status.HTTP_201_CREATED,
    responses=RATE_LIMITED_RESPONSES,
)
async def create_call(
    body: CallCreate,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient, Depends(get_livekit)],
    idem: Annotated[IdempotencyContext | None, Depends(idempotency_dep)] = None,
) -> CallResponse:
    # Replay before any DB or LiveKit work — a retry must not re-dispatch.
    if idem is not None and idem.is_replay:
        cached_id, cached = replay_cached(idem, response, resource_prefix="/calls")
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="call.create.replayed",
            resource_type="call",
            resource_id=cached_id,
            payload={"to": cached.get("to_e164"), "from": cached.get("from_e164")},
        )
        return CallResponse.model_validate(cached)

    # SSRF guard for a per-call BYO llm.base_url — the full resolving check
    # (DNS + private/loopback/link-local/reserved address rejection), off the
    # event loop so a slow-resolving attacker domain can't stall the worker.
    # LLMConfig's pydantic validator only does cheap https/host syntax checks;
    # this is what actually proves the endpoint is public. Runs before any
    # Call row or LiveKit side effects so a bad URL 422s clean.
    llm_base_url: str | None = None
    if body.llm is not None:
        try:
            llm_base_url = await asyncio.to_thread(
                assert_public_https_url, body.llm.base_url
            )
        except UnsafeUrlError as exc:
            # Deterministic failure — cache it so a same-key retry replays the
            # 422 rather than 409ing on the in-flight sentinel idempotency_dep
            # committed on acquire (matches the consent/compliance/funds/number
            # gates below).
            raise await cache_failure(
                idem,
                HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ),
            ) from exc

    # Consent attestation gate — reject before any Call row is created.
    try:
        enforce_consent(
            recipient_consent=body.recipient_consent,
            consent_source=body.consent_source,
            message_type=body.message_type,
        )
    except HTTPException as exc:
        raise await cache_failure(idem, exc) from None

    # Tool allowlist gate — reject unknown tool names before any Call row is
    # created. `None` (omitted) means "all tools"; `[]` means "no tools" and
    # is passed through as-is below.
    if body.tools is not None:
        known = {t.name for t in all_tools()}
        unknown = sorted(set(body.tools) - known)
        if unknown:
            raise await cache_failure(
                idem,
                unprocessable(
                    f"unknown tools: {', '.join(unknown)}", loc=["body", "tools"]
                ),
            )

    # Language/provider compatibility gate — reject before any Call row is
    # created. Deterministic on request + org config, so failures are
    # cached for idempotent replay like the other 422 gates.
    lang = body.voice_config.language
    org_rows = (
        await load_org_provider_configs(db, principal.organization_id)
        if lang is not None
        else {}
    )
    if lang is not None:
        caps = SUPPORTED_LANGUAGES[lang]
        # Asymmetric by design: the STT side never 422s here — STT provider
        # selection is console-BYO-only (no per-call knob), and routing
        # (org BYO row > language auto-route) degrades safely inside the
        # voicebot (deepgram covers every supported language). TTS has no
        # per-call pin either; the org's BYO TTS row is the sole source of
        # truth, so it's checked here regardless of voice_config contents.
        tts_row = org_rows.get("tts")
        if tts_row is not None and tts_row.provider not in caps.tts:
            raise await cache_failure(
                idem,
                unprocessable(
                    f"your BYO tts provider '{tts_row.provider}' does not "
                    f"support language '{lang}'; supported providers: "
                    f"{sorted(caps.tts)}",
                    loc=["body", "voice_config", "language"],
                ),
            )

    # Compliance gate — suppression/DNC, premium-rate prefix, velocity cap.
    # Also before any Call row is created, so a denial has no resource to
    # clean up; the audit entry below carries resource_id=None.
    gate = await check_call_allowed(db, principal.organization_id, body.to)
    if not gate.allowed:
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="call.blocked",
            resource_type="call",
            resource_id=None,
            payload={"to": body.to, "reason": gate.reason, "checks": gate.checks},
        )
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail=gate.reason,
            ),
        )

    await require_funds(db, principal, idem)
    await require_agent_send_allowed(db, principal, "voice", [body.to], idem)

    # 1. Resolve the from-number: explicit `from` → org-owned active → shared
    #    pool. Pool numbers are never explicitly addressable; naming one would
    #    let a caller grab a number that isn't theirs.
    pool_number: PhoneNumber | None = None
    from_number = await resolve_org_number(
        db, principal.organization_id, body.from_, capability="voice"
    )
    if from_number is None:
        if body.from_ is not None:
            raise await cache_failure(
                idem,
                unprocessable(
                    f"phone number {body.from_} is not registered to this "
                    "organization, is not active, or lacks the voice capability",
                    loc=["body", "from"],
                ),
            )
        pool_number = await claim_pool_number(db)
        if pool_number is None:
            # Transient (no dispatch happened): release the in-flight
            # idempotency sentinel so a same-key retry can succeed once the
            # pool frees up. Without this, the sentinel idempotency_dep
            # committed on acquire would 409 "still processing" on every retry
            # until the 24h TTL — the opposite of the intended behavior.
            if idem is not None:
                await idem.release()
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="shared call line pool exhausted; try again shortly",
            )
        from_number = pool_number

    voice_config = body.voice_config.model_dump()

    # 2. Insert the Call row. Pool path needs flush-then-bind: see
    #    claim_pool_number docstring for the FK dance.
    call_metadata = dict(body.metadata)
    if pool_number is not None:
        call_metadata[CALL_META_FROM_POOL] = True
    # Billed unless this is the unbilled self-hosted shared key. Both real API
    # keys and console/website JWTs are billed, so the mid-call agent funds
    # re-check (internal/agent._shared_denial) applies to them — keying on
    # ``api_key_id is not None`` would wrongly exempt every JWT-placed call.
    call_metadata[CALL_META_BILLED] = principal.auth_kind != "shared"

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
        max_duration_seconds=settings.hail_voice_max_duration_seconds,
        metadata_=call_metadata,
    )
    db.add(call)
    if pool_number is not None:
        await db.flush()  # materialize call.id so the FK target exists.
        pool_number.reserved_call_id = call.id

    await db.commit()
    await db.refresh(call)

    # 3. Audit log in a separate transaction; failures must not unwind the call.
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="call.create",
        resource_type="call",
        resource_id=call.id,
        payload={
            "to": call.to_e164,
            "from": call.from_e164,
            "recipient_consent": body.recipient_consent,
            "consent_source": body.consent_source,
            "consent_obtained_at": isoformat_or_none(body.consent_obtained_at),
            "message_type": body.message_type,
            # Compliance-relevant: record when a caller opted out of the
            # spoken AI disclosure, so the decision is attributable later.
            "ai_disclosure": body.ai_disclosure,
            "compliance": gate.checks,
        },
    )

    # Resolve the org's display name for the spoken TCPA identity
    # disclosure (47 CFR 64.1200(b)(1)). Fail-safe: any lookup failure →
    # None → the voicebot speaks the generic fallback line instead. Run as
    # a task so the up-to-1s lookup overlaps the LiveKit room creation
    # below (only the dispatch metadata needs the result) instead of
    # adding its latency serially; the task never raises, so an abandoned
    # result on the failure path is inert.
    org_name_task = asyncio.create_task(
        fetch_organization_name(str(call.organization_id))
    )

    # 4. External calls — best-effort with status reconciliation.
    room_name: str | None = None
    dispatch_id: str | None = None
    setup_stage = "room_create"
    try:
        room_name = await lk.create_room(call.id)
        setup_stage = "agent_dispatch"

        llm_meta: dict | None = None
        if body.llm is not None:
            llm_meta = body.llm.model_dump()
            llm_meta["base_url"] = llm_base_url  # canonicalized by the guard above
            try:
                cipher = provider_cipher()
                llm_meta["api_key_enc"] = cipher.encrypt(llm_meta.pop("api_key"))
            except SecretKeyMissing:
                # Legacy self-host without HAIL_PROVIDER_SECRET_KEY: keep the
                # historical plaintext dispatch rather than breaking mode B.
                logger.warning(
                    "HAIL_PROVIDER_SECRET_KEY unset - per-call llm.api_key "
                    "sent to LiveKit dispatch in plaintext for call_id=%s",
                    call.id,
                )

        dispatch_id = await lk.dispatch_agent(
            room_name=room_name,
            agent_name="hail-voicebot",
            metadata={
                "call_id": str(call.id),
                "organization_id": str(call.organization_id),
                "voice_config": voice_config,
                "system_prompt": body.system_prompt,
                "llm": llm_meta,
                "first_message": body.first_message,
                "ai_disclosure": body.ai_disclosure,
                "tools": body.tools,
                "org_name": await org_name_task,
            },
        )
        setup_stage = "sip_participant"
        participant = await lk.create_sip_participant(
            room_name=room_name,
            to_e164=call.to_e164,
            from_e164=call.from_e164,
            sip_trunk_id=settings.livekit_sip_outbound_trunk_id,
            participant_identity=f"caller-{call.id}",
        )
    except Exception as exc:
        logger.warning(
            "call setup failed for call_id=%s stage=%s",
            call.id,
            setup_stage,
            exc_info=True,
        )
        await _cleanup_partial_livekit(lk, room_name, dispatch_id)
        now = datetime.now(timezone.utc)
        # Coerce via CallEndReason so an unrecognized stage name fails here
        # rather than at the DB ENUM boundary.
        failure_code = CallEndReason(f"{setup_stage}_failed").value
        # Guard on a non-terminal status: the voicebot's own dispatch can race
        # this LiveKit-failure path and already finalize the row (e.g.
        # provider_key_error on a BYO session-build failure). This write must
        # lose cleanly rather than clobber that outcome. rowcount gates the
        # event below.
        result = await db.execute(
            update(Call)
            .where(Call.id == call.id, Call.status.not_in(TERMINAL_CALL_STATUSES))
            .values(
                status="failed",
                end_reason=failure_code,
                ended_at=now,
            )
        )
        if (result.rowcount or 0) > 0:
            db.add(
                CallEvent(
                    call_id=call.id,
                    kind="state_change",
                    payload={
                        "from": "queued",
                        "to": "failed",
                        "reason": failure_code,
                    },
                )
            )
            await fanout_call_event(
                db,
                organization_id=call.organization_id,
                event_type="call.failed",
                event_id=call.id,
                data={"id": str(call.id), "status": "failed"},
            )
        # No-op when this call didn't hold a pool reservation.
        await release_pool_reservation(db, call_id=call.id)
        await db.commit()
        if idem is not None:
            # Cache failures too — Stripe-style retries replay rather than
            # re-dispatching. A fresh attempt requires a new Idempotency-Key.
            await idem.store(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                body={"detail": _CALL_SETUP_FAILED_DETAIL},
            )
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=_CALL_SETUP_FAILED_DETAIL,
        ) from exc

    # Success: update Call to dialing + insert state-change CallEvent.
    # Guard on `status='queued'`: the voicebot owns the answer transition and,
    # in the rare case the SIP leg goes active before this commit lands, will
    # already have written `in_progress`. The guard makes this write lose that
    # race cleanly instead of clobbering `in_progress` back to `dialing`. The
    # event is emitted only when the row actually transitioned (rowcount).
    now = datetime.now(timezone.utc)
    sip_call_id = getattr(participant, "sip_call_id", None)
    result = await db.execute(
        update(Call)
        .where(Call.id == call.id, Call.status == "queued")
        .values(
            livekit_room=room_name,
            provider_call_sid=sip_call_id,
            status="dialing",
            started_at=now,
        )
    )
    if (result.rowcount or 0) > 0:
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
    rows, next_cursor = await fetch_cursor_page(
        db,
        stmt,
        Call.created_at,
        Call.id,
        cursor=cursor,
        limit=limit,
        newest_first=True,
    )

    return CallListResponse(
        items=[CallResponse.model_validate(c) for c in rows],
        next_cursor=next_cursor,
    )


__all__ = ["get_livekit", "router"]
