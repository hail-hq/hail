"""Routes for the outbound SMS channel.

POST /sms - send an outbound SMS from the org's dedicated number.
GET /sms/{id} - read a single message (org-scoped).
GET /sms - cursor-paginated list (org-scoped, optional status / to filters).

No pool fallback: SMS requires a dedicated PhoneNumber on the org (see
Decision 6 of the SMS design spec) — inbound replies need unambiguous
number-to-org routing, which a shared pool number can't provide.

One exception: a workspace without approved US registration can omit ``from``
to send up to 25 trial texts to its verified trial phone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi import status as http_status
from hailhq.api.agent_gate import (
    RATE_LIMITED_RESPONSES,
    require_agent_send_allowed,
)
from hailhq.api.audit import actor_of, write_audit_log
from hailhq.api.consent import enforce_consent, isoformat_or_none
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.funds import FUNDS_RESPONSES, require_funds
from hailhq.api.idempotency import (
    IdempotencyContext,
    cache_failure,
    idempotency_dep,
    replay_cached,
)
from hailhq.api.numbers import resolve_org_number
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.ratelimit import (
    GENERAL_RATE_LIMITED_RESPONSES,
    merge_rate_limited_responses,
)
from hailhq.api.route_prefixes import request_mount_prefix
from hailhq.api.suppressions import (
    delete_channel_suppression,
    list_channel_suppressions,
)
from hailhq.api.usage import write_usage_event
from hailhq.core.carrier_routing import sms_route, sms_status_path
from hailhq.core.compliance_gate import check_sms_allowed
from hailhq.core.config import settings
from hailhq.core.db import get_session, org_lock
from hailhq.core.models import (
    Sms,
    SmsEvent,
    SmsSenderIdentity,
)
from hailhq.core.pricing_tier import classify_pricing_tier
from hailhq.core.schemas import (
    SenderIdPatch,
    SenderIdResponse,
    SmsCreate,
    SmsListResponse,
    SmsResponse,
    SmsStatus,
    SuppressionListResponse,
)
from hailhq.core.sender_id import PLATFORM_DEFAULT_SENDER_ID, resolve_sender
from hailhq.core.sms_registration_gate import (
    is_us,
    registered_sms_number,
    us_registration_block,
)
from hailhq.core.sms_setup import org_messaging_service_sid
from hailhq.core.sms_trial import (
    TRIAL_HINT,
    TrialBodyTooLong,
    claim_trial_send,
    trial_hint_applies,
    trial_refusal_reason,
)
from hailhq.core.telemetry_identity import tag_context
from hailhq.core.urls import join_url
from hailhq.core.webhook_fanout import fanout_sms_event
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/sms", tags=["sms"], responses=GENERAL_RATE_LIMITED_RESPONSES
)

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200
_SMS_SEND_FAILED_DETAIL = "sms send failed"


async def deliver_sms(
    db: AsyncSession, sms: Sms, *, billable: bool = True
) -> str | None:
    """Wire-send one queued Sms row and reconcile its status.

    Shared by POST /sms and the internal agent-send route. Returns None
    when the carrier accepted (status='sent', usage billed),
    'provider_error' on transport failure, or the carrier error code on
    rejection. Never raises — the caller owns HTTP semantics.
    """
    try:
        provider = sms_route(sms.provider)
        callback_url = join_url(settings.hail_api_url, sms_status_path(sms.provider))
        messaging_service_sid = None
        if sms.from_number_id is None and provider.sender_id_needs_messaging_service:
            # A sender-ID send has no number to carry the org's messaging
            # service, so name it: the one the org's numbers on this carrier
            # already share, else a fresh one (created once, then found by
            # name). Under the org lock, like number setup: two first sends
            # at once must not create two services. Released at the commit
            # below.
            await org_lock(db, sms.organization_id)
            messaging_service_sid = await provider.ensure_messaging_service(
                sms.organization_id,
                await org_messaging_service_sid(db, sms.organization_id, sms.provider),
            )
        result = await provider.send_sms(
            from_e164=sms.from_e164,
            to_e164=sms.to_e164,
            body=sms.body,
            status_callback_url=callback_url,
            messaging_service_sid=messaging_service_sid,
        )
    except Exception:
        logger.warning("sms send failed for sms_id=%s", sms.id, exc_info=True)
        sms.status = "failed"
        db.add(
            SmsEvent(
                sms_id=sms.id,
                organization_id=sms.organization_id,
                kind="state_change",
                payload={"from": "queued", "to": "failed", "reason": "provider_error"},
            )
        )
        await fanout_sms_event(
            db,
            organization_id=sms.organization_id,
            event_type="sms.failed",
            event_id=sms.id,
            data={
                "id": str(sms.id),
                "to": sms.to_e164,
                "from": sms.from_e164,
                "status": "failed",
                "reason": "provider_error",
            },
        )
        await db.commit()
        return "provider_error"

    # A carrier rejection surfaces via error_code AND/OR a failure status
    # (base.ProviderSmsResult contract: "status reflecting the failure").
    # Check both so a provider that reports failure by status alone isn't
    # recorded as sent and billed.
    carrier_rejected = result.error_code is not None or result.status.lower() in {
        "failed",
        "undelivered",
    }
    new_status = "failed" if carrier_rejected else "sent"
    sms.status = new_status
    sms.provider_message_sid = result.provider_message_sid
    sms.segment_count = result.segment_count
    sms.error_code = result.error_code
    # ``sent_at`` means "the carrier accepted the message" — a rejected
    # send keeps it NULL, matching emails (sent_at only with status='sent')
    # and this route's own transport-failure branch above.
    if not carrier_rejected:
        sms.sent_at = datetime.now(timezone.utc)
    event_payload: dict[str, Any] = {"from": "queued", "to": new_status}
    if carrier_rejected:
        event_payload["error_code"] = result.error_code
    db.add(
        SmsEvent(
            sms_id=sms.id,
            organization_id=sms.organization_id,
            kind="state_change",
            payload=event_payload,
        )
    )
    if carrier_rejected:
        await fanout_sms_event(
            db,
            organization_id=sms.organization_id,
            event_type="sms.failed",
            event_id=sms.id,
            data={
                "id": str(sms.id),
                "to": sms.to_e164,
                "from": sms.from_e164,
                "status": "failed",
                "error_code": result.error_code,
            },
        )
    await db.commit()

    if carrier_rejected:
        return result.error_code or "carrier_rejected"

    if not billable:
        return None

    tier = classify_pricing_tier(sms.to_e164)
    await write_usage_event(
        organization_id=sms.organization_id,
        channel="sms",
        units=sms.segment_count,
        ref=f"sms:{sms.id}:{tier}",
    )
    return None


_NO_SMS_NUMBER = (
    "no dedicated SMS-capable phone number on this organization; "
    "SMS requires a dedicated number, not the shared voice pool"
)


async def _with_trial_hint(
    db: AsyncSession, organization_id: UUID, to: str, msg: str
) -> str:
    """``msg``, plus how to send a trial text when the workspace can still
    send one to this US destination."""
    if is_us(to) and await trial_hint_applies(db, organization_id):
        return f"{msg} {TRIAL_HINT}"
    return msg


@router.post(
    "",
    response_model=SmsResponse,
    status_code=http_status.HTTP_201_CREATED,
    responses={
        **merge_rate_limited_responses(
            RATE_LIMITED_RESPONSES, GENERAL_RATE_LIMITED_RESPONSES
        ),
        **FUNDS_RESPONSES,
    },
)
async def create_sms(
    body: SmsCreate,
    response: Response,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    idem: Annotated[IdempotencyContext | None, Depends(idempotency_dep)] = None,
) -> SmsResponse:
    """Send an outbound SMS.

    Sends synchronously — the response reports the final status (sent or
    failed), not a queued placeholder, though delivery confirmation from
    the carrier can still arrive later as a webhook or GET /v1/sms/{sms_id}
    update. An explicit from resolves a dedicated number; otherwise Hail
    picks an alphanumeric sender ID where the destination corridor allows
    it, or requires a dedicated SMS-capable number otherwise. Requires
    recipient_consent=true on the request body; Hail does not verify
    lawful basis to contact the recipient, the caller warrants it.

    A workspace without approved US registration can omit from to send up
    to 25 trial texts to its verified trial phone.
    """
    if idem is not None and idem.is_replay:
        cached_id, cached = replay_cached(
            idem, response, request, resource_prefix="/sms"
        )
        actor_user_id, actor_kind = actor_of(principal)
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="sms.create.replayed",
            resource_type="sms",
            resource_id=cached_id,
            payload={"to": cached.get("to_e164"), "from": cached.get("from_e164")},
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
        )
        return SmsResponse.model_validate(cached)

    try:
        enforce_consent(
            recipient_consent=body.recipient_consent,
            consent_source=body.consent_source,
            message_type=body.message_type,
        )
    except HTTPException as exc:
        raise await cache_failure(idem, exc) from None

    gate = await check_sms_allowed(db, principal.organization_id, body.to)
    if not gate.allowed:
        actor_user_id, actor_kind = actor_of(principal)
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="sms.blocked",
            resource_type="sms",
            resource_id=None,
            payload={"to": body.to, "reason": gate.reason, "checks": gate.checks},
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
        )
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN, detail=gate.reason
            ),
        )

    await require_funds(db, principal, idem)

    # The dedicated-number requirement is conditional on the destination
    # corridor (Task 5). For alphanumeric-eligible corridors with no explicit
    # ``from``, the org's Sender ID is used and NO dedicated number is needed;
    # otherwise a dedicated SMS-capable number is still required (Decision 6).
    #
    # An explicit ``from`` always resolves a dedicated number regardless of
    # corridor, so the Sender ID lookup is skipped entirely in that case — no
    # SmsSenderIdentity query on explicit-``from`` sends.
    from_number = None
    from_e164 = None
    sms_body = body.body
    metadata = dict(body.metadata)
    # No ``from`` and a US recipient: prefer a number on an approved US SMS
    # registration, the only kind the registration gate lets through.
    registered = (
        await registered_sms_number(db, principal.organization_id)
        if body.from_ is None and is_us(body.to)
        else None
    )
    # No ``from``, a US recipient and no registered number: the workspace's
    # own number and its gate answer, found once and before the trial claim
    # takes the workspace lock (the gate can ask the carrier).
    own_checked = body.from_ is None and is_us(body.to) and registered is None
    own_number = None
    own_block = None
    if own_checked:
        own_number = await resolve_org_number(
            db, principal.organization_id, None, capability="sms"
        )
        if own_number is not None:
            own_block = await us_registration_block(
                db, principal.organization_id, own_number, body.to
            )
    # Trial texts: ``from`` names the trial number, or no ``from`` and the
    # own number is missing or blocked, as at the agent send sites.
    # ``claim_trial_send`` decides the rest.
    trial = None
    trial_from = body.from_ is not None and body.from_ == settings.hail_sms_trial_number
    if trial_from or (own_checked and (own_number is None or own_block is not None)):
        try:
            trial = await claim_trial_send(
                db,
                principal.organization_id,
                to=body.to,
                body=body.body,
                limit=1600,
                explicit_from=body.from_,
            )
        except TrialBodyTooLong as exc:
            # The claim holds the workspace lock: release it before answering.
            await db.rollback()
            raise await cache_failure(
                idem, unprocessable(str(exc), loc=["body", "body"])
            ) from None
        if trial is None and db.in_transaction():
            # A refused claim may hold the workspace lock (all texts used,
            # phone changed). Release it before the checks below, which can
            # call the carrier. The rollback drops nothing: this transaction
            # has only read so far (authentication commits its own key
            # stamp, and the agent caps below write their log after it).
            # test_refused_trial_claim_rolls_back_only_reads checks this.
            # The rollback expires ``own_number``; below, only its gate
            # answer is read.
            await db.rollback()
    # The agent caps log the send, so they run after any rollback above;
    # a trial send logs it under the claim's lock.
    try:
        await require_agent_send_allowed(db, principal, "sms", [body.to], idem)
    except HTTPException:
        if trial is not None:
            # Release the claim's workspace lock before answering.
            await db.rollback()
        raise
    if trial is not None:
        # The claim holds the workspace lock: nothing commits before the
        # insert and commit below, in this same transaction.
        from_number = trial.number
        from_e164 = trial.number.e164
        sms_body = trial.body
        metadata = {**metadata, **trial.metadata}
    elif body.from_ is None:
        sender_id_row = (
            await db.execute(
                select(SmsSenderIdentity).where(
                    SmsSenderIdentity.organization_id == principal.organization_id
                )
            )
        ).scalar_one_or_none()
        resolution = resolve_sender(
            body.to,
            custom_sender_id=sender_id_row.custom_sender_id if sender_id_row else None,
            carrier=settings.sender_id_sms_carrier,
        )
        if resolution.kind == "alphanumeric":
            # Alphanumeric corridor with no explicit ``from`` → send from the
            # resolved Sender ID; no dedicated number is provisioned.
            from_e164 = resolution.sender_id

    if from_e164 is None and own_checked:
        # The own number was resolved and gated above.
        if own_number is None:
            raise await cache_failure(
                idem,
                unprocessable(
                    await _with_trial_hint(
                        db, principal.organization_id, body.to, _NO_SMS_NUMBER
                    ),
                    loc=["body", "from"],
                ),
            )
        if own_block:
            msg = await _with_trial_hint(
                db, principal.organization_id, body.to, own_block
            )
            raise await cache_failure(idem, unprocessable(msg, loc=["body", "from"]))
        from_number = own_number
        from_e164 = own_number.e164
    elif from_e164 is None:
        # Explicit ``from``, or a corridor that still requires a dedicated
        # SMS-capable number.
        from_number = registered or await resolve_org_number(
            db, principal.organization_id, body.from_, capability="sms"
        )
        if from_number is None:
            if body.from_ is not None:
                # ``from`` is the trial number and the claim was refused:
                # name why, not that the number belongs to someone else.
                reason = (
                    await trial_refusal_reason(
                        db, principal.organization_id, to=body.to
                    )
                    if trial_from
                    else None
                )
                msg = reason or (
                    f"phone number {body.from_} is not registered to this "
                    "organization, is not active, or lacks the sms capability"
                )
            else:
                msg = await _with_trial_hint(
                    db, principal.organization_id, body.to, _NO_SMS_NUMBER
                )
            raise await cache_failure(idem, unprocessable(msg, loc=["body", "from"]))
        block = await us_registration_block(
            db, principal.organization_id, from_number, body.to
        )
        if block:
            msg = await _with_trial_hint(db, principal.organization_id, body.to, block)
            raise await cache_failure(idem, unprocessable(msg, loc=["body", "from"]))
        from_e164 = from_number.e164

    sms = Sms(
        organization_id=principal.organization_id,
        provider=(
            from_number.provider
            if from_number is not None
            else settings.sender_id_sms_carrier
        ),
        from_number_id=from_number.id if from_number is not None else None,
        from_e164=from_e164,
        to_e164=body.to,
        direction="outbound",
        status="queued",
        body=sms_body,
        metadata_=metadata,
    )
    # No refresh needed: expire_on_commit=False and asyncpg INSERT..RETURNING
    # (eager_defaults) leave id/timestamps populated on the instance.
    db.add(sms)
    await db.commit()
    tag_context(
        channel="sms",
        direction="outbound",
        sms_id=sms.id,
        from_=sms.from_e164,
        to=sms.to_e164,
    )

    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="sms.create",
        resource_type="sms",
        resource_id=sms.id,
        payload={
            "to": sms.to_e164,
            "from": sms.from_e164,
            "recipient_consent": body.recipient_consent,
            "consent_source": body.consent_source,
            "consent_obtained_at": isoformat_or_none(body.consent_obtained_at),
            "message_type": body.message_type,
            "compliance": gate.checks,
        },
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )

    # Provider send — best-effort with status reconciliation.
    err = await deliver_sms(db, sms)
    if err == "provider_error":
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail=_SMS_SEND_FAILED_DETAIL,
            ),
        )
    # carrier rejection: row already reconciled to failed; fall through and
    # return the SmsResponse exactly as before.

    response.headers["Location"] = f"{request_mount_prefix(request)}/sms/{sms.id}"
    sms_response = SmsResponse.model_validate(sms)

    if idem is not None:
        await idem.store(
            status_code=http_status.HTTP_201_CREATED,
            body=sms_response.model_dump(mode="json"),
        )

    return sms_response


# Once an Sms reaches any of these, it is done: no later callback — however
# delayed or out-of-order the carrier's at-least-once redelivery makes it —
# may change status or fan out again.
TERMINAL_SMS_STATUSES = frozenset({"delivered", "undelivered", "failed"})


async def apply_sms_status(
    db: AsyncSession,
    sms: Sms,
    new_status: str,
    *,
    error_code: str | None = None,
) -> None:
    """Record a status change and fan out terminal ones. Caller commits.

    A carrier ``error_code`` (a Twilio 21610 after a STOP, for example) is
    kept on the row when the row has none, and rides on the event and fan-out.
    """
    prior = sms.status
    sms.status = new_status
    if error_code is not None and not sms.error_code:
        sms.error_code = error_code
    event_payload: dict[str, str] = {"from": prior, "to": new_status}
    data = {
        "id": str(sms.id),
        "to": sms.to_e164,
        "from": sms.from_e164,
        "status": new_status,
    }
    if error_code is not None:
        event_payload["error_code"] = error_code
        data["error_code"] = error_code
    db.add(
        SmsEvent(
            sms_id=sms.id,
            organization_id=sms.organization_id,
            kind="state_change",
            payload=event_payload,
        )
    )
    if new_status in TERMINAL_SMS_STATUSES:
        await fanout_sms_event(
            db,
            organization_id=sms.organization_id,
            event_type=f"sms.{new_status}",
            event_id=sms.id,
            data=data,
        )


@router.get(
    "/suppressions",
    response_model=SuppressionListResponse,
)
async def list_sms_suppressions(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
) -> SuppressionListResponse:
    """List SMS numbers suppressed from receiving messages, newest first.

    Cursor-paginated. A suppressed number blocks POST /v1/sms sends to it
    with a 403 until removed via DELETE /v1/sms/suppressions/{number}.
    """
    return await list_channel_suppressions(
        db,
        organization_id=principal.organization_id,
        channel="sms",
        cursor=cursor,
        limit=limit,
    )


@router.delete(
    "/suppressions/{number}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
async def delete_sms_suppression(
    number: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Remove one number from the SMS suppression list, re-allowing sends to it.

    Does not itself constitute renewed consent — the caller is responsible
    for having a lawful basis (e.g. a fresh opt-in) before sending again.
    Returns 404 if the number was not suppressed.
    """
    await delete_channel_suppression(
        db, organization_id=principal.organization_id, recipient=number, channel="sms"
    )


