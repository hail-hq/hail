"""Carrier verification: collect a customer's details and documents, build the
carrier-side record and submit it. A superadmin can still approve a draft the
carrier did not take at creation.

Number-verification files and details pass through in memory to the carrier.
SMS applications keep submitted text fields and address privately for repair;
only the organization-scoped application endpoint returns them. Files sent as
``file.<slot>`` parts are never stored. A document uploaded with an upload link
(``POST /uploads``, named here as ``upload.<slot>``) is kept in the mail bucket
until a verification using it is saved, then deleted; the GC deletes any left
1 hour after upload. Other identifying data is not written to disk or logs, except
the name of the person or company (``subject_name``), which the
database keeps so the customer can tell verifications apart. It is never
logged. One more exception: when the carrier rejects a verification, its own reason is
kept in the audit row (``payload.carrier_reason``), never in logs and never
shown to the customer. That text can quote a value the customer sent.
The multipart body is parsed here with an in-memory parser and a hard size cap,
so no part is ever spooled to a temporary file.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, NamedTuple
from uuid import UUID

import phonenumbers
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi import status as http_status
from hailhq.api import verification_decisions  # noqa: F401 (session listeners)
from hailhq.api.audit import actor_of, write_audit_log
from hailhq.api.deps import Principal, get_current_principal, get_s3_mail_optional
from hailhq.api.errors import unprocessable
from hailhq.api.funds import BILLING_URL
from hailhq.api.multipart import parse_capped_form
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.api.sms_registration import (
    attach_org_numbers,
    detach_org_numbers,
    reconcile_approved_registrations,
)
from hailhq.api.superadmin import require_superadmin
from hailhq.core import (
    carrier_routing,
    sms_registration_fees,
    sms_setup,
    verification_hold,
)
from hailhq.core.billing import get_balance_cents, get_ledger_cents
from hailhq.core.carrier_offer import Approval
from hailhq.core.config import SELF_HOSTED_ORG_ID, settings
from hailhq.core.db import get_session, org_lock, try_org_lock
from hailhq.core.file_uploads import (
    VERIFICATION_FILE_TYPES,
    VERIFICATION_MAX_FILE_BYTES,
    VERIFICATION_UPLOAD_TTL,
    delete_upload_objects,
)
from hailhq.core.internal_webhook import (
    notify_sms_registration_changed,
    rate_sms_registration,
    tollfree_verification_supported,
)
from hailhq.core.models import (
    AccountCredit,
    CarrierVerification,
    FileUpload,
    NumberOffer,
    PhoneNumber,
)
from hailhq.core.providers.verification import (
    Address,
    DocumentInput,
    Problem,
    ProviderStatus,
    Requirements,
    SmsRegistrationProvider,
    SubjectType,
    SubmitRefused,
    UnsupportedSubjectType,
    UploadedFile,
    VerificationProvider,
    VerificationProviderError,
    VerificationRequirements,
    get_verification_provider,
    length_problems,
)
from hailhq.core.providers.verification.forms import validate_input
from hailhq.core.providers.verification.sms_form import campaign_text
from hailhq.core.providers.verification.sms_review import same_sender
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.schemas import (
    SmsApplicationAmendRequest,
    SmsApplicationResponse,
    VerificationCheckFile,
    VerificationCheckResponse,
    VerificationResponse,
)
from hailhq.core.sms_registration_gate import US_SMS_COUNTRY_CODES
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import (
    DateTime,
    Row,
    String,
    case,
    cast,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified, get_history

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = VERIFICATION_MAX_FILE_BYTES
MAX_REQUEST_BYTES = 30 * 1024 * 1024
ALLOWED_FILE_TYPES = set(VERIFICATION_FILE_TYPES)
NUMBER_TYPES = {"local", "mobile", "toll_free", "national"}
_READ_CHUNK = 1024 * 1024
_LIVE_STATES = ("draft", "awaiting_review", "submitting", "submitted", "approved")
_POLLED_STATES = ("submitting", "submitted")
_POLL_INTERVAL_S = 60
# Wrong confirmation codes allowed per code the carrier sends. The count
# lives in provider_refs['confirm_attempts']; the plug-in resets it to 0
# whenever it sends a new code.
_MAX_CONFIRM_ATTEMPTS = 5
# A row left in 'submitting' this long was cut off mid-submit (see
# _submit_row). Reads then ask the carrier what really happened.
_SUBMITTING_STUCK_AFTER = timedelta(minutes=5)
# When this process last asked the carrier about a submitted verification.
_last_polled: dict[UUID, float] = {}

PURPOSES = {"number", "sms"}

# An approved number verification keeps its credit set aside this long for
# the purchase; after that the sweeper gives it back.
HOLD_AFTER_APPROVAL_DAYS = 30

# (name, purpose) -> plug-in
Registry = Callable[[str, str], VerificationProvider | None]


def get_verification_registry() -> Registry:
    """Dependency so tests can swap in a fake carrier."""
    return get_verification_provider


def _hold_402(cents: int) -> HTTPException:
    """402 for a number verification the balance cannot set aside."""
    return HTTPException(
        status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
        detail=(
            "not enough credit to start verification; setup and the first "
            f"month cost ${cents / 100:.2f}; add credit first"
        ),
    )


def _quote_required(where: str) -> HTTPException:
    """422 for a number verification sent without the offer's quote_id."""
    return unprocessable(
        "quote_id is required for purpose=number: get offers from "
        "POST /numbers/quotes and pass the quote_id of the offer to verify for",
        loc=[where, "quote_id"],
    )


