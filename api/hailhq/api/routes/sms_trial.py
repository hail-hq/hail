"""Routes for US trial texting.

GET /sms/trial - the workspace's trial status and texts left.
POST /sms/trial/phone - text a code to the workspace's own phone.
POST /sms/trial/phone/confirm - check the code; the phone then receives
trial texts.

The phone routes are for console sessions only: the consent tick and its
wording live in the console.
"""

from __future__ import annotations

import logging
import math
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from hailhq.api.audit import actor_of, write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.ratelimit import (
    GENERAL_RATE_LIMITED_RESPONSES,
    merge_rate_limited_responses,
)
from hailhq.core.carrier_routing import sms_route
from hailhq.core.db import get_session
from hailhq.core.schemas import (
    SmsTrialPhoneConfirm,
    SmsTrialPhoneStart,
    SmsTrialResponse,
)
from hailhq.core.sms_trial import (
    SMS_TRIAL_LIMIT,
    CodeBudgetExceeded,
    CodeExpired,
    NoPendingCode,
    PhoneIsHailNumber,
    PhoneNotUs,
    PhoneStopped,
    PhoneTaken,
    WrongCode,
    code_text,
    confirm_phone,
    get_trial_phone,
    start_phone_proof,
    trial_configured,
    trial_sender,
    trial_texts_used,
)
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/sms/trial", tags=["sms"], responses=GENERAL_RATE_LIMITED_RESPONSES
)

_CONSOLE_ONLY = "Set up trial texting in the console."
_NOT_AVAILABLE = "Trial texting is not available yet."
_STOPPED = "This phone replied STOP to Hail. Reply START, then try again."
_REJECTED = "Hail could not text this phone. Check the number."
_SEND_FAILED = "Hail could not send the code. Try again."
# The carrier's error for a recipient that replied STOP to the sender.
_STOP_ERROR_CODE = "21610"

_CONSOLE_ONLY_RESPONSE: dict = {
    403: {"description": "Only a signed-in console session can set the phone."}
}


async def _trial_status(db: AsyncSession, organization_id: UUID) -> SmsTrialResponse:
    row = await get_trial_phone(db, organization_id)
    used = await trial_texts_used(db, organization_id)
    return SmsTrialResponse(
        configured=await trial_configured(db),
        available=await trial_sender(db) is not None,
        status=row.status if row is not None else "none",
        phone=row.phone_e164 if row is not None else None,
        pending_phone=row.pending_phone_e164 if row is not None else None,
        code_expires_at=row.code_expires_at if row is not None else None,
        texts_used=used,
        texts_limit=SMS_TRIAL_LIMIT,
        texts_left=max(SMS_TRIAL_LIMIT - used, 0),
    )


def _require_console(principal: Principal) -> None:
    if not principal.console_session:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN, detail=_CONSOLE_ONLY
        )