@router.get(
    "/sender-id",
    response_model=SenderIdResponse,
)
async def get_sender_id(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SenderIdResponse:
    """Get the org's custom alphanumeric SMS sender ID, if any is set.

    effective_default is the platform sender id used for
    alphanumeric-eligible corridors when custom_sender_id is null.
    """
    stmt = select(SmsSenderIdentity).where(
        SmsSenderIdentity.organization_id == principal.organization_id
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    return SenderIdResponse(
        custom_sender_id=row.custom_sender_id if row else None,
        effective_default=PLATFORM_DEFAULT_SENDER_ID,
    )


@router.patch(
    "/sender-id",
    response_model=SenderIdResponse,
)
async def patch_sender_id(
    body: SenderIdPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SenderIdResponse:
    """Set or clear the org's custom alphanumeric SMS sender ID.

    Pass custom_sender_id=null to clear it and fall back to the platform
    default sender id. Only affects alphanumeric-eligible corridors — a
    corridor requiring a dedicated number is unaffected.
    """
    if body.custom_sender_id is None:
        # Clear: unconditional delete (a no-op when no row exists), so there is
        # no read-then-delete window.
        await db.execute(
            delete(SmsSenderIdentity).where(
                SmsSenderIdentity.organization_id == principal.organization_id
            )
        )
        await db.commit()
        return SenderIdResponse(
            custom_sender_id=None, effective_default=PLATFORM_DEFAULT_SENDER_ID
        )

    # Set: atomic upsert keyed on the org PK, so two concurrent first-writes
    # can't both INSERT and 500 on a duplicate-key IntegrityError (one round
    # trip instead of SELECT-then-INSERT/UPDATE).
    await db.execute(
        pg_insert(SmsSenderIdentity)
        .values(
            organization_id=principal.organization_id,
            custom_sender_id=body.custom_sender_id,
        )
        .on_conflict_do_update(
            index_elements=["organization_id"],
            set_={"custom_sender_id": body.custom_sender_id, "updated_at": func.now()},
        )
    )
    await db.commit()
    return SenderIdResponse(
        custom_sender_id=body.custom_sender_id,
        effective_default=PLATFORM_DEFAULT_SENDER_ID,
    )


@router.get(
    "/{sms_id}",
    response_model=SmsResponse,
)
async def get_sms(
    sms_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SmsResponse:
    """Fetch one SMS by id, including its current status.

    Org-scoped: returns 404 for an SMS belonging to a different
    organization.
    """
    stmt = select(Sms).where(
        Sms.id == sms_id,
        Sms.organization_id == principal.organization_id,
    )
    sms = (await db.execute(stmt)).scalar_one_or_none()
    if sms is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="sms not found"
        )
    return SmsResponse.model_validate(sms)


@router.get(
    "",
    response_model=SmsListResponse,
)
async def list_sms(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
    status: SmsStatus | None = Query(default=None),
    to: str | None = Query(default=None),
) -> SmsListResponse:
    """List SMS messages for the caller's organization, newest first.

    Cursor-paginated: pass the returned next_cursor to fetch the next page;
    a null next_cursor means there are no more results. Filter by status or
    destination number (to) to narrow the list.
    """
    stmt = select(Sms).where(Sms.organization_id == principal.organization_id)
    if status is not None:
        stmt = stmt.where(Sms.status == status)
    if to is not None:
        stmt = stmt.where(Sms.to_e164 == to)
    rows, next_cursor = await fetch_cursor_page(
        db, stmt, Sms.created_at, Sms.id, cursor=cursor, limit=limit, newest_first=True
    )
    return SmsListResponse(
        items=[SmsResponse.model_validate(s) for s in rows],
        next_cursor=next_cursor,
    )


__all__ = [
    "TERMINAL_SMS_STATUSES",
    "apply_sms_status",
    "deliver_sms",
    "router",
]