async def _quote_offer(
    db: AsyncSession,
    organization_id: UUID,
    quote_id: UUID,
    country_code: str,
    number_type: str,
    where: str,
) -> dict:
    """The stored offer of a number quote. Its ``provider`` is the carrier,
    so a client can verify for an offer without knowing which carrier backs
    it. The quote must be for the same country and number type as the
    request."""
    offer = (
        await db.execute(
            select(NumberOffer.offer).where(
                NumberOffer.id == quote_id,
                NumberOffer.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if offer is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Number quote not found; quote again and pass the new quote_id",
        )
    if (
        offer.get("country_code", "").upper() != country_code.upper()
        or offer.get("number_type") != number_type
    ):
        raise unprocessable(
            "quote_id is for a different country_code or number_type",
            loc=[where, "quote_id"],
        )
    return offer


async def _sms_number(
    db: AsyncSession, organization_id: UUID, number_id: UUID
) -> PhoneNumber:
    number = (
        await db.execute(
            select(PhoneNumber).where(
                PhoneNumber.id == number_id,
                PhoneNumber.organization_id == organization_id,
                PhoneNumber.provisioning_state == "active",
                PhoneNumber.capabilities.any("sms"),
            )
        )
    ).scalar_one_or_none()
    if number is None or not (
        (number.country_code in US_SMS_COUNTRY_CODES and number.number_type == "local")
        or (number.country_code in {"US", "CA"} and number.number_type == "toll_free")
    ):
        raise HTTPException(
            status_code=404,
            detail="Number not found among your active US local or US/Canadian toll-free numbers that can text",
        )
    return number


async def _sms_carriers(
    db: AsyncSession, organization_id: UUID, number_id: UUID | None
) -> list[str]:
    if number_id is not None:
        return [(await _sms_number(db, organization_id, number_id)).provider]
    query = select(PhoneNumber.provider).where(
        PhoneNumber.organization_id == organization_id,
        PhoneNumber.provisioning_state == "active",
        PhoneNumber.country_code.in_(US_SMS_COUNTRY_CODES),
        PhoneNumber.number_type == "local",
        PhoneNumber.capabilities.any("sms"),
    )
    return list((await db.execute(query.distinct())).scalars().all())


def _one_carrier(carriers: list[str], where: str) -> str:
    """422 when the org's US numbers are with more than one carrier."""
    if len(carriers) > 1:
        raise unprocessable(
            "Your US numbers are with more than one carrier. Pass number_id: "
            "the id of the number to register.",
            loc=[where, "number_id"],
        )
    return carriers[0]


async def _sms_provider_name(
    db: AsyncSession,
    organization_id: UUID,
    number_id: UUID | None,
    where: str,
    *,
    country_code: str | None = None,
    number_type: str | None = None,
) -> str:
    """The carrier for an SMS registration: the carrier of the number passed
    as ``number_id``, else the carrier of the org's active US long codes that
    can text."""
    if number_type is not None and not (
        (country_code == "US" and number_type == "local")
        or (country_code in {"US", "CA"} and number_type == "toll_free")
    ):
        raise unprocessable(
            "SMS registration supports US local and US/Canadian toll-free numbers.",
            loc=[where, "country_code"],
        )
    if number_type == "toll_free" and number_id is None:
        raise unprocessable(
            "Choose the toll-free number to verify with number_id.",
            loc=[where, "number_id"],
        )
    if number_id is not None:
        number = await _sms_number(db, organization_id, number_id)
        expected_country = (
            "US" if number.number_type == "local" else number.country_code
        )
        if (country_code is not None and country_code != expected_country) or (
            number_type is not None and number_type != number.number_type
        ):
            raise unprocessable(
                "country_code and number_type must match the selected number.",
                loc=[where, "number_id"],
            )
        tf_carrier = carrier_routing.CARRIERS.get(number.provider)
        if number.number_type == "toll_free" and not (
            tf_carrier and tf_carrier.toll_free_sms_verification
        ):
            raise unprocessable(
                "Toll-free texting verification is not available for this number. Contact support.",
                loc=[where, "number_id"],
            )
        return number.provider
    carriers = await _sms_carriers(db, organization_id, number_id)
    if not carriers:
        raise unprocessable(
            "Buy a US number first, then register it for texting.",
            loc=[where, "number_id"],
        )
    return _one_carrier(carriers, where)


router = APIRouter(
    prefix="/verifications",
    tags=["verifications"],
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)

# Not in the public OpenAPI spec. Every route needs the superadmin role.
admin_router = APIRouter(
    prefix="/admin/verifications",
    tags=["admin"],
    include_in_schema=False,
    dependencies=[Depends(require_superadmin)],
)


def _provider_or_404(
    registry: Registry, name: str, purpose: str = "number"
) -> VerificationProvider:
    provider = registry(name, purpose)
    if provider is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="no verification provider is configured for this carrier",
        )
    return provider


def _log_carrier_refusal(
    problems: list[Problem],
    organization_id: UUID,
    verification_id: UUID | None,
    carrier: str,
) -> None:
    """Log which fields the carrier refused, for the operator. Never the
    carrier's own text: it can quote the customer's personal data, and logs
    leave this server. The customer gets ``Problem.message``."""
    for p in problems:
        logger.warning(
            "carrier %s refused verification details (org %s, verification %s, "
            "field %r)",
            carrier,
            organization_id,
            verification_id,
            p.field,
        )


def _wiped_refs(row: CarrierVerification) -> dict:
    """``provider_refs`` once emptied. A filing that reused a brand keeps the
    mark: the fee rater reads it on every sweep, and without it would bill
    the submission."""
    refs = row.provider_refs or {}
    kept = {"brand_reused": True} if refs.get("brand_reused") else {}
    # Expired answers are not carried over: a dismissal stamps a new
    # ``cancelled_at``, which would make them readable for 30 more days.
    if (
        row.purpose == "sms"
        and isinstance(refs.get("application"), dict)
        and not _application_expired(row, datetime.now(timezone.utc))
    ):
        kept["application"] = copy.deepcopy(refs["application"])
    return kept


async def _kept_brand(
    db: AsyncSession, organization_id: UUID, provider_name: str, subject_type: str
) -> str | None:
    """The brand the organization's last filing left at the carrier for the
    next one (``discard_rejected``). Only the last filing counts: once a
    newer filing exists, an older kept brand may be in use or gone."""
    row = (
        await db.execute(
            select(CarrierVerification.state, CarrierVerification.provider_refs)
            .where(
                CarrierVerification.organization_id == organization_id,
                CarrierVerification.provider == provider_name,
                CarrierVerification.purpose == "sms",
                CarrierVerification.subject_type == subject_type,
            )
            .order_by(CarrierVerification.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None or row.state != "cancelled":
        return None
    return (row.provider_refs or {}).get("kept_brand_id")


# An ISO 8601 date, or date and time, that Postgres casts to timestamptz:
# the API writes isoformat(). Month, day, hour and offset are in range.
_ISO_TIME = (
    r"^[1-9][0-9]{3}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
    r"([T ]([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9](\.[0-9]+)?)?"
    r"(Z|[+-](0[0-9]|1[0-5])(:?[0-5][0-9])?)?)?$"
)


def _refs_time(key: str):
    """``provider_refs[key]`` as a timestamptz, or NULL. Cast only an ISO
    8601 date or time (``_ISO_TIME``; the API writes isoformat() or the
    database's own now()): anything else is not a time and must not fail
    the read."""
    value = CarrierVerification.provider_refs[key].astext
    return case(
        (value.op("~")(_ISO_TIME), cast(value, DateTime(timezone=True))),
        else_=None,
    )


async def _uncharged_sms_steps(
    db: AsyncSession, organization_id: UUID, step: str
) -> list[tuple[str, str]]:
    """``(provider, subject_type)`` of the organization's US texting rows
    whose ``step`` ('submission' or 'review') happened in the last day with
    no ledger row yet: the website's rater charges each one shortly after.
    A step is settled by its debit (``sms_registration:<id>:<step>``), or by
    the one-time debit of a filing made before fees were charged per step.
    A submission happens at filing, except for a filing that reused a brand
    (never charged one); a review once the campaign is filed. Exact refs,
    so the ledger's (organization, ref) index is used."""
    if step == "submission":
        happened = [
            CarrierVerification.created_at > func.now() - text("interval '1 day'"),
            CarrierVerification.provider_refs["brand_reused"].astext.is_distinct_from(
                "true"
            ),
        ]
    else:
        filed = _refs_time("campaign_filed_at")
        happened = [filed > func.now() - text("interval '1 day'")]
    ref = func.concat("sms_registration:", cast(CarrierVerification.id, String))
    found = await db.execute(
        select(CarrierVerification.provider, CarrierVerification.subject_type).where(
            CarrierVerification.organization_id == organization_id,
            CarrierVerification.purpose == "sms",
            CarrierVerification.number_type != "toll_free",
            *happened,
            ~select(AccountCredit.id)
            .where(
                AccountCredit.organization_id == organization_id,
                AccountCredit.ref.in_([ref, func.concat(ref, f":{step}")]),
            )
            .exists(),
        )
    )
    return [(provider, subject_type) for provider, subject_type in found]


async def _unsettled_sms_filings(db: AsyncSession, organization_id: UUID) -> int:
    """US texting filings of the last day whose submission is not charged
    yet (``_uncharged_sms_steps``)."""
    return len(await _uncharged_sms_steps(db, organization_id, "submission"))


async def _record_billed_refusal(
    db: AsyncSession,
    principal: Principal,
    provider_name: str,
    sub,
    subject_name: str | None,
    billed_refs: dict,
    reason: str,
    requirements_version: str,
) -> None:
    """The carrier made and bills part of a filing that then failed (a
    brand it registered, then refused). Keep it as a rejected registration,
    so the submission is charged and support can trace the carrier id. It
    holds no live state, so it never blocks a new filing."""
    row = CarrierVerification(
        organization_id=principal.organization_id,
        provider=provider_name,
        purpose=sub.purpose,
        country_code=sub.country_code,
        number_type=sub.number_type,
        subject_type=sub.subject_type,
        subject_name=subject_name,
        state="rejected",
        rejection_reason=reason[:500],
        provider_refs={**billed_refs, "refused_at_filing": True},
        requirements_version=requirements_version,
    )
    db.add(row)
    await db.commit()
    await write_audit_log(
        principal.organization_id,
        None,
        "verification.reject",
        "carrier_verification",
        row.id,
        {"rejected_by": None, "reason": row.rejection_reason, "at_filing": True},
        actor_user_id=None,
        actor_kind="system",
    )
    _notify_billing([row])


def _problems_422(
    problems: list[Problem],
    organization_id: UUID,
    carrier: str,
    verification_id: UUID | None = None,
) -> HTTPException:
    """422 with one entry per problem: the field as ``loc`` and Hail's own
    wording as ``msg``. The carrier's text is neither returned nor logged."""
    _log_carrier_refusal(problems, organization_id, verification_id, carrier)
    return HTTPException(status_code=422, detail=_problem_details(problems))


def _problem_details(problems: list[Problem]) -> list[dict]:
    """The 422 ``detail``: one entry per problem, the field as ``loc``."""
    return [
        {
            "loc": ["body", *(p.field.split(".") if p.field else [])],
            "msg": p.message,
            "type": "verification_problem",
        }
        for p in problems
    ]


async def _requirements(
    provider: VerificationProvider,
    country_code: str,
    number_type: str,
    subject_type: SubjectType,
    where: str = "query",
) -> Requirements:
    """``where`` names the request part the inputs came from, for error paths."""
    if number_type not in NUMBER_TYPES:
        raise unprocessable("unknown number_type", loc=[where, "number_type"])
    try:
        return await provider.requirements(
            country_code.upper(), number_type, subject_type
        )
    except UnsupportedSubjectType as exc:
        error = unprocessable(str(exc), loc=[where, "subject_type"])
        error.detail[0]["ctx"] = {"subject_types": list(exc.allowed)}
        raise error from exc
    except Exception as exc:
        logger.exception("verification requirements lookup failed")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="carrier unavailable; try again later",
        ) from exc


def _pollable(row: CarrierVerification) -> bool:
    """True when the carrier has something to say about this row: it is
    submitted, or was cut off while submitting long enough ago that no approve
    can still be running."""
    if row.state not in _POLLED_STATES:
        return False
    if row.purpose == "sms" and (row.provider_refs or {}).get("needs_information"):
        return False
    if row.state == "submitting":
        return datetime.now(timezone.utc) - row.updated_at >= _SUBMITTING_STUCK_AFTER
    return True


def _poll_due(row: CarrierVerification, now: float) -> bool:
    """`_pollable` and not asked in the last minute. Marks the row as asked."""
    if not _pollable(row):
        return False
    if now - _last_polled.get(row.id, -_POLL_INTERVAL_S) < _POLL_INTERVAL_S:
        return False
    _last_polled[row.id] = now
    return True


async def _fetch_status(
    row: CarrierVerification,
    registry: Registry,
    *,
    may_file_campaign: bool,
) -> ProviderStatus | None:
    """Ask the carrier about a verification. None when there is nothing to
    apply. An SMS plug-in files the campaign inside status() and stores its
    id in the refs: ``_plugin_call`` stamps it, and keeps only the ids a
    failed call added. ``may_file_campaign`` False: the plug-in skips that
    paid filing (``_campaign_may_go``) and runs every other step."""
    provider = registry(row.provider, row.purpose)
    if provider is None:
        return None
    try:
        async with _plugin_call(row, provider):
            if isinstance(provider, SmsRegistrationProvider):
                return await provider.status(
                    row.provider_refs, may_file_campaign=may_file_campaign
                )
            return await provider.status(row.provider_refs)
    except Exception:
        logger.warning("verification status poll failed", exc_info=True)
        return None


def _apply_status(row: CarrierVerification, status: ProviderStatus | None) -> bool:
    """Copy the carrier's view onto the row. True when the row changed.

    A row stuck in 'submitting' settles here: the carrier says whether the
    submit went through ('pending' -> submitted) or not ('draft' ->
    awaiting_review, so an admin can approve again)."""
    if status is None:
        return False
    now = datetime.now(timezone.utc)
    if row.state == "submitting" and status.state in (
        "pending",
        "approved",
        "rejected",
    ):
        # The interrupted approve did submit; record when we learned that.
        row.submitted_at = now
    if status.state == "approved":
        row.state, row.approved_at, row.updated_at = "approved", now, now
    elif status.state == "rejected":
        row.state, row.rejection_reason, row.updated_at = "rejected", status.reason, now
    elif row.state == "submitting" and status.state == "pending":
        row.state, row.submitted_at, row.updated_at = "submitted", now, now
        if status.reason and (row.provider_refs or {}).get("needs_information"):
            # Held for answers on this same poll: a held row is not polled
            # again, so keep the repair guidance now.
            row.rejection_reason = status.reason
    elif row.state == "submitting" and status.state == "draft":
        row.state, row.approved_by, row.updated_at = "awaiting_review", None, now
    elif (
        status.state == "pending"
        and status.reason
        and (row.provider_refs or {}).get("needs_information")
        and row.rejection_reason != status.reason
    ):
        # A filing held for the customer's answers: say what to fix. The
        # amend route clears it.
        row.rejection_reason, row.updated_at = status.reason, now
    else:
        return False
    _last_polled.pop(row.id, None)
    return True


async def _settle_sms_approval(
    db: AsyncSession,
    row: CarrierVerification,
    status: ProviderStatus | None,
    registry: Registry,
) -> ProviderStatus | None:
    """For an SMS registration the carrier approved, attach the org's numbers.
    The row is approved even when a number fails to attach: that number is
    logged, and attach_if_registered or POST /numbers/{id}/enable-sms
    attaches it later."""
    if status is None or status.state != "approved" or row.purpose != "sms":
        return status
    provider = registry(row.provider, "sms")
    if not isinstance(provider, SmsRegistrationProvider):
        return status
    # No org lock here: this holds the verification row lock, and
    # cancel_verification takes the org lock before it writes a row, so the
    # two would wait on each other. A number the carrier already holds is
    # locked by its own row instead (sms_registration._free_number).
    attached, failed = await attach_org_numbers(db, row, provider)
    if attached or failed:
        await write_audit_log(
            row.organization_id,
            None,
            "verification.sms_attached",
            "carrier_verification",
            row.id,
            {"numbers": attached, "failed": failed},
            actor_kind="system",
        )
    return status


async def _poll_and_apply(
    db: AsyncSession,
    row: CarrierVerification,
    status: ProviderStatus | None,
    registry: Registry,
    refs_before: dict,
) -> bool:
    """Apply a fetched status. True when the row needs a commit: its state
    changed, or a plug-in call (status, attaching numbers) wrote new ids
    into ``provider_refs`` (the JSONB column does not track in-place
    changes, so mark it)."""
    status = await _settle_sms_approval(db, row, status, registry)
    changed = _apply_status(row, status)
    if changed and status is not None and status.state == "rejected":
        await _audit_carrier_rejection(row, status.raw_reason)
        await _release_number_hold(db, row)
    elif (
        changed
        and status is not None
        and status.state == "pending"
        and status.reason
        and (row.provider_refs or {}).get("campaign_needs_repair")
    ):
        # A refused campaign held for the customer's answers (a legacy
        # evidence hold is no refusal). The carrier's text can quote
        # personal data: audit row only, never the log.
        await write_audit_log(
            row.organization_id,
            None,
            "verification.campaign_refused",
            "carrier_verification",
            row.id,
            {"reason": row.rejection_reason, "carrier_reason": status.raw_reason},
            actor_kind="system",
        )
    if row.provider_refs != refs_before:
        flag_modified(row, "provider_refs")
        changed = True
    return changed


def _campaign_filed(provider: object, refs: dict | None) -> bool:
    """``SmsRegistrationProvider.campaign_filed``; False for any other
    plug-in, or none."""
    if not isinstance(provider, SmsRegistrationProvider):
        return False
    return provider.campaign_filed(refs)


# A US texting registration in these states has its campaign still to file.
_BEFORE_CAMPAIGN_STATES = ("awaiting_review", "submitting", "submitted")
# States a registration never leaves for one before its campaign.
_SETTLED_STATES = ("approved", "rejected", "cancelled")
# A poll the credit check lets file a campaign marks its row with
# ``provider_refs.campaign_reserved_at`` (database statement time) and clears
# it when its carrier call returns. While set, and for at most this long
# (a process that died in the call), the row keeps first claim on the
# credit: another check during the call (the org lock is released by then)
# cannot hand the same credit to another row. Longer than a slow carrier
# call (each request has its own network timeout).
_CAMPAIGN_RESERVED_FOR = timedelta(minutes=15)


def _campaign_reserved_sql():
    """SQL: the row's campaign reservation still holds, by the database's
    clock (one clock for every API process and worker)."""
    return func.coalesce(
        _refs_time("campaign_reserved_at")
        > func.statement_timestamp() - _CAMPAIGN_RESERVED_FOR,
        False,
    )


async def _drop_reservation(
    db: AsyncSession,
    verification_id: UUID,
    reserved_at: str | None = None,
    *,
    skip_locked: bool = False,
) -> None:
    """Remove ``campaign_reserved_at`` from the row's refs: only the value
    ``reserved_at`` when given (not one another poll has kept since). With
    ``skip_locked``, leave it when another transaction holds the row (it
    then runs out on its own)."""
    target = CarrierVerification.id == verification_id
    if reserved_at is not None:
        target = target & (
            CarrierVerification.provider_refs["campaign_reserved_at"].astext
            == reserved_at
        )
    if skip_locked:
        target = target & CarrierVerification.id.in_(
            select(CarrierVerification.id)
            .where(CarrierVerification.id == verification_id)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
    await db.execute(
        update(CarrierVerification)
        .where(target)
        .values(
            provider_refs=CarrierVerification.provider_refs.op("-")(
                "campaign_reserved_at"
            )
        )
        .execution_options(synchronize_session=False)
    )


def _billed_sms(row: CarrierVerification | Row) -> bool:
    """A US texting registration of an organization that pays for it: the
    self-hosted organization (the shared key's) is not billed."""
    return (
        row.purpose == "sms"
        and row.number_type != "toll_free"
        and row.organization_id != SELF_HOSTED_ORG_ID
    )


def _campaign_gated(row: CarrierVerification | Row, registry: Registry) -> bool:
    """True when the next billed step of this US texting registration is
    filing its campaign, and its organization pays for it (``_billed_sms``).
    The filing itself was paid for at creation (create_verification checks
    the credit)."""
    if not _billed_sms(row) or row.state not in _BEFORE_CAMPAIGN_STATES:
        return False
    if (row.provider_refs or {}).get("needs_information"):
        return False  # waits for the customer's repair, not for credit
    provider = registry(row.provider, "sms")
    return isinstance(provider, SmsRegistrationProvider) and not _campaign_filed(
        provider, row.provider_refs
    )


def _carrier_fee(
    fee: Callable[[str, str], int], provider: str, subject_type: str
) -> int | None:
    """``fee(provider, subject_type)`` from ``sms_registration_fees``; None
    for a carrier with no SMS registration fees."""
    try:
        return fee(provider, subject_type)
    except KeyError:
        return None


async def _uncharged_cents(db: AsyncSession, organization_id: UUID) -> int:
    """Carrier costs of the organization's US texting steps not charged yet
    (``_uncharged_sms_steps``): new brands and filed campaigns. They count
    against the balance, as unsettled filings do at filing, so one credit
    does not pay for two steps."""
    brands = await _uncharged_sms_steps(db, organization_id, "submission")
    campaigns = await _uncharged_sms_steps(db, organization_id, "review")
    brand = sms_registration_fees.brand_fee_cents
    campaign = sms_registration_fees.campaign_fee_cents
    return sum(_carrier_fee(brand, p, t) or 0 for p, t in brands) + sum(
        _carrier_fee(campaign, p, t) or 0 for p, t in campaigns
    )


# What _campaign_gated and _campaign_unfunded read of a row.
_GATE_COLUMNS = (
    CarrierVerification.id,
    CarrierVerification.organization_id,
    CarrierVerification.purpose,
    CarrierVerification.state,
    CarrierVerification.provider,
    CarrierVerification.subject_type,
    CarrierVerification.number_type,
    CarrierVerification.provider_refs,
    CarrierVerification.created_at,
)


async def _campaign_unfunded(
    db: AsyncSession, rows: list[CarrierVerification], registry: Registry
) -> dict[UUID, int]:
    """The rows whose campaign is still to file (``_campaign_gated``) while
    the credit does not cover the carrier's campaign fee, each with the
    credit it still needs, in cents. Their polls file no campaign until a
    top-up. The public API knows no Hail prices; the carrier's cost is the
    floor, as for the filing.

    The credit is the balance less the steps not yet charged
    (``_uncharged_cents``). It goes to the organization's registrations
    still before their campaign, reserved ones first (``_campaign_reserved_sql``)
    then oldest first, each using up its fee, so
    one credit never pays for two campaigns. All of them count, not only
    ``rows``, so every path gives the credit to the same registration.

    The credit needed is per row and adds up: the first row left without
    credit needs its fee less what is left once every covered row has
    taken its own (more than its fee when that is below zero), each later
    one its whole fee. So the sum over an organization's waiting rows is
    the one top-up that lets them all go."""
    given = {row.id for row in rows}
    # From every US texting row given, not only those gated in the caller's
    # copy: a copy can be stale. The stored rows decide below.
    orgs = {
        row.organization_id
        for row in rows
        if _billed_sms(row) and row.state not in _SETTLED_STATES
    }
    unfunded: dict[UUID, int] = {}
    for org in orgs:
        # The stored rows, not the caller's copies: a copy can be stale (the
        # sweeper reads all rows first). Columns only, so the rows the caller
        # holds are not reloaded.
        found = await db.execute(
            select(*_GATE_COLUMNS, _campaign_reserved_sql().label("reserved")).where(
                CarrierVerification.organization_id == org,
                CarrierVerification.purpose == "sms",
                CarrierVerification.state.in_(_BEFORE_CAMPAIGN_STATES),
            )
        )
        found = found.all()
        if not any(_campaign_gated(r, registry) for r in found):
            continue
        # One read of the ledger: the check uses the balance as
        # get_balance_cents reads it (truncated toward zero).
        ledger = await get_ledger_cents(db, org)
        balance = int(ledger)
        left = balance - await _uncharged_cents(db, org)
        # A reserved row (its campaign may be in flight) keeps its claim.
        order = sorted(found, key=lambda r: (not r.reserved, r.created_at, r.id))
        waiting: list[tuple[UUID, int]] = []
        for row in order:
            if not _campaign_gated(row, registry):
                continue
            fee = _carrier_fee(
                sms_registration_fees.campaign_fee_cents,
                row.provider,
                row.subject_type,
            )
            if fee is None:
                continue
            if left < fee:
                waiting.append((row.id, fee))
            else:
                left -= fee
        # A top-up adds whole cents and the check truncates the sum toward
        # zero: below zero, a fraction of a cent costs one more cent.
        left -= balance - math.floor(ledger)
        # ``left`` only falls, so it is below each waiting row's fee: the
        # first one needs more than zero, the later ones their whole fee.
        for row_id, fee in waiting:
            unfunded[row_id] = fee - left
            left = 0
    return {row_id: need for row_id, need in unfunded.items() if row_id in given}


class _CreditCheck(NamedTuple):
    """``_campaign_may_go``'s answer."""

    may_file: bool
    reserved_at: str | None
    gated: bool


async def _campaign_may_go(
    db: AsyncSession,
    row: CarrierVerification | Row,
    registry: Registry,
    *,
    wait: bool = True,
) -> _CreditCheck:
    """The campaign credit check of one row, before a poll that may file
    its campaign. ``may_file`` True: the poll may file it (covered, or no
    check applies). False: the poll skips only that paid filing and runs
    every other step (brand reads, rejections, code resends, adopting a
    campaign whose answer was lost): not covered, the org lock is busy
    (``wait`` False), the row is held, or the row already carries a
    running reservation (``_CAMPAIGN_RESERVED_FOR``) another poll made.
    ``reserved_at``: the reservation this poll wrote (only with
    ``may_file``), the one value it may drop. ``gated``: the stored row
    needed the check (the caller's copy can be stale); a row gone from the
    database files nothing.

    Reads the stored row and the balance under the organization's lock
    (``org_lock``), after the billing writes in flight, as the filing check
    does, then commits to release it: it is not held through the row lock
    and the carrier call that follows, which can take seconds while the
    website's rater and other billing writes wait for the same lock.
    Background and GET polls do not wait for it (like the row lock); a
    confirmed code does, so its campaign is filed at once unless another
    poll's reservation is running on the row or another poll holds the row
    lock."""
    if not _billed_sms(row):  # purpose and organization never change
        return _CreditCheck(True, None, False)
    # A first read without the lock: most polls (a campaign already filed)
    # need no check and must not compete for the lock. A row that becomes
    # gated after this read is caught by _refresh's re-read (fail closed).
    first = (
        await db.execute(select(*_GATE_COLUMNS).where(CarrierVerification.id == row.id))
    ).one_or_none()
    if first is None:
        return _CreditCheck(False, None, False)
    if not _campaign_gated(first, registry):
        return _CreditCheck(True, None, False)
    if wait:
        await org_lock(db, row.organization_id)
    elif not await try_org_lock(db, row.organization_id):
        await db.commit()
        return _CreditCheck(False, None, True)
    stored = (
        await db.execute(
            select(*_GATE_COLUMNS, _campaign_reserved_sql().label("reserved")).where(
                CarrierVerification.id == row.id
            )
        )
    ).one_or_none()
    if stored is None or not _campaign_gated(stored, registry):
        await db.commit()  # ends the org lock
        return _CreditCheck(stored is not None, None, False)
    if stored.reserved or await _campaign_unfunded(db, [stored], registry):
        # A running reservation is another poll's: its call is in flight,
        # or failed and may have filed a campaign the next answer shows.
        await db.commit()  # ends the org lock
        return _CreditCheck(False, None, True)
    # Funded: reserve the credit before the lock is released. Not when
    # another poll holds the row (it is asking the carrier now): waiting
    # here would hold the org lock through that call, and this poll would
    # skip the row anyway.
    held = (
        await db.execute(
            select(CarrierVerification.id)
            .where(CarrierVerification.id == row.id)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    reserved_at = None
    if held is not None:
        refs = func.coalesce(
            func.nullif(CarrierVerification.provider_refs, text("'null'::jsonb")),
            text("'{}'::jsonb"),
        )
        reserved_at = (
            await db.execute(
                update(CarrierVerification)
                .where(CarrierVerification.id == row.id)
                .values(
                    provider_refs=refs.op("||")(
                        func.jsonb_build_object(
                            "campaign_reserved_at", func.statement_timestamp()
                        )
                    )
                )
                .returning(
                    CarrierVerification.provider_refs["campaign_reserved_at"].astext
                )
                .execution_options(synchronize_session=False)
            )
        ).scalar_one()
    await db.commit()  # ends the org lock (and the row lock)
    return _CreditCheck(held is not None, reserved_at, True)


def _added_only(before: dict, after: dict) -> dict:
    """``before`` (a private copy) plus the ids ``after`` added: new keys,
    empty values filled in (None, False, ""), items appended to a list, and
    the same inside nested dicts. Keys removed or changed go back to their
    ``before`` value."""
    out = dict(before)
    for key, new in after.items():
        old = before.get(key)
        # ``is`` for None and False: ``0 in (False,)`` is True, so a zero
        # would count as empty.
        empty = old is None or old is False or old == ""
        if key not in before or (empty and new):
            out[key] = new
        elif isinstance(old, list) and isinstance(new, list):
            if new[: len(old)] == old:
                out[key] = new
        elif isinstance(old, dict) and isinstance(new, dict):
            out[key] = _added_only(old, new)
    return out


def _stamp_campaign_filed(provider: object, refs: dict) -> None:
    """Write ``campaign_filed_at`` once a US texting campaign is filed, under
    whatever key the carrier keeps it (``campaign_ref``). The billing
    service charges the carriers' review on this one key, so a new carrier
    needs nothing more than its ``campaign_ref``. Call it after every
    plug-in call that can file a campaign, before the commit."""
    filed_at = refs.get("campaign_filed_at")
    # Missing unless a non-empty string: the rule campaign_filed and the
    # billing service read.
    if not (isinstance(filed_at, str) and filed_at) and _campaign_filed(provider, refs):
        refs["campaign_filed_at"] = datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def _plugin_call(
    row: CarrierVerification, provider: object
) -> AsyncIterator[None]:
    """Around each plug-in call whose writes into ``row.provider_refs`` (in
    place) outlive it: check, submit, status, and the discard of a refused
    draft or a superadmin reject. After it: stamp a filed campaign, and
    mark the refs changed when they did, so the caller's next commit saves
    them (JSONB does not track in-place changes). When the call raises
    partway, only the ids it added are kept (``_added_only``; one it filed
    may be billed). The caller commits. A cancel (a restart) commits
    nothing, so the refs are left as they are (a plug-in thread may still
    be writing them).
    A customer cancel and the create rollback are not wrapped: the refs are
    wiped or rolled back right after."""
    before = copy.deepcopy(row.provider_refs)
    try:
        yield
    except Exception:
        kept = _added_only(before, row.provider_refs)
        row.provider_refs.clear()
        row.provider_refs.update(kept)
        raise
    finally:
        try:
            _stamp_campaign_filed(provider, row.provider_refs)
        except Exception:
            # Plug-in code: never turn a call that worked into a failure.
            logger.exception("campaign stamp failed for %s", row.id)
        if row.provider_refs != before:
            flag_modified(row, "provider_refs")


def _notify_billing(rows: list[CarrierVerification]) -> None:
    """Tell the website's fee rater about US texting rows that changed and
    are committed (a new filing and a campaign sent for review are billed
    steps). Call it after every commit that changes a row's state or refs;
    the rater is idempotent."""
    for row in rows:
        if row.purpose == "sms" and row.number_type != "toll_free":
            notify_sms_registration_changed(str(row.id))


BillingView = tuple[str, dict]


def _billing_view(row: CarrierVerification) -> BillingView:
    """What the fee rater reads of a row: its state and refs (a copy)."""
    return row.state, copy.deepcopy(row.provider_refs)


def _notify_if_changed(row: CarrierVerification, before: BillingView | None) -> None:
    """``_notify_billing`` for a committed row whose state or refs differ
    from ``before`` (its ``_billing_view`` before the change). ``before``
    None means a new row: always told."""
    if before is None or _billing_view(row) != before:
        _notify_billing([row])


async def _rate_before_wipe(
    db: AsyncSession, row: CarrierVerification, provider: object
) -> bool:
    """Before ``provider_refs`` is emptied: the rater charges the carrier
    review by the campaign id in the refs, so let it read them first. A
    campaign's ping when it was filed can be lost; once the id is gone,
    neither a ping nor the rater's sweep can charge it. Awaited, not
    fire-and-forget, so the rater reads before the wipe is committed.

    Commits first (True when it did), which ends any row lock. Never call
    it while holding the organization's lock (``org_lock``): the rater
    takes the same lock."""
    if (
        row.purpose != "sms"
        or row.number_type == "toll_free"
        or not _campaign_filed(provider, row.provider_refs)
    ):
        return False
    # Saved with the stamp, so the rater reads a campaign filed just now.
    _stamp_campaign_filed(provider, row.provider_refs)
    flag_modified(row, "provider_refs")
    await db.commit()
    await rate_sms_registration(str(row.id))
    return True


async def _audit_carrier_rejection(
    row: CarrierVerification, raw_reason: str | None
) -> None:
    """The carrier rejected a submitted verification. The row holds Hail's
    wording for the customer; the carrier's own text is kept in the audit
    row only (it can quote personal data, so it is not logged)."""
    logger.warning(
        "carrier %s rejected verification %s (org %s)",
        row.provider,
        row.id,
        row.organization_id,
    )
    await write_audit_log(
        row.organization_id,
        None,
        "verification.reject",
        "carrier_verification",
        row.id,
        {
            "rejected_by": None,
            "reason": row.rejection_reason,
            "carrier_reason": raw_reason,
        },
        actor_user_id=None,
        actor_kind="system",
    )


async def _lock_for_poll(db: AsyncSession, row: CarrierVerification) -> bool:
    """Lock a verification row before asking the carrier about it, and
    re-read it. False when another poll holds the lock: that poll is in
    flight, so this one skips the row. Two polls at once would both apply
    the carrier's answer: an SMS plug-in would create two campaigns, and the
    owners would get two decision emails. The lock is held until the caller
    commits."""
    locked = (
        await db.execute(
            select(CarrierVerification)
            .where(CarrierVerification.id == row.id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return locked is not None


async def _refresh(
    db: AsyncSession,
    row: CarrierVerification,
    registry: Registry,
    *,
    wait_for_org_lock: bool = False,
) -> None:
    """Pull a submitted verification's outcome from the carrier.

    A row cut off in 'submitting' that the carrier reports as submitted (or
    already reviewed) gets the audit row its interrupted submit never wrote;
    approved_by was saved before the carrier call, so the trail names the
    admin when one approved it."""
    was_submitting = row.state == "submitting"
    if not _pollable(row):
        return
    # Not covered, or the org lock is busy (another poll or a billing
    # write: the next poll asks): the poll runs, without the campaign filing.
    check = await _campaign_may_go(db, row, registry, wait=wait_for_org_lock)
    may_file, reserved = check.may_file, check.reserved_at
    if not await _lock_for_poll(db, row):
        if reserved:
            # No carrier call here. The holder is usually another poll: it
            # finds this reservation running, files nothing, and drops it
            # when its call returns. If the row is free again, drop it now;
            # if a non-poll holds it, it runs out (_CAMPAIGN_RESERVED_FOR).
            # Only the value this poll wrote.
            await _drop_reservation(db, row.id, reserved, skip_locked=True)
            await db.commit()
        return
    if not _pollable(row):  # another poll settled it before the lock
        if "campaign_reserved_at" in (row.provider_refs or {}):
            # Settled, or on hold: no campaign to claim credit for.
            await _drop_reservation(db, row.id)
        await db.commit()
        return
    reserved_now = (row.provider_refs or {}).get("campaign_reserved_at")
    if may_file and check.gated and reserved_now != reserved:
        # The reservation this poll made is gone (another poll's call
        # returned and dropped it): no claim, so no campaign this poll.
        may_file = False
    if not check.gated and _campaign_gated(row, registry):
        # It changed after it was read and got no credit check: fail closed,
        # no campaign on this poll; the next one checks.
        may_file = False
    # Not the plug-in's, and not a billed change: kept out of the refs it
    # sees and the change check (``before``).
    reserved_at = (row.provider_refs or {}).pop("campaign_reserved_at", None)
    before = copy.deepcopy(row.provider_refs)
    status = await _fetch_status(row, registry, may_file_campaign=may_file)
    if reserved_at is not None:
        if status is None:
            # The call failed: a campaign it filed may come to light on the
            # next poll. The reservation holds until then (or runs out).
            row.provider_refs["campaign_reserved_at"] = reserved_at
            before["campaign_reserved_at"] = reserved_at
        else:
            await _drop_reservation(db, row.id, reserved_at)
    changed = await _poll_and_apply(db, row, status, registry, before)
    await db.commit()  # also ends the row lock
    if not changed:
        return
    _notify_billing([row])
    if was_submitting and row.state in ("submitted", "approved", "rejected"):
        # approved_by is set only by a superadmin's approve; an automatic
        # submit (creation or the sweeper) that was cut off has none.
        approved = row.approved_by is not None
        await write_audit_log(
            row.organization_id,
            None,
            "verification.approve" if approved else "verification.submit",
            "carrier_verification",
            row.id,
            {
                "approved_by": str(row.approved_by) if row.approved_by else None,
                "recovered": True,
            },
            actor_user_id=row.approved_by,
            actor_kind="superadmin" if approved else "system",
        )


def _sms_progress(
    row: CarrierVerification, registry: Registry
) -> tuple[str | None, str | None]:
    """``(confirmation, step)`` of an SMS registration in progress: how the
    person must confirm ('code', 'reply' or None) and where the filing is.
    A draft not yet submitted is still being filed."""
    if row.purpose != "sms":
        return None, None
    if row.state == "awaiting_review":
        return None, "filing"
    if row.state not in _POLLED_STATES:
        return None, None
    provider = registry(row.provider, "sms")
    if not isinstance(provider, SmsRegistrationProvider):
        return None, None
    refs = row.provider_refs or {}
    return provider.confirmation(refs), provider.step(refs)


def _out(
    row: CarrierVerification,
    registry: Registry,
    model: type[VerificationResponse] = VerificationResponse,
    *,
    credit_needed_cents: int | None = None,
) -> VerificationResponse:
    """The response for a row, with ``confirmation``, ``step`` and
    ``identity_confirmed`` filled in. ``credit_needed_cents``: the row's
    credit need from ``_campaign_unfunded`` (the caller reads the balance),
    None when it does not wait for credit."""
    refs = row.provider_refs or {}
    identity_confirmed = bool(refs.get("kept_brand_id"))
    if row.purpose == "sms" and row.state == "rejected":
        provider = registry(row.provider, "sms")
        identity_confirmed = isinstance(
            provider, SmsRegistrationProvider
        ) and provider.keeps_brand(refs)
    confirmation, step = _sms_progress(row, registry)
    return model.model_validate(row).model_copy(
        update={
            "confirmation": confirmation,
            "step": step,
            "identity_confirmed": identity_confirmed,
            "campaign_accepted": bool(refs.get("campaign_accepted"))
            or (row.purpose == "sms" and row.state == "approved"),
            "runtime_ready": (
                refs.get("runtime_ready") if row.purpose == "sms" else None
            ),
            "cancellable": _can_cancel(row),
            "waiting_for_credit": credit_needed_cents is not None,
            "credit_needed_cents": credit_needed_cents,
        }
    )


async def _out_checked(
    db: AsyncSession,
    row: CarrierVerification,
    registry: Registry,
    model: type[VerificationResponse] = VerificationResponse,
) -> VerificationResponse:
    """``_out`` with ``waiting_for_credit`` and ``credit_needed_cents`` read
    from the balance."""
    waiting = await _campaign_unfunded(db, [row], registry)
    return _out(row, registry, model, credit_needed_cents=waiting.get(row.id))


async def approved_handles(
    db: AsyncSession,
    registry: Registry,
    organization_id: UUID,
    country_code: str,
    number_type: str,
    providers: list[str] | tuple[str, ...],
) -> dict[str, list[Approval]]:
    """The org's approved number verifications for this country and number
    type, per carrier and newest first, with the carrier ids a purchase
    needs. An org may hold several of one subject type (several people or
    companies). Hail's rows decide which approvals exist; the carrier only
    confirms each is still valid (``Carrier.offers``).

    Approved rows only, and no carrier call: the purchase route must not
    wait on the carrier here. A submitted verification becomes approved
    when it is read (GET refreshes) or when numbers are quoted."""
    rows = (
        await db.execute(
            select(CarrierVerification)
            .where(
                CarrierVerification.organization_id == organization_id,
                CarrierVerification.provider.in_(list(providers)),
                CarrierVerification.country_code == country_code,
                CarrierVerification.number_type == number_type,
                CarrierVerification.purpose == "number",
                CarrierVerification.state == "approved",
            )
            .order_by(CarrierVerification.created_at.desc(), CarrierVerification.id)
        )
    ).scalars()
    out: dict[str, list[Approval]] = {}
    for row in rows:
        provider = registry(row.provider, "number")
        if provider is None:
            continue
        handle = await provider.purchase_handle(row.provider_refs)
        out.setdefault(row.provider, []).append(
            Approval(
                row_id=row.id,
                subject_type=row.subject_type,
                subject_name=row.subject_name,
                verification_id=handle.get("verification_id"),
                address_id=handle.get("address_id"),
            )
        )
    return out


async def refresh_number_verifications(
    db: AsyncSession,
    registry: Registry,
    organization_id: UUID,
    country_code: str,
    number_types: list[str],
) -> None:
    """Ask the carrier about the org's submitted number verifications for
    this country and these number types, so one the carrier approved is
    used by the quote that follows. Throttled like the list route: a row
    asked in the last minute is skipped. Carrier errors are logged and
    skipped (``_fetch_status``). Commits when a row was asked."""
    rows = (
        (
            await db.execute(
                select(CarrierVerification).where(
                    CarrierVerification.organization_id == organization_id,
                    CarrierVerification.country_code == country_code,
                    CarrierVerification.number_type.in_(number_types),
                    CarrierVerification.purpose == "number",
                    CarrierVerification.state.in_(_POLLED_STATES),
                )
            )
        )
        .scalars()
        .all()
    )
    now = time.monotonic()
    asked = [r for r in rows if _poll_due(r, now)]
    if not asked:
        return
    # Locked and re-read first (see _lock_for_poll).
    due = [r for r in asked if await _lock_for_poll(db, r) and _pollable(r)]
    before = [copy.deepcopy(r.provider_refs) for r in due]
    # Fail closed: a row that needs the credit check got none here.
    statuses = await asyncio.gather(
        *(
            _fetch_status(
                r, registry, may_file_campaign=not _campaign_gated(r, registry)
            )
            for r in due
        )
    )
    changed = [
        r
        for r, st, b in zip(due, statuses, before)
        if await _poll_and_apply(db, r, st, registry, b)
    ]
    await db.commit()  # saves changes and ends the row locks
    _notify_billing(changed)


# -- customer routes ------------------------------------------------------


@router.get(
    "/requirements",
    response_model=VerificationRequirements,
    operation_id="get_verification_requirements",
)
async def get_requirements(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
    country_code: Annotated[str, Query(min_length=2, max_length=2)],
    number_type: str,
    subject_type: Annotated[
        SubjectType,
        Query(
            description="Who is verifying: 'person' (you) or 'business' "
            "(your company). Required. Ask the user which one."
        ),
    ],
    provider: Annotated[
        str | None,
        Query(
            deprecated=True,
            description="Deprecated and ignored. Hail picks the carrier from "
            "quote_id or number_id.",
        ),
    ] = None,
    purpose: str = "number",
    quote_id: Annotated[
        UUID | None,
        Query(
            description="Required for purpose=number: quote_id of an offer "
            "from POST /numbers/quotes that needs verification. The carrier "
            "is read from it."
        ),
    ] = None,
    number_id: Annotated[
        UUID | None,
        Query(
            description="For purpose=sms: id of an owned US local or US/Canadian toll-free number. Required for toll-free verification. The "
            "carrier is read from it. Needed when your US numbers are with "
            "more than one carrier."
        ),
    ] = None,
) -> VerificationRequirements:
    """What the carrier needs from you: the fields, the documents, and whether
    an address is needed. `purpose=number` (default) is for buying a number;
    it requires the offer's `quote_id` (422 without it) and returns the form
    for that offer. `purpose=sms`
    is for registering to send SMS in the US; it uses the carrier of your US
    numbers, or of the number passed as `number_id`. Build your form from it.
    `provider` is ignored."""
    if purpose not in PURPOSES:
        raise unprocessable("unknown purpose", loc=["query", "purpose"])
    if purpose == "sms" and quote_id is not None:
        raise unprocessable(
            "quote_id is only for purpose=number", loc=["query", "quote_id"]
        )
    if purpose != "sms" and number_id is not None:
        raise unprocessable(
            "number_id is only for purpose=sms", loc=["query", "number_id"]
        )
    if purpose == "sms":
        name = await _sms_provider_name(
            db,
            principal.organization_id,
            number_id,
            "query",
            country_code=country_code.upper(),
            number_type=number_type,
        )
    elif quote_id is not None:
        name = (
            await _quote_offer(
                db,
                principal.organization_id,
                quote_id,
                country_code,
                number_type,
                "query",
            )
        )["provider"]
    else:
        raise _quote_required("query")
    requirements = await _requirements(
        _provider_or_404(registry, name, purpose),
        country_code,
        number_type,
        subject_type,
    )
    # The public form leaves out which carrier it is for.
    return VerificationRequirements.model_validate(
        requirements.model_dump(exclude={"provider", "address_scope"})
    )


def _application_record(
    subject_type: SubjectType,
    requirements: Requirements,
    fields: dict[str, str],
    address: Address | None,
) -> dict:
    """The private saved SMS answers (``provider_refs.application``): only
    the form's own fields and the address, never files or extra parts."""
    return {
        "schema_version": 1,
        "subject_type": subject_type,
        "fields": {
            spec.name: fields[spec.name]
            for spec in requirements.fields
            if spec.name in fields
        },
        "address": address.model_dump() if address else None,
    }


async def _sms_mobile_problems(
    db: AsyncSession, subject_type: SubjectType, fields: dict[str, str]
) -> list[Problem]:
    """A Hail number cannot be the person's independently owned mobile.

    This local check covers every provider; the carrier's identity flow still
    establishes ownership. Unknown or invalid inputs are handled by preflight.
    """
    if subject_type != "person" or not fields.get("mobile_phone_number"):
        return []
    try:
        parsed = phonenumbers.parse(fields["mobile_phone_number"])
        mobile = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        return []
    owned = (
        await db.execute(
            select(PhoneNumber.id)
            .where(
                PhoneNumber.e164 == mobile,
                PhoneNumber.provisioning_state != "released",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if owned is not None:
        return [
            Problem(
                field="mobile_phone_number",
                message="Use your own mobile number, not a Hail number.",
            )
        ]
    return []


class SmsPreviewRequest(BaseModel):
    subject_type: SubjectType = Field(
        description="Sender route to preview: person or business."
    )
    number_id: UUID | None = Field(
        default=None,
        description="Owned US local or US/Canadian toll-free number. Required for toll-free verification or when the carrier is ambiguous.",
    )
    fields: dict[str, str] = Field(description="Current registration form answers.")
    address: Address | None = Field(
        default=None, description="Sender address required by the registration form."
    )


@router.post("/sms/preview", operation_id="preview_sms_application")
async def preview_sms_application(
    body: SmsPreviewRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
) -> dict:
    """Compile the exact campaign text without creating or charging a filing.

    Checks answers locally. This does not establish policy accessibility,
    carrier acceptance, identity ownership or runtime messaging readiness.
    """
    provider_name = await _sms_provider_name(
        db, principal.organization_id, body.number_id, "body"
    )
    provider = _provider_or_404(registry, provider_name, "sms")
    if not isinstance(provider, SmsRegistrationProvider):
        raise HTTPException(status_code=404, detail="SMS registration is unavailable.")
    number = (
        await _sms_number(db, principal.organization_id, body.number_id)
        if body.number_id
        else None
    )
    number_type = number.number_type if number else "local"
    country_code = number.country_code if number_type == "toll_free" else "US"
    requirements = await _requirements(
        provider, country_code, number_type, body.subject_type, where="body"
    )
    if (number_type == "toll_free") != (
        body.fields.get("registration_kind") == "toll_free"
    ):
        raise unprocessable(
            "Use the registration form for the selected number type.",
            loc=["body", "fields", "registration_kind"],
        )
    problems = length_problems(requirements, body.fields, {})
    problems.extend(
        provider.validate_application(body.fields, body.address, body.subject_type)
    )
    if number_type != "toll_free":
        problems.extend(await _sms_mobile_problems(db, body.subject_type, body.fields))
    if problems:
        raise _problems_422(problems, principal.organization_id, provider_name)
    response.headers["Cache-Control"] = "no-store"
    return asdict(campaign_text(body.fields, body.subject_type))


class _DocumentPart(BaseModel):
    option: str = ""
    fields: dict[str, str] = Field(default_factory=dict)


class _Submission(BaseModel):
    # Deprecated and ignored: old clients still send it.
    provider: str | None = None
    quote_id: UUID | None = None
    number_id: UUID | None = None
    purpose: str = "number"
    country_code: str = Field(min_length=2, max_length=2)
    number_type: str
    subject_type: SubjectType
    fields: dict[str, str] = Field(default_factory=dict)
    address: Address | None = None
    documents: dict[str, _DocumentPart] = Field(default_factory=dict)


def _duplicate_detail(subject_type: str, subject_name: str | None) -> str:
    """The 409 for a second live verification of the same kind and name. It
    names the person or company back to the customer who typed it; the
    detail is not logged."""
    who = "a person" if subject_type == "person" else "a company"
    if subject_name is None:
        return (
            f"You already have a verification as {who} for this number type "
            "with no name on it."
        )
    return (
        f"You already have a verification for {subject_name} as {who} "
        "for this number type."
    )


def _text_part(form, name: str, default: str = "") -> str:
    raw = form.get(name)
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str):
        raise unprocessable(f"{name} must be text", loc=["body", name])
    return raw


def _json_part(form, name: str, default):
    raw = _text_part(form, name)
    if raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise unprocessable(f"{name} must be valid JSON", loc=["body", name]) from exc


# Text parts of a verification form: about 10 named parts plus one
# ``upload.<slot>`` per file, and a file may be sent either way (20 at most).
MAX_FORM_FIELDS = 40


async def _parse_form(request: Request):
    return await parse_capped_form(
        request,
        max_bytes=MAX_REQUEST_BYTES,
        max_files=20,
        max_fields=MAX_FORM_FIELDS,
    )


async def _read_file(upload) -> UploadedFile:
    if upload.content_type not in ALLOWED_FILE_TYPES:
        raise unprocessable(
            "files must be JPG, PNG or PDF",
            loc=["body", "files", upload.filename or ""],
        )
    buf = bytearray()
    while True:
        chunk = await upload.read(_READ_CHUNK)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_FILE_BYTES:
            raise unprocessable(
                "each file must be 10 MB or smaller", loc=["body", "files"]
            )
    return UploadedFile(
        filename="upload", content_type=upload.content_type, data=bytes(buf)
    )


class _Prepared(NamedTuple):
    """A submission that passed every check Hail runs before filing."""

    sub: _Submission
    offer: dict
    provider_name: str
    provider: VerificationProvider
    hold_cents: int
    requirements: Requirements
    subject_name: str | None
    documents: dict[str, DocumentInput]
    draft_kwargs: dict
    # The ``upload.<slot>`` rows read, and every file read, for the check.
    uploads: list[FileUpload]
    files: list[VerificationCheckFile]


async def _prepare_submission(
    request: Request,
    principal: Principal,
    db: AsyncSession,
    registry: Registry,
    s3: S3MailClient | None,
    *,
    lock: bool,
) -> _Prepared:
    """Every step of ``POST /verifications`` before anything is filed:
    the form and purpose rules, the carrier pick, the credit checks (402),
    the carrier's form (a read), the duplicate check (409), the length
    limits and the files. Shared with ``POST /verifications/check``, which
    passes ``lock=False``: it saves nothing, so it takes no lock.
    """
    form = await _parse_form(request)
    if _text_part(form, "subject_type") not in ("person", "business"):
        raise unprocessable(
            "subject_type is required: 'person' (you) or 'business' (your company)",
            loc=["body", "subject_type"],
        )

    try:
        sub = _Submission(
            provider=_text_part(form, "provider") or None,
            quote_id=_text_part(form, "quote_id") or None,
            number_id=_text_part(form, "number_id") or None,
            purpose=_text_part(form, "purpose", "number"),
            country_code=_text_part(form, "country_code").upper(),
            number_type=_text_part(form, "number_type"),
            subject_type=_text_part(form, "subject_type"),  # type: ignore[arg-type]
            fields=_json_part(form, "fields", {}),
            address=_json_part(form, "address", None),
            documents=_json_part(form, "documents", {}),
        )
    except ValidationError as exc:
        raise unprocessable(
            "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()
            ),
        ) from exc

    if sub.purpose not in PURPOSES:
        raise unprocessable("unknown purpose", loc=["body", "purpose"])
    if sub.purpose == "sms" and (
        not (
            (sub.country_code == "US" and sub.number_type == "local")
            or (sub.country_code in {"US", "CA"} and sub.number_type == "toll_free")
        )
    ):
        raise unprocessable(
            "SMS registration supports US local and US/Canadian toll-free numbers",
            loc=["body", "country_code"],
        )

    if sub.purpose == "sms" and sub.quote_id is not None:
        raise unprocessable(
            "quote_id is only for purpose=number", loc=["body", "quote_id"]
        )
    if sub.purpose != "sms" and sub.number_id is not None:
        raise unprocessable(
            "number_id is only for purpose=sms", loc=["body", "number_id"]
        )
    offer: dict = {}
    if sub.purpose == "sms":
        provider_name = await _sms_provider_name(
            db,
            principal.organization_id,
            sub.number_id,
            "body",
            country_code=sub.country_code,
            number_type=sub.number_type,
        )
    elif sub.quote_id is not None:
        offer = await _quote_offer(
            db,
            principal.organization_id,
            sub.quote_id,
            sub.country_code,
            sub.number_type,
            "body",
        )
        provider_name = offer["provider"]
    else:
        raise _quote_required("body")
    provider = _provider_or_404(registry, provider_name, sub.purpose)

    # A billed number verification sets aside setup and the first month
    # until the number is bought (a by-request offer is ordered by hand, so
    # no purchase would give it back). Checked here first, before any
    # carrier call, and again under the lock when the row is saved.
    hold_cents = 0
    if (
        sub.purpose == "number"
        and principal.auth_kind != "shared"
        and not offer.get("by_request")
    ):
        hold_cents = offer["setup_cents"] + offer["monthly_cents"]
        if await get_balance_cents(db, principal.organization_id) < hold_cents:
            raise _hold_402(hold_cents)

    if lock and sub.purpose == "sms":
        # Held until the row is saved: a second filing at the same time
        # waits here, then sees the first row and gets 409.
        await org_lock(db, principal.organization_id)
    # The form for this offer. A read from the carrier; nothing is filed.
    # Some plug-ins need its field labels to find the name below.
    requirements = await _requirements(
        provider, sub.country_code, sub.number_type, sub.subject_type, where="body"
    )
    if sub.purpose == "sms" and (
        (sub.number_type == "toll_free")
        != (sub.fields.get("registration_kind") == "toll_free")
    ):
        raise unprocessable(
            "Use the registration form for the selected number type.",
            loc=["body", "fields", "registration_kind"],
        )
    if not requirements.required:
        raise unprocessable(
            "this number does not need verification", loc=["body", "number_type"]
        )

    # The name the customer typed, as the plug-in reads it from this form.
    # Some plug-ins read it from a document's fields; none reads the files,
    # so they are not read yet. Stored on the row; never logged.
    typed = {
        slot: DocumentInput(option=value.option, fields=value.fields)
        for slot, value in sub.documents.items()
    }
    subject_name = provider.subject_name(requirements, sub.fields, sub.address, typed)

    # Every 409 comes before the uploads are checked and copied, and before
    # any filing with the carrier. Number: several live verifications per
    # subject type, but not the same name twice, and only one with no name.
    # SMS: one live registration. (The live unique indexes on carrier_verifications.)
    live = select(CarrierVerification).where(
        CarrierVerification.organization_id == principal.organization_id,
        CarrierVerification.provider == provider_name,
        CarrierVerification.purpose == sub.purpose,
        CarrierVerification.country_code == sub.country_code,
        CarrierVerification.number_type == sub.number_type,
        CarrierVerification.state.in_(_LIVE_STATES),
    )
    if sub.purpose == "sms" and sub.number_type == "toll_free":
        live = live.where(CarrierVerification.number_id == sub.number_id)
    if sub.purpose == "number":
        live = live.where(
            CarrierVerification.subject_type == sub.subject_type,
            (
                CarrierVerification.subject_name.is_(None)
                if subject_name is None
                else func.lower(CarrierVerification.subject_name)
                == func.lower(subject_name)
            ),
        )
    existing = (await db.execute(live)).scalars().first()
    if existing is not None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                _duplicate_detail(sub.subject_type, subject_name)
                if sub.purpose == "number"
                else f"a verification already exists ({existing.state}); id {existing.id}"
            ),
        )

    # Hail's own check of the form's limits (max_length), for every carrier:
    # some plug-ins check nothing. Before any file is read or anything filed.
    too_long = length_problems(requirements, sub.fields, typed)
    if too_long:
        raise HTTPException(status_code=422, detail=_problem_details(too_long))

    documents: dict[str, DocumentInput] = {}
    files: list[VerificationCheckFile] = []
    for key in form:
        if key.startswith("upload.") and key[len("upload.") :] not in sub.documents:
            raise unprocessable(
                "this upload names a slot that is not in documents",
                loc=["body", key],
            )
    # The upload rows first (one session: one query at a time), and the file
    # parts, then the total size from both, all before any object is fetched.
    rows: dict[str, FileUpload] = {}
    parts: dict[str, UploadedFile] = {}
    total = 0
    for slot in sub.documents:
        part = form.get(f"file.{slot}")
        has_part = part is not None and hasattr(part, "read")
        upload_ref = form.get(f"upload.{slot}")
        if upload_ref is not None and upload_ref != "":
            if has_part:
                raise unprocessable(
                    f"send file.{slot} or upload.{slot}, not both",
                    loc=["body", f"upload.{slot}"],
                )
            rows[slot] = await _usable_upload(
                db, principal.organization_id, slot, upload_ref
            )
        elif has_part:
            parts[slot] = await _read_file(part)
            total += len(parts[slot].data)
            if total > MAX_REQUEST_BYTES:
                raise _files_too_large()
    # Per slot: one upload in two slots is sent to the carrier twice.
    total += sum(row.size_bytes or 0 for row in rows.values())
    if total > MAX_REQUEST_BYTES:
        raise _files_too_large()
    if rows and s3 is None:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="file uploads are not configured on this server",
        )
    fetches = asyncio.Semaphore(_MAX_PARALLEL_FETCHES)

    async def fetch(row: FileUpload) -> UploadedFile:
        async with fetches:
            return await _read_upload(s3, row)

    # Each distinct upload once. Every fetch finishes (the total was checked
    # above, so this is bounded), then the first failure in slot order is
    # raised, with its cause: the same request always gets the same error.
    distinct = {row.id: row for row in rows.values()}
    results = await asyncio.gather(
        *(fetch(row) for row in distinct.values()), return_exceptions=True
    )
    fetched = dict(zip(distinct, results))
    for slot in sub.documents:
        if slot in rows and isinstance(fetched[rows[slot].id], BaseException):
            raise fetched[rows[slot].id]
    total = 0
    for slot, value in sub.documents.items():
        file: UploadedFile | None = (
            fetched[rows[slot].id] if slot in rows else parts.get(slot)
        )
        if file is not None:
            # Again with the bytes read: an object may differ from its row.
            total += len(file.data)
            if total > MAX_REQUEST_BYTES:
                raise _files_too_large()
            files.append(
                VerificationCheckFile(
                    slot=slot,
                    upload_id=rows[slot].id if slot in rows else None,
                    size_bytes=len(file.data),
                    sha256=hashlib.sha256(file.data).hexdigest(),
                )
            )
        documents[slot] = DocumentInput(
            option=value.option, fields=value.fields, file=file
        )

    if sub.purpose == "sms" and sub.number_type != "toll_free":
        mobile_problems = await _sms_mobile_problems(db, sub.subject_type, sub.fields)
        if mobile_problems:
            raise _problems_422(
                mobile_problems, principal.organization_id, provider_name
            )

    draft_kwargs: dict = {}
    if sub.purpose == "sms" and sub.number_type == "toll_free":
        # Free TFV rows must never reach an older website's 10DLC sweep.
        if not await tollfree_verification_supported():
            raise HTTPException(
                status_code=503,
                detail="Toll-free texting verification is temporarily unavailable. Try again later.",
            )
        number = await _sms_number(db, principal.organization_id, sub.number_id)
        draft_kwargs.update(
            provider_resource_id=number.provider_resource_id, e164=number.e164
        )
    if sub.purpose == "sms" and sub.number_type != "toll_free":
        # Hail Cloud charges each step of the filing from prepaid credit (the
        # website prices it; this repo holds no prices). The carrier's own
        # cost is the floor the balance must cover before anything is filed.
        kept_brand_id = await _kept_brand(
            db, principal.organization_id, provider_name, sub.subject_type
        )
        try:
            floor_cents = sms_registration_fees.filing_fee_cents(
                provider_name, sub.subject_type, brand_kept=bool(kept_brand_id)
            )
            full_cents = sms_registration_fees.filing_fee_cents(
                provider_name, sub.subject_type
            )
        except KeyError:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="no SMS registration for this carrier",
            )
        new_brand_funded = True
        if principal.auth_kind != "shared":
            balance = await get_balance_cents(db, principal.organization_id)
            # Filings the website has not charged yet (it charges a moment
            # later) count too, or an account could file again and again on
            # the same credit.
            unsettled = await _unsettled_sms_filings(db, principal.organization_id)
            if balance < floor_cents + full_cents * unsettled:
                raise HTTPException(
                    status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                    detail="not enough credit to file; add credit first",
                )
            new_brand_funded = balance >= full_cents * (unsettled + 1)
        if kept_brand_id:
            draft_kwargs["kept_brand_id"] = kept_brand_id
            # The floor above assumes the brand is reused. Only the carrier
            # can tell, so the plug-in stops if it needs a new one.
            draft_kwargs["new_brand_funded"] = new_brand_funded
        draft_kwargs["messaging_service_sid"] = (
            await sms_setup.org_messaging_service_sid(
                db, principal.organization_id, provider_name
            )
        )

    return _Prepared(
        sub=sub,
        offer=offer,
        provider_name=provider_name,
        provider=provider,
        hold_cents=hold_cents,
        requirements=requirements,
        subject_name=subject_name,
        documents=documents,
        draft_kwargs=draft_kwargs,
        uploads=list(distinct.values()),
        files=files,
    )


@router.post(
    "",
    response_model=VerificationResponse,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="create_verification",
    responses={
        402: {
            "description": (
                "Not enough credit. A number verification needs the offer's "
                "setup and first month (`setup_cents` + `monthly_cents` of the "
                "quote) and sets that amount aside until you buy the number. "
                "A US SMS registration needs its filing fee. Nothing is filed."
            ),
        },
    },
)
async def create_verification(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
    s3: Annotated[S3MailClient | None, Depends(get_s3_mail_optional)],
) -> VerificationResponse:
    """Submit your details and documents (multipart/form-data).

    Parts: `purpose` (`number`, the default, or `sms` to register to send SMS
    in the US; use US/local for 10DLC, or US/CA and toll_free with number_id for free per-number toll-free verification; 10DLC fees
    are charged from credit per step, when the details are submitted and when
    the carriers start their review, and 402 means there is not enough credit
    to file, also when a kept brand cannot be reused and the credit does not
    cover a new one), `country_code`, `number_type`, `subject_type` (required:
    `person` for you, `business` for your company; ask the user which),
    `quote_id` (required for `number`: the offer to verify for), `number_id`
    (optional, for `sms`: one of your US numbers; needed when your US numbers
    are with more than one carrier),
    `fields` (JSON object), `address` (JSON object, when required), `documents`
    (JSON object: slot name to `{"option": ..., "fields": {...}}`), and for
    each slot either a file part named `file.<slot>` or a text part named
    `upload.<slot>` holding the id of a `verification_document` uploaded
    with `POST /uploads` less than 1 hour ago (not both). Files are JPG, PNG
    or PDF, 10 MB each at most. Get the slot and field names from
    `GET /verifications/requirements`. Used uploads are deleted once the
    verification is saved; after a failed submit they stay for a retry.

    The name of the person or company comes from the form you send (for
    example the business name, or the first and last name) and is shown
    back as `subject_name`. For a number you may hold several verifications
    of each subject type (several people, several companies); a second live
    one of the same subject type and the same name is a 409, and so is a
    second live one of the same subject type when the form holds no name.
    A 409 comes before your files are checked and before anything is filed.

    A number verification needs credit for the offer's setup and first month
    (`setup_cents` + `monthly_cents` of the quote); without it, 402 and
    nothing is filed. That amount is set aside from your balance while the
    carrier checks your papers. It pays for setup and the first month when
    you buy the number with this verification, and comes back if the
    verification is rejected or cancelled, or if no number is bought with it
    within 30 days of approval. A by-request offer sets nothing aside.

    The carrier checks the details right away. Problems come back as 422 with
    the field named, and nothing is kept. When it passes, the verification is
    submitted to the carrier for its review; read it back to follow the state.
    A `provider` part is deprecated and ignored: Hail picks the carrier.
    """
    prepared = await _prepare_submission(
        request, principal, db, registry, s3, lock=True
    )
    try:
        draft = await prepared.provider.create_draft(
            organization_id=str(principal.organization_id),
            contact_email=settings.hail_support_email,
            requirements=prepared.requirements,
            fields=prepared.sub.fields,
            address=prepared.sub.address,
            documents=prepared.documents,
            **prepared.draft_kwargs,
        )
    except VerificationProviderError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="carrier unavailable; try again later",
        ) from exc
    except Exception as exc:
        logger.exception("verification draft failed")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="carrier unavailable; try again later",
        ) from exc
    if draft.needs_new_brand_credit:
        raise HTTPException(
            status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                "The details changed or cannot be reused, so a submission fee "
                f"applies. Add credit at {BILLING_URL} and file again."
            ),
        )
    if prepared.sub.purpose == "sms":
        # Stored privately for rejected-application repair; no files, provider
        # feedback, or arbitrary extra multipart fields belong in this record.
        draft.refs["application"] = _application_record(
            prepared.sub.subject_type,
            prepared.requirements,
            prepared.sub.fields,
            prepared.sub.address,
        )

    _stamp_campaign_filed(prepared.provider, draft.refs)
    if draft.billed_refs:
        if prepared.sub.purpose == "sms":
            draft.billed_refs["application"] = copy.deepcopy(draft.refs["application"])
        _stamp_campaign_filed(prepared.provider, draft.billed_refs)
    if draft.problems:
        if draft.billed_refs:
            await _record_billed_refusal(
                db,
                principal,
                prepared.provider_name,
                prepared.sub,
                prepared.subject_name,
                draft.billed_refs,
                "; ".join(p.message for p in draft.problems),
                prepared.requirements.version,
            )
        raise _problems_422(
            draft.problems, principal.organization_id, prepared.provider_name
        )

    row = CarrierVerification(
        organization_id=principal.organization_id,
        provider=prepared.provider_name,
        purpose=prepared.sub.purpose,
        country_code=prepared.sub.country_code,
        number_type=prepared.sub.number_type,
        number_id=(
            prepared.sub.number_id
            if prepared.sub.number_type == "toll_free" and prepared.sub.purpose == "sms"
            else None
        ),
        subject_type=prepared.sub.subject_type,
        subject_name=prepared.subject_name,
        state="awaiting_review",
        provider_refs=draft.refs,
        requirements_version=prepared.requirements.version,
    )
    if prepared.hold_cents:
        # Not held across the carrier calls above: a top-up must not wait.
        await org_lock(db, principal.organization_id)
        if await get_balance_cents(db, principal.organization_id) < prepared.hold_cents:
            await db.rollback()
            # Nothing of the customer's is left at the carrier.
            try:
                await prepared.provider.discard(draft.refs)
            except Exception:
                logger.exception("verification draft discard failed")
            raise _hold_402(prepared.hold_cents)
    db.add(row)
    try:
        if prepared.hold_cents:
            await db.flush()
            await verification_hold.place_hold(
                db,
                principal.organization_id,
                row.id,
                prepared.hold_cents,
                channel="voice" if "voice" in prepared.offer["capabilities"] else "sms",
            )
        await db.commit()
    except Exception as exc:
        # The carrier record exists but the row does not: remove it, or it
        # would be left behind with the customer's details.
        await db.rollback()
        # Not a reused brand: this request did not make it, and a filing that
        # won the race may be using it.
        if not draft.refs.get("brand_reused"):
            await prepared.provider.discard(draft.refs)
        if isinstance(exc, IntegrityError):
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail=(
                    _duplicate_detail(prepared.sub.subject_type, prepared.subject_name)
                    if prepared.sub.purpose == "number"
                    else "a verification already exists for this number type"
                ),
            ) from exc
        raise
    await db.refresh(row)
    # No human gate: the draft goes to the carrier now. If the carrier is
    # down, the row stays 'awaiting_review' and the sweeper retries it.
    # A new row: _submit_row pings the fee rater whatever the outcome.
    await _submit_row(
        db,
        row,
        prepared.provider,
        before=None,
        actor=actor_of(principal),
        action="verification.submit",
    )
    await _delete_used_uploads(db, s3, prepared.uploads)
    return _out(row, registry)


# ``upload.<slot>`` objects fetched from the bucket at once.
_MAX_PARALLEL_FETCHES = 4


def _files_too_large() -> HTTPException:
    return unprocessable("the files total more than 30 MB", loc=["body", "files"])


async def _usable_upload(
    db: AsyncSession, organization_id: UUID, slot: str, value: object
) -> FileUpload:
    """The ``upload.<slot>`` row: this organization's verification document,
    uploaded less than ``VERIFICATION_UPLOAD_TTL`` ago. Else 422 naming the
    slot only."""
    where = ["body", f"upload.{slot}"]
    try:
        upload_id = UUID(str(value))
    except ValueError:
        raise unprocessable(f"upload.{slot} must be an upload id", loc=where) from None
    row = (
        await db.execute(
            select(FileUpload).where(
                FileUpload.id == upload_id,
                FileUpload.organization_id == organization_id,
                FileUpload.purpose == "verification_document",
                FileUpload.uploaded_at.is_not(None),
                FileUpload.uploaded_at
                > func.statement_timestamp() - VERIFICATION_UPLOAD_TTL,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise unprocessable(
            f"upload.{slot}: no usable upload with this id; it must be a "
            "verification_document of this organization, uploaded less than "
            "1 hour ago",
            loc=where,
        )
    return row


async def _read_upload(s3: S3MailClient, row: FileUpload) -> UploadedFile:
    """The upload's bytes from the mail bucket, in memory only."""
    try:
        data = await s3.fetch_raw(row.s3_key)
    except Exception as exc:
        logger.warning("verification upload read failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage unavailable; try again",
        ) from exc
    if row.content_type not in ALLOWED_FILE_TYPES or len(data) > MAX_FILE_BYTES:
        # The PUT checked both; this is a guard against an edited row.
        raise unprocessable("files must be JPG, PNG or PDF, 10 MB or smaller")
    return UploadedFile(filename="upload", content_type=row.content_type, data=data)


async def _delete_used_uploads(
    db: AsyncSession, s3: S3MailClient | None, uploads: list[FileUpload]
) -> None:
    """Delete the uploads a saved verification used. Best effort: the GC
    removes any left after ``VERIFICATION_UPLOAD_TTL``."""
    if not uploads or s3 is None:
        return
    # Plain values first: a rollback below expires the ORM objects.
    targets = [(upload.id, upload.s3_key) for upload in uploads]
    results = await asyncio.gather(
        *(delete_upload_objects(s3, s3_key) for _, s3_key in targets),
        return_exceptions=True,
    )
    deleted = []
    for (upload_id, _), result in zip(targets, results):
        if isinstance(result, BaseException):
            # The row stays, so the GC tries the object again.
            logger.warning(
                "could not delete used verification upload %s (%s); the GC "
                "removes it later",
                upload_id,
                type(result).__name__,
            )
        else:
            deleted.append(upload_id)
    if not deleted:
        return
    try:
        await db.execute(delete(FileUpload).where(FileUpload.id.in_(deleted)))
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.warning(
            "could not delete %d used verification upload row(s) (%s); the GC "
            "removes them later",
            len(deleted),
            type(exc).__name__,
        )


@router.post(
    "/check",
    response_model=VerificationCheckResponse,
    operation_id="check_verification",
    responses={
        402: {
            "description": (
                "Not enough credit to submit this form. Same rule as "
                "`POST /verifications`."
            ),
        },
        409: {"description": "Submit would be a duplicate. Same rule as submit."},
    },
)
async def check_verification(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
    s3: Annotated[S3MailClient | None, Depends(get_s3_mail_optional)],
) -> VerificationCheckResponse:
    """Check a verification form without filing anything (multipart/form-data).

    Takes the same parts as `POST /verifications` and runs every check Hail
    can run before filing: the purpose rules, the credit check (402), the
    carrier's form (a read), the duplicate check (409), field lengths, the
    files (`file.<slot>` and `upload.<slot>`: type, size, owner, age), and
    the form's own rules (required fields, documents, address; for `sms`
    the US texting application). Errors are the same codes and shapes
    submit gives.

    Nothing is filed at the carrier, saved, held or charged, and uploads
    are not used up. The carrier runs its own checks when you submit.
    """
    prepared = await _prepare_submission(
        request, principal, db, registry, s3, lock=False
    )
    sub, provider = prepared.sub, prepared.provider
    if sub.purpose == "sms" and isinstance(provider, SmsRegistrationProvider):
        problems = provider.validate_application(
            sub.fields, sub.address, sub.subject_type
        )
    else:
        problems = validate_input(
            prepared.requirements, sub.fields, sub.address, prepared.documents
        )
    # Nothing was written; end the read transaction.
    await db.rollback()
    if problems:
        raise HTTPException(status_code=422, detail=_problem_details(problems))
    return VerificationCheckResponse(
        ok=True,
        files=prepared.files,
        note="The carrier runs its own checks when you submit.",
    )


async def _submit_row(
    db: AsyncSession,
    row: CarrierVerification,
    provider: VerificationProvider,
    *,
    before: BillingView | None,
    actor: tuple[UUID | None, str],
    action: str,
    extra: dict | None = None,
    raise_refused: bool = False,
) -> bool:
    """Send an 'awaiting_review' draft the carrier has already evaluated
    (create_draft or check). True when it is now 'submitted'; False when the
    carrier was unreachable (the row stays 'awaiting_review' for the next try)
    or refused the draft (the row is 'rejected'; 'awaiting_review' when the
    discard failed; as it is when it changed meanwhile).
    Never raises for carrier trouble: creation must still answer 201 and the
    sweeper must keep going. With ``raise_refused`` a refusal re-raises
    SubmitRefused after the row is rejected (or kept), so a superadmin sees
    why.

    The row is saved as 'submitting' before the carrier call so a crash in
    between cannot submit twice; _refresh settles a stuck 'submitting' row.
    approved_by names the superadmin who approved, or nobody for the
    automatic submits.

    ``before`` is the row's ``_billing_view`` before the caller touched it
    (None for a new row). An exit pings the fee rater only when the row's
    state or refs differ from it: a retry that found the carrier down
    changed nothing and is not told."""
    actor_user_id, actor_kind = actor
    approved_by = actor_user_id if actor_kind == "superadmin" else None
    row.state, row.updated_at = "submitting", datetime.now(timezone.utc)
    row.approved_by = approved_by
    await db.commit()
    try:
        async with _plugin_call(row, provider):
            await provider.submit(row.provider_refs)
    except SubmitRefused as exc:
        # The same draft would be refused on every retry: reject it so the
        # customer sees why and can start again. If the discard fails, wait
        # for the next try, as when the carrier is down.
        outcome = await _refuse_row(  # pings when rejected
            db,
            row,
            provider,
            exc.problems,
            kept_state="awaiting_review",
        )
        if outcome != "rejected":
            _notify_if_changed(row, before)
        # A row moved on is not the admin's refusal to see, unless it is
        # waiting again: then the same draft would be refused again.
        if raise_refused and (outcome != "moved" or row.state == "awaiting_review"):
            raise
        return False
    except Exception:
        logger.exception("verification submit failed for %s", row.id)
        row.state, row.updated_at = "awaiting_review", datetime.now(timezone.utc)
        row.approved_by = None
        await db.commit()
        _notify_if_changed(row, before)
        return False
    now = datetime.now(timezone.utc)
    row.state, row.submitted_at, row.updated_at = "submitted", now, now
    await db.commit()
    _notify_if_changed(row, before)
    await write_audit_log(
        row.organization_id,
        None,
        action,
        "carrier_verification",
        row.id,
        {"approved_by": str(approved_by) if approved_by else None, **(extra or {})},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return True


# An 'awaiting_review' row this old was not submitted at creation (the
# carrier was down); the sweeper retries it.
_RETRY_SUBMIT_AFTER = timedelta(minutes=2)
# How long a cancelled or rejected US texting application keeps the saved
# answers (address, tax id, mobile, email) so the customer can file again.
APPLICATION_KEPT_FOR = timedelta(days=30)


def _application_expired(row: CarrierVerification, now: datetime) -> bool:
    """True once a cancelled or rejected application is past
    ``APPLICATION_KEPT_FOR`` (from ``cancelled_at``, else ``updated_at``)."""
    return (
        row.state in ("cancelled", "rejected")
        and (row.cancelled_at or row.updated_at) <= now - APPLICATION_KEPT_FOR
    )


async def _lock_waiting(
    db: AsyncSession, verification_id: UUID
) -> CarrierVerification | None:
    """The row, locked, if it is still 'awaiting_review'. None when an admin
    holds it (admin_approve locks the same way) or it moved on."""
    return (
        await db.execute(
            select(CarrierVerification)
            .where(
                CarrierVerification.id == verification_id,
                CarrierVerification.state == "awaiting_review",
            )
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()


async def _commit_and_notify(
    db: AsyncSession, row: CarrierVerification, before: BillingView
) -> None:
    """Commit (after a failed plug-in call: the refs ``_plugin_call`` kept,
    an id it filed may be billed), and ping the rater when the row
    changed."""
    await db.commit()
    _notify_if_changed(row, before)


async def _retry_submit(
    db: AsyncSession, verification_id: UUID, registry: Registry
) -> bool:
    """One sweeper retry of an unsent draft. The carrier evaluates it first:
    a draft it refuses becomes 'rejected' with the carrier's reason, so the
    customer sees why and can start over; it is not retried again. The
    submit costs nothing for a US texting draft; the poll that files its
    campaign checks the credit (``_refresh``)."""
    row = await _lock_waiting(db, verification_id)
    if row is None:
        return False
    provider = registry(row.provider, row.purpose)
    if provider is None:
        await db.rollback()
        return False
    before = _billing_view(row)
    try:
        async with _plugin_call(row, provider):
            problems = await provider.check(row.provider_refs)
    except Exception:
        logger.exception("verification check failed for %s", row.id)
        await _commit_and_notify(db, row, before)
        return False
    if problems:
        outcome = await _refuse_row(db, row, provider, problems)  # pings when rejected
        if outcome != "rejected":
            _notify_if_changed(row, before)
        return False
    return await _submit_row(
        db,
        row,
        provider,
        before=before,
        actor=(None, "system"),
        action="verification.submit",
    )


async def _refuse_row(
    db: AsyncSession,
    row: CarrierVerification,
    provider: VerificationProvider,
    problems: list[Problem],
    *,
    kept_state: str | None = None,
) -> RejectOutcome:
    """Reject a draft the carrier refused, with its per-field reasons."""
    _log_carrier_refusal(problems, row.organization_id, row.id, row.provider)
    return await _reject_row(
        db,
        row,
        provider,
        "; ".join(
            f"{p.field}: {p.message}" if p.field else p.message for p in problems
        ),
        carrier_reason="; ".join(p.raw for p in problems if p.raw) or None,
        kept_state=kept_state,
    )


async def _relock(
    db: AsyncSession, row: CarrierVerification, state: str, changed_at: datetime
) -> bool:
    """Lock the row again after a commit ended its lock, waiting for a poll
    that holds it, and reload it. False when it changed meanwhile (its
    state or ``updated_at`` differ): leave it as it is."""
    relocked = (
        await db.execute(
            select(CarrierVerification)
            .where(
                CarrierVerification.id == row.id,
                CarrierVerification.state == state,
                CarrierVerification.updated_at == changed_at,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return relocked is not None


# What _reject_row did: 'rejected'; 'kept' (the discard failed, the row
# keeps its refs); 'moved' (the row changed meanwhile and is left as it is).
RejectOutcome = Literal["rejected", "kept", "moved"]


async def _reject_row(
    db: AsyncSession,
    row: CarrierVerification,
    provider: VerificationProvider,
    reason: str,
    carrier_reason: str | None = None,
    *,
    kept_state: str | None = None,
) -> RejectOutcome:
    """The carrier refused the draft before submission: record why (Hail's
    wording on the row, the carrier's own text in the audit row), discard
    the draft so nothing of the customer's is left at the carrier, and audit
    it as a system action (no person rejected it). If the discard fails the
    row keeps its refs, moves to ``kept_state`` (default: keeps its state)
    and is tried again next tick; the sweeper never dies on carrier trouble.
    """
    state, changed_at = row.state, row.updated_at
    rate = (
        row.purpose == "sms"
        and row.number_type != "toll_free"
        and _campaign_filed(provider, row.provider_refs)
    )
    if rate or get_history(row, "provider_refs").has_changes():
        # Saved before the discard: the ids the plug-in call wrote (a filed
        # campaign is billed) must survive a failed discard, or a restart
        # during it. The commit ends the row lock. Nothing changed: nothing
        # is written, so the lock query cannot write stale refs.
        await db.commit()
    if rate:
        # The rater charges the review by the campaign id: before the wipe.
        await rate_sms_registration(str(row.id))
    # Lock the row (again after a commit; the submit path holds no lock).
    if not await _relock(db, row, state, changed_at):
        await db.rollback()
        await db.refresh(row)
        return "moved"
    try:
        async with _plugin_call(row, provider):
            await provider.discard(row.provider_refs)
    except Exception:
        logger.exception("verification discard failed for %s", row.id)
        if kept_state is not None:
            row.state, row.approved_by = kept_state, None
            row.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return "kept"
    # Wiped before the state moves: the row has not ended yet, so its
    # answers are kept however long it waited.
    row.provider_refs = _wiped_refs(row)
    row.state, row.rejection_reason = "rejected", reason
    row.approved_by = None
    row.updated_at = datetime.now(timezone.utc)
    await _release_number_hold(db, row)
    await db.commit()
    _notify_billing([row])
    await write_audit_log(
        row.organization_id,
        None,
        "verification.reject",
        "carrier_verification",
        row.id,
        {"rejected_by": None, "reason": reason, "carrier_reason": carrier_reason},
        actor_user_id=None,
        actor_kind="system",
    )
    return "rejected"


async def sweep_verifications(db: AsyncSession, registry: Registry) -> dict[str, int]:
    """Background pass: submit drafts that are still waiting, and pull the
    carrier's answer for submitted ones. Returns counts for the log."""
    now = datetime.now(timezone.utc)
    submitted = refreshed = 0
    waiting = (
        (
            await db.execute(
                select(CarrierVerification.id).where(
                    CarrierVerification.state == "awaiting_review",
                    CarrierVerification.updated_at <= now - _RETRY_SUBMIT_AFTER,
                )
            )
        )
        .scalars()
        .all()
    )
    for verification_id in waiting:
        if await _retry_submit(db, verification_id, registry):
            submitted += 1
    polled = (
        (
            await db.execute(
                select(CarrierVerification).where(
                    CarrierVerification.state.in_(_POLLED_STATES)
                )
                # Oldest first: with credit for one campaign, the oldest
                # registration gets it, as in the list.
                .order_by(CarrierVerification.created_at)
            )
        )
        .scalars()
        .all()
    )
    for row in polled:
        before = row.state
        await _refresh(db, row, registry)
        if row.state != before:
            refreshed += 1
    # Before the repair pass: a carrier error there must not keep holds.
    await _release_unused_holds(db, now)
    await reconcile_approved_registrations(db, registry)
    forgotten = await forget_old_applications(db, now)
    return {"submitted": submitted, "refreshed": refreshed, "forgotten": forgotten}


async def forget_old_applications(db: AsyncSession, now: datetime) -> int:
    """Delete the saved answers of US texting applications cancelled or
    rejected more than ``APPLICATION_KEPT_FOR`` ago. Only the ``application``
    key goes: the other refs still dismiss the filing and bill it right.
    Returns how many rows lost their answers."""
    ended = func.coalesce(
        CarrierVerification.cancelled_at, CarrierVerification.updated_at
    )
    ids = (
        (
            await db.execute(
                select(CarrierVerification.id).where(
                    CarrierVerification.purpose == "sms",
                    CarrierVerification.state.in_(("cancelled", "rejected")),
                    CarrierVerification.provider_refs.has_key("application"),
                    ended <= now - APPLICATION_KEPT_FOR,
                )
            )
        )
        .scalars()
        .all()
    )
    forgotten = 0
    for verification_id in ids:
        row = await db.get(CarrierVerification, verification_id)
        if row is None:
            continue
        await db.refresh(row, with_for_update=True)
        refs = row.provider_refs or {}
        # Re-checked under the lock: an amend or a new state may have won.
        if "application" in refs and _application_expired(row, now):
            row.provider_refs = {k: v for k, v in refs.items() if k != "application"}
            flag_modified(row, "provider_refs")
            forgotten += 1
        await db.commit()
    return forgotten


async def _release_number_hold(db: AsyncSession, row: CarrierVerification) -> None:
    """Give back the credit a number verification set aside, in the
    caller's transaction. Call it wherever a row becomes rejected or
    cancelled. A second call, or a row with no hold, adds nothing."""
    if row.purpose == "number":
        await verification_hold.release_hold(db, row.organization_id, row.id)


async def _release_unused_holds(db: AsyncSession, now: datetime) -> None:
    """Give back the hold of approved number verifications no number was
    bought with within ``HOLD_AFTER_APPROVAL_DAYS`` of approval. A purchase
    releases the hold itself, so a row with an open hold this old was not
    used. Each release runs under the organization lock, like a purchase."""

    def ledger_row(source: str):
        # The SQL form of hold_ref / release_ref: "<source>:<verification id>".
        return select(AccountCredit.id).where(
            AccountCredit.organization_id == CarrierVerification.organization_id,
            AccountCredit.ref
            == func.concat(f"{source}:", CarrierVerification.id.cast(String)),
        )

    stale = (
        await db.execute(
            select(CarrierVerification.id, CarrierVerification.organization_id).where(
                CarrierVerification.purpose == "number",
                CarrierVerification.state == "approved",
                CarrierVerification.approved_at
                <= now - timedelta(days=HOLD_AFTER_APPROVAL_DAYS),
                ledger_row(verification_hold.HOLD_SOURCE).exists(),
                ~ledger_row(verification_hold.RELEASE_SOURCE).exists(),
            )
        )
    ).all()
    await db.commit()
    for verification_id, org in stale:
        # One failed release must not stop the others; it is tried again
        # on the next pass.
        try:
            await org_lock(db, org)
            await verification_hold.release_hold(db, org, verification_id)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("verification hold release failed for %s", verification_id)


async def _org_row_or_404(
    db: AsyncSession, principal: Principal, verification_id: UUID
) -> CarrierVerification:
    row = (
        await db.execute(
            select(CarrierVerification).where(
                CarrierVerification.id == verification_id,
                CarrierVerification.organization_id == principal.organization_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="not found"
        )
    return row


@router.get(
    "", response_model=list[VerificationResponse], operation_id="list_verifications"
)
async def list_verifications(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
    purpose: str | None = None,
) -> list[VerificationResponse]:
    """Your organization's verifications, newest first. Pass `purpose`
    (`number` or `sms`) to list only that kind."""
    query = select(CarrierVerification).where(
        CarrierVerification.organization_id == principal.organization_id
    )
    if purpose is not None:
        query = query.where(CarrierVerification.purpose == purpose)
    rows = (
        (await db.execute(query.order_by(CarrierVerification.created_at.desc())))
        .scalars()
        .all()
    )
    # Ask the carrier about rows not asked in the last minute, all at once.
    # Only the carrier calls run concurrently; the session is used in order.
    now = time.monotonic()
    asked = [r for r in rows if _poll_due(r, now)]
    # A campaign still to file needs the credit check under the org lock:
    # those rows go through _refresh one at a time, after the others, so
    # the lock is not held through the other rows' carrier calls.
    gated = [r for r in asked if _campaign_gated(r, registry)]
    asked = [r for r in asked if r not in gated]
    # Locked and re-read first (see _lock_for_poll).
    due = [r for r in asked if await _lock_for_poll(db, r) and _pollable(r)]
    before = [copy.deepcopy(r.provider_refs) for r in due]
    # Gated rows go through _refresh, which checks their credit. A row that
    # became gated after it was read (re-read under the lock) got no check:
    # fail closed, no campaign on this poll.
    statuses = await asyncio.gather(
        *(
            _fetch_status(
                r, registry, may_file_campaign=not _campaign_gated(r, registry)
            )
            for r in due
        )
    )
    changed = [
        r
        for r, s, b in zip(due, statuses, before)
        if await _poll_and_apply(db, r, s, registry, b)
    ]
    if asked:
        await db.commit()  # saves changes and ends the row locks
    _notify_billing(changed)
    for row in sorted(gated, key=lambda r: r.created_at):  # oldest first
        await _refresh(db, row, registry)
    waiting = await _campaign_unfunded(db, rows, registry)
    return [_out(r, registry, credit_needed_cents=waiting.get(r.id)) for r in rows]


@router.get(
    "/{verification_id}",
    response_model=VerificationResponse,
    operation_id="get_verification",
)
async def get_verification(
    verification_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
) -> VerificationResponse:
    """One verification of your organization. Once submitted, its state is
    updated from the carrier each time you read it."""
    row = await _org_row_or_404(db, principal, verification_id)
    await _refresh(db, row, registry)
    return await _out_checked(db, row, registry)


@router.get(
    "/{verification_id}/application",
    response_model=SmsApplicationResponse,
    operation_id="get_verification_application",
)
async def get_verification_application(
    verification_id: UUID,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
) -> SmsApplicationResponse:
    """Read your organization's saved SMS answers, including after rejection.
    Answers of a cancelled or rejected registration are kept for 30 days
    after it ended; after that this returns 404.

    Older applications may recover their verified sender identity through
    read-only provider calls; a 404 asks for original details and support when
    these cannot be recovered. The ordinary list never contains application
    fields or addresses. No records are filed or charged here.
    """
    row = await _org_row_or_404(db, principal, verification_id)
    # Answers past APPLICATION_KEPT_FOR are gone, even before the background
    # pass deletes them, and are not rebuilt from the carrier either.
    expired = _application_expired(row, datetime.now(timezone.utc))
    application = None if expired else (row.provider_refs or {}).get("application")
    if row.purpose == "sms" and not isinstance(application, dict) and not expired:
        provider = registry(row.provider, "sms")
        if isinstance(provider, SmsRegistrationProvider):
            try:
                identity = await provider.application_identity(
                    row.provider_refs or {}, row.subject_type
                )
            except VerificationProviderError as exc:
                raise _carrier_unavailable() from exc
            if identity and all(
                isinstance((identity.get("address") or {}).get(key), str)
                and identity["address"][key].strip()
                for key in ("street", "city", "region", "postal_code", "country_code")
            ):
                fields = {
                    key: value
                    for key, value in identity.get("fields", {}).items()
                    if isinstance(value, str)
                }
                address = {
                    **identity["address"],
                    "customer_name": row.subject_name or "Sender",
                }
                application = {
                    "schema_version": 1,
                    "subject_type": row.subject_type,
                    "fields": fields,
                    "address": address,
                }
    if row.purpose != "sms" or not isinstance(application, dict):
        raise HTTPException(
            status_code=404,
            detail="Saved answers are unavailable. Complete the current registration form with the original sender details; contact support if the original identity cannot be verified.",
        )
    response.headers["Cache-Control"] = "no-store"
    return SmsApplicationResponse.model_validate(application)


@router.patch(
    "/{verification_id}/application",
    response_model=VerificationResponse,
    operation_id="amend_sms_application",
)
async def amend_sms_application(
    verification_id: UUID,
    body: SmsApplicationAmendRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
) -> VerificationResponse:
    """Repair missing evidence on the same sender, brand and registration.

    No paid provider action occurs here. Only the campaign answers change;
    carrier review resumes on a later poll. Original sender identity/address
    must match stored or authoritative provider details. An identical retry
    is safe even after the campaign has subsequently been filed.
    """
    row = await _org_row_or_404(db, principal, verification_id)
    await org_lock(db, principal.organization_id)
    await db.refresh(row, with_for_update=True)
    provider = registry(row.provider, row.purpose)
    if row.purpose != "sms" or not isinstance(provider, SmsRegistrationProvider):
        raise HTTPException(
            status_code=409, detail="Only an existing SMS registration can be amended."
        )
    raw = json.dumps(
        body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()
    refs = row.provider_refs or {}
    # A row held again after this same amendment (the carrier refused it, or
    # the customer fixed a public page) takes the same answers once more.
    if (
        refs.get("application_amendment_hash") == digest
        and row.state in ("submitted", "approved")
        and not refs.get("needs_information")
    ):
        return await _out_checked(db, row, registry)
    if (
        row.state != "submitted"
        or not refs.get("needs_information")
        or (row.number_type != "toll_free" and provider.campaign_filed(refs))
    ):
        raise HTTPException(
            status_code=409,
            detail="This registration is not waiting for application information. Keep its existing filing and contact support for changes.",
        )
    requirements = await _requirements(
        provider, row.country_code, row.number_type, row.subject_type, where="body"
    )
    problems = length_problems(requirements, body.fields, {})
    problems.extend(
        provider.validate_application(body.fields, body.address, row.subject_type)
    )
    if row.number_type != "toll_free":
        problems.extend(await _sms_mobile_problems(db, row.subject_type, body.fields))
    if problems:
        raise _problems_422(problems, row.organization_id, row.provider)
    original = refs.get("application")
    try:
        if not isinstance(original, dict):
            original = await provider.application_identity(refs, row.subject_type)
        if (
            not original
            or body.address is None
            or not same_sender(original, body.fields, body.address, row.subject_type)
        ):
            raise HTTPException(
                status_code=409,
                detail="The original sender identity or address cannot be verified against these answers. Keep the original sender details and contact support; this registration and its paid brand have been retained.",
            )
        # A previous create may have succeeded while its response was lost.
        # Recover it under the same lock before permitting campaign edits.
        if row.number_type != "toll_free" and await provider.recover_campaign(refs):
            _stamp_campaign_filed(provider, refs)
            flag_modified(row, "provider_refs")
            await db.commit()
            _notify_billing([row])
            raise HTTPException(
                status_code=409,
                detail="The carrier already has this campaign. Its existing filing has been recovered; wait for review instead of amending it.",
            )
    except VerificationProviderError as exc:
        raise _carrier_unavailable() from exc
    if row.number_type == "toll_free":
        if body.fields.get("registration_kind") != "toll_free":
            raise unprocessable(
                "Use the toll-free registration form.",
                loc=["body", "fields", "registration_kind"],
            )
        try:
            await provider.amend_filed_application(
                refs, body.fields, body.address, row.subject_type
            )
        except VerificationProviderError as exc:
            raise _carrier_unavailable() from exc
    else:
        refs["campaign"] = asdict(campaign_text(body.fields, row.subject_type))
    refs["application"] = _application_record(
        row.subject_type, requirements, body.fields, body.address
    )
    refs["application_amendment_hash"] = digest
    refs.pop("needs_information", None)
    refs.pop("campaign_needs_repair", None)
    row.provider_refs = refs
    row.requirements_version = requirements.version
    row.updated_at = datetime.now(timezone.utc)
    row.rejection_reason = None
    flag_modified(row, "provider_refs")
    _last_polled.pop(row.id, None)
    await db.commit()
    actor_user_id, actor_kind = actor_of(principal)
    # Records who amended it, never the answers (personal data).
    await write_audit_log(
        row.organization_id,
        principal.api_key_id,
        "verification.amend",
        "carrier_verification",
        row.id,
        {"requirements_version": row.requirements_version},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return await _out_checked(db, row, registry)


def _can_cancel(row: CarrierVerification) -> bool:
    if row.state in ("awaiting_review", "rejected"):
        return True
    refs = row.provider_refs or {}
    return row.purpose == "sms" and (
        row.state == "approved"
        or (
            row.state == "submitted"
            and refs.get("campaign_accepted") is True
            and refs.get("runtime_ready") is False
        )
    )


@router.delete(
    "/{verification_id}",
    response_model=VerificationResponse,
    operation_id="cancel_verification",
)
async def cancel_verification(
    verification_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
) -> VerificationResponse:
    """Withdraw a verification that has not been sent for review, or dismiss
    a rejected one. The draft is discarded either way, except a person's
    confirmed brand on a rejected US SMS registration: it stays at the
    carrier for the next filing. One that is under review cannot be
    cancelled: wait for the result; if it is rejected, dismiss it.

    An accepted campaign waiting on runtime setup can be cancelled, as can
    an approved US SMS registration (`purpose=sms`):
    its numbers leave the campaign, the campaign is deleted at the carrier,
    and US-to-US texts from those numbers get 422 again. Its monthly fee is
    charged through the month of the cancellation, then stops. Some carriers
    bill a minimum number of monthly fees from approval: a registration
    cancelled sooner is still charged for that many months (prices: see the
    console)."""
    row = await _org_row_or_404(db, principal, verification_id)
    await db.refresh(row, with_for_update=True)
    if not _can_cancel(row):
        raise HTTPException(
            status_code=409,
            detail="This verification is under review or cannot be cancelled in its current state.",
        )
    provider = registry(row.provider, row.purpose)
    # Billing releases the transaction so its own org lock can be acquired.
    # Re-read under both locks afterwards: a poll may have approved/attached
    # the campaign, or another cancellation may have completed meanwhile.
    await _rate_before_wipe(db, row, provider)
    await db.commit()
    await org_lock(db, row.organization_id)
    await db.refresh(row, with_for_update=True)
    if row.state == "cancelled":
        return _out(row, registry)
    if not _can_cancel(row):
        raise HTTPException(
            status_code=409,
            detail="The verification changed while cancellation was prepared; refresh its current status.",
        )
    approved_sms = row.purpose == "sms" and row.state in ("approved", "submitted")
    state_before = row.state
    if approved_sms:
        await _detach_registration(db, row, provider)
    kept: dict = {}
    if isinstance(provider, SmsRegistrationProvider) and row.state == "rejected":
        kept = await provider.discard_rejected(row.provider_refs)
    elif provider is not None:
        await provider.discard(row.provider_refs)
    now = datetime.now(timezone.utc)
    row.state, row.provider_refs = "cancelled", {**_wiped_refs(row), **kept}
    row.updated_at = row.cancelled_at = now
    await _release_number_hold(db, row)
    await db.commit()
    _notify_billing([row])
    if approved_sms:
        actor_user_id, actor_kind = actor_of(principal)
        await write_audit_log(
            row.organization_id,
            principal.api_key_id,
            "verification.cancel",
            "carrier_verification",
            row.id,
            {"purpose": row.purpose, "state_before": state_before},
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
        )
    return _out(row, registry)


async def _detach_registration(
    db: AsyncSession, row: CarrierVerification, provider: VerificationProvider | None
) -> None:
    """Take every number off an approved SMS registration that is being
    cancelled. A number that left the registration's own messaging service
    (a person's Twilio campaign) goes back to the organization's service."""
    if isinstance(provider, SmsRegistrationProvider):
        for number in await detach_org_numbers(db, row, provider):
            try:
                await sms_setup.ensure_sms(db, number)
            except sms_setup.SmsSetupError:
                logger.warning(
                    "number %s: SMS setup failed after leaving its registration; "
                    "POST /numbers/{id}/enable-sms repairs it",
                    number.id,
                )
    await db.execute(
        update(PhoneNumber)
        .where(PhoneNumber.sms_registration_id == row.id)
        .values(sms_registration_id=None)
    )


class ConfirmRequest(BaseModel):
    code: str = Field(
        min_length=4,
        max_length=10,
        description="The code the carrier texted to the person's mobile.",
    )


@router.post(
    "/{verification_id}/confirm",
    response_model=VerificationResponse,
    operation_id="confirm_verification",
)
async def confirm_verification(
    verification_id: UUID,
    body: ConfirmRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
) -> VerificationResponse:
    """Enter the code the carrier texted to confirm a person's US texting
    registration. Use it when the verification shows `confirmation: "code"`.
    On success the campaign is usually filed at once. It waits when your
    credit does not cover its fee (`waiting_for_credit` is true until you
    add credit; `credit_needed_cents` says how much). It also waits while
    another check of it is running or recently failed, or another process
    holds it; a later check files it.
    Poll GET /verifications/{id} until `approved`. A wrong or expired code is
    422 on `code`; after five wrong codes, 429 until the carrier sends a
    new code (once a day). 409 when there is nothing to confirm."""
    row = await _org_row_or_404(db, principal, verification_id)
    # Wait for a poll in flight (it holds the same lock), then re-read.
    row = (
        await db.execute(
            select(CarrierVerification)
            .where(CarrierVerification.id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    provider = registry(row.provider, row.purpose)
    if (
        row.purpose != "sms"
        or row.state != "submitted"
        or not isinstance(provider, SmsRegistrationProvider)
        or provider.confirmation(row.provider_refs or {}) != "code"
    ):
        await db.rollback()
        raise _nothing_to_confirm()
    attempts = int(row.provider_refs.get("confirm_attempts") or 0)
    if attempts >= _MAX_CONFIRM_ATTEMPTS:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many wrong codes. Wait for a new code.",
        )
    try:
        problems = await provider.confirm(row.provider_refs, body.code)
    except NotImplementedError as exc:
        await db.rollback()
        raise _nothing_to_confirm() from exc
    except VerificationProviderError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="carrier unavailable; try again later",
        ) from exc
    attempts += 1
    if problems:
        row.provider_refs["confirm_attempts"] = attempts
    else:
        row.provider_refs["identity_verified"] = True
    row.updated_at = datetime.now(timezone.utc)
    flag_modified(row, "provider_refs")
    await db.commit()  # also ends the row lock
    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        row.organization_id,
        principal.api_key_id,
        "verification.confirm",
        "carrier_verification",
        row.id,
        {"ok": not problems, "attempts": attempts},  # never the code
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    if problems:
        raise _problems_422(problems, row.organization_id, row.provider, row.id)
    # File the campaign now rather than on the next poll.
    await _refresh(db, row, registry, wait_for_org_lock=True)
    return await _out_checked(db, row, registry)


def _nothing_to_confirm() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT, detail="nothing to confirm"
    )


# -- superadmin routes ----------------------------------------------------


class AdminVerification(VerificationResponse):
    # Superadmin only (not in the public spec): names the carrier.
    provider: str
    organization_id: UUID


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@admin_router.get("", response_model=list[AdminVerification])
async def admin_list(
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
    state: str = "awaiting_review",
) -> list[AdminVerification]:
    rows = (
        (
            await db.execute(
                select(CarrierVerification)
                .where(CarrierVerification.state == state)
                .order_by(CarrierVerification.created_at)
            )
        )
        .scalars()
        .all()
    )
    waiting = await _campaign_unfunded(db, rows, registry)
    return [
        _out(r, registry, AdminVerification, credit_needed_cents=waiting.get(r.id))
        for r in rows
    ]


async def _admin_row(db: AsyncSession, verification_id: UUID) -> CarrierVerification:
    # Locked until the request commits, so two admins cannot both act on it.
    row = (
        await db.execute(
            select(CarrierVerification)
            .where(CarrierVerification.id == verification_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="not found"
        )
    if row.state != "awaiting_review":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"verification is {row.state}, not awaiting review",
        )
    return row


def _carrier_unavailable() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_502_BAD_GATEWAY,
        detail="the carrier request failed; try again",
    )


@admin_router.post("/{verification_id}/approve", response_model=AdminVerification)
async def admin_approve(
    verification_id: UUID,
    admin: Annotated[Principal, Depends(require_superadmin)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
) -> AdminVerification:
    """Submit the draft to the carrier for its review. A US texting draft
    is submitted whatever the credit; its campaign is filed only once the
    credit covers it (`waiting_for_credit` until then)."""
    row = await _admin_row(db, verification_id)
    provider = _provider_or_404(registry, row.provider, row.purpose)
    before = _billing_view(row)
    try:
        async with _plugin_call(row, provider):
            problems = await provider.check(row.provider_refs)
    except Exception as exc:
        logger.exception("verification check failed")
        await _commit_and_notify(db, row, before)
        raise _carrier_unavailable() from exc
    if problems:
        await _commit_and_notify(db, row, before)
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            # Superadmin only: the carrier's own words help the review.
            detail="the carrier no longer accepts this draft: "
            + "; ".join(p.raw or p.message for p in problems),
        )
    try:
        submitted = await _submit_row(
            db,
            row,
            provider,
            before=before,
            actor=actor_of(admin),
            action="verification.approve",
            raise_refused=True,
        )
    except SubmitRefused as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            # Superadmin only: the carrier's own words help the review.
            detail="the carrier refused the submission: "
            + "; ".join(p.raw or p.message for p in exc.problems),
        ) from exc
    if not submitted and row.state == "awaiting_review":
        raise _carrier_unavailable()
    return await _out_checked(db, row, registry, AdminVerification)


@admin_router.post("/{verification_id}/reject", response_model=AdminVerification)
async def admin_reject(
    verification_id: UUID,
    body: RejectRequest,
    admin: Annotated[Principal, Depends(require_superadmin)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
) -> AdminVerification:
    row = await _admin_row(db, verification_id)
    provider = registry(row.provider, row.purpose)
    state, changed_at = row.state, row.updated_at
    # Rating commits, which ends the row lock: lock it again, and leave a
    # row that changed meanwhile (the background pass submitted it, say).
    rated = await _rate_before_wipe(db, row, provider)
    if rated and not await _relock(db, row, state, changed_at):
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="this verification changed meanwhile; reload and try again",
        )
    if provider is not None:
        before = _billing_view(row)
        try:
            async with _plugin_call(row, provider):
                await provider.discard(row.provider_refs)
        except Exception as exc:
            # The row keeps its state and its refs; reject again later.
            # Touched, so the background pass waits before it retries a
            # submit the admin meant to stop.
            logger.exception("verification discard failed for %s", row.id)
            row.updated_at = datetime.now(timezone.utc)
            await _commit_and_notify(db, row, before)
            raise _carrier_unavailable() from exc
    # Wiped before the state moves (see _reject_row).
    row.provider_refs = _wiped_refs(row)
    row.state, row.rejection_reason = "rejected", body.reason
    row.updated_at = datetime.now(timezone.utc)
    await _release_number_hold(db, row)
    await db.commit()
    _notify_billing([row])
    actor_user_id, actor_kind = actor_of(admin)
    await write_audit_log(
        row.organization_id,
        None,
        "verification.reject",
        "carrier_verification",
        row.id,
        {"rejected_by": str(admin.user_id) if admin.user_id else None},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return _out(row, registry, AdminVerification)