@router.get("", response_model=SmsTrialResponse)
async def get_sms_trial(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SmsTrialResponse:
    """Read the workspace's US trial texting status.

    Shows whether trial texting is set up and can send now, the workspace's
    trial phone and its status, and how many of the 25 trial texts are left.
    """
    return await _trial_status(db, principal.organization_id)


@router.post(
    "/phone",
    response_model=SmsTrialResponse,
    responses={
        **_CONSOLE_ONLY_RESPONSE,
        409: {
            "description": (
                "Trial texting is not available yet, or another workspace uses "
                "this phone."
            )
        },
        422: {
            "description": (
                "The phone is not a US number, is a Hail number, replied STOP "
                "to Hail, or could not be texted."
            )
        },
        **merge_rate_limited_responses(
            {
                429: {
                    "description": (
                        "The workspace asked for, or the phone got, 5 codes "
                        "in the last 24 hours."
                    ),
                    "headers": {
                        "Retry-After": {
                            "description": "Seconds until a new code can be sent.",
                            "schema": {"type": "integer"},
                        }
                    },
                }
            },
            GENERAL_RATE_LIMITED_RESPONSES,
        ),
        502: {
            "description": (
                "Hail could not reach the carrier. The code may still arrive "
                "and counts toward the 5 codes."
            )
        },
    },
)
async def start_sms_trial_phone(
    body: SmsTrialPhoneStart,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SmsTrialResponse:
    """Text a 6-digit code to the workspace's own US mobile.

    The code expires in 10 minutes. A new code replaces the old one. A
    verified phone keeps getting trial texts until the new phone is
    confirmed; until then the new phone shows as pending_phone. Asking
    again for the verified phone drops the waiting new phone. A
    workspace or a phone gets at most 5 codes in 24 hours. Code texts are
    free and do not count toward the 25 trial texts. Console sessions only.
    """
    _require_console(principal)
    number = await trial_sender(db)
    if number is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=_NOT_AVAILABLE
        )
    try:
        issued = await start_phone_proof(
            db,
            principal.organization_id,
            phone=body.phone,
            consent_version=body.consent_version,
            user_id=principal.user_id,
        )
    except (PhoneNotUs, PhoneIsHailNumber, PhoneStopped) as exc:
        await db.rollback()
        raise unprocessable(str(exc), loc=["body", "phone"]) from exc
    except PhoneTaken as exc:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except CodeBudgetExceeded as exc:
        await db.rollback()
        retry = max(1, math.ceil(exc.retry_after.total_seconds()))
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(retry)},
        ) from exc

    if issued.code is None:
        # Already verified with this phone: nothing to text. Commit drops a
        # new phone that was waiting for its code.
        await db.commit()
        return await _trial_status(db, principal.organization_id)

    # start_phone_proof holds the workspace and phone locks until the commit,
    # so only the send runs between the claim and the commit. Text and audit
    # the phone as stored (canonical), never the request's spelling: the
    # new phone, which may wait beside a verified one.
    phone = issued.phone
    row_id = issued.row.id
    send_error: Exception | None = None
    try:
        result = await sms_route(number.provider).send_sms(
            from_e164=number.e164,
            to_e164=phone,
            body=code_text(issued.code),
            status_callback_url=None,
        )
    except Exception as exc:
        # A timeout or a dropped connection may come after the carrier sent
        # the code, so keep the code and its use of the 5: otherwise a script
        # gets unlimited code texts while the carrier is slow.
        logger.warning(
            "trial code send failed for organization_id=%s",
            principal.organization_id,
            exc_info=True,
        )
        send_error = exc
    else:
        if result.error_code is not None or result.status.lower() in {
            "failed",
            "undelivered",
        }:
            # The carrier refused: the code never reached the phone.
            await db.rollback()
            msg = _STOPPED if result.error_code == _STOP_ERROR_CODE else _REJECTED
            raise unprocessable(msg, loc=["body", "phone"])

    await db.commit()

    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="sms.trial.code_sent",
        resource_type="sms_trial_phone",
        resource_id=row_id,
        payload={"phone": phone},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    if send_error is not None:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY, detail=_SEND_FAILED
        ) from send_error
    return await _trial_status(db, principal.organization_id)


@router.post(
    "/phone/confirm",
    response_model=SmsTrialResponse,
    responses={
        **_CONSOLE_ONLY_RESPONSE,
        409: {
            "description": (
                "Another workspace uses this phone for trial texts. Use a "
                "different phone."
            )
        },
        422: {
            "description": (
                "No code is open, the code expired, or the code is not "
                "correct. After 5 wrong tries, send a new code."
            )
        },
    },
)
async def confirm_sms_trial_phone(
    body: SmsTrialPhoneConfirm,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SmsTrialResponse:
    """Confirm the workspace's trial phone with the texted code.

    On success the phone receives trial texts. Each code allows 5 tries.
    A phone can be the trial phone of only one workspace. Console sessions
    only.
    """
    _require_console(principal)
    try:
        row = await confirm_phone(db, principal.organization_id, body.code)
    except WrongCode as exc:
        # The try count must outlive this request, or a retry loop gets
        # unlimited tries.
        await db.commit()
        raise unprocessable(str(exc), loc=["body", "code"]) from exc
    except (NoPendingCode, CodeExpired) as exc:
        await db.rollback()
        raise unprocessable(str(exc), loc=["body", "code"]) from exc
    except PhoneTaken as exc:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    row_id, phone = row.id, row.phone_e164
    await db.commit()

    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="sms.trial.verified",
        resource_type="sms_trial_phone",
        resource_id=row_id,
        payload={"phone": phone},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return await _trial_status(db, principal.organization_id)
