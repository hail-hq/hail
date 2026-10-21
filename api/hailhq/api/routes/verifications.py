"""Carrier verification: collect a customer's details and documents, build the
carrier-side record and submit it. A superadmin can still approve a draft the
carrier did not take at creation.

Files and details pass through this process in memory to the carrier. Nothing
identifying is written to the database, disk or logs (see the design spec).
The multipart body is parsed here with an in-memory parser and a hard size cap,
so no part is ever spooled to a temporary file.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from hailhq.api.audit import actor_of, write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.api.sms_registration import (
    attach_org_numbers,
    detach_org_numbers,
    registered_numbers,
)
from hailhq.api.superadmin import require_superadmin
from hailhq.core import sms_registration_fees, sms_setup
from hailhq.core.billing import get_balance_cents
from hailhq.core.config import settings
from hailhq.core.db import get_session, org_lock
from hailhq.core.models import (
    AccountCredit,
    CarrierVerification,
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
    UnsupportedSubjectType,
    UploadedFile,
    VerificationProvider,
    VerificationProviderError,
    default_verification_provider_name,
    get_verification_provider,
)
from hailhq.core.schemas import VerificationResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from starlette.formparsers import MultiPartException, MultiPartParser

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 30 * 1024 * 1024
ALLOWED_FILE_TYPES = {"image/jpeg", "image/png", "application/pdf"}
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

# (name, purpose) -> plug-in
Registry = Callable[[str, str], VerificationProvider | None]


def get_verification_registry() -> Registry:
    """Dependency so tests can swap in a fake carrier."""
    return get_verification_provider


def get_default_provider_name() -> str | None:
    """Carrier used when a request names none. Dependency so tests can swap it."""
    return default_verification_provider_name()


def _resolve_provider_name(name: str | None, default: str | None) -> str:
    resolved = name or default
    if not resolved:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="no verification provider is configured",
        )
    return resolved


async def _quote_provider_name(
    db: AsyncSession,
    organization_id: UUID,
    quote_id: UUID,
    name: str | None,
    country_code: str,
    number_type: str,
    where: str,
) -> str:
    """The carrier of a number quote, so a client can verify for an offer
    without knowing which carrier backs it. The quote must be for the same
    country and number type as the request."""
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
    carrier = offer["provider"]
    if name and name != carrier:
        raise unprocessable(
            "provider does not match the quote", loc=[where, "provider"]
        )
    return carrier


async def _sms_provider_name(
    db: AsyncSession,
    organization_id: UUID,
    name: str | None,
    number_id: UUID | None,
    where: str,
) -> str:
    """The carrier for an SMS registration: the one named, else the carrier
    of the number passed as ``number_id``, else the carrier of the org's
    active US long codes that can text."""
    query = select(PhoneNumber.provider).where(
        PhoneNumber.organization_id == organization_id,
        PhoneNumber.provisioning_state == "active",
        PhoneNumber.country_code == "US",
        PhoneNumber.number_type == "local",
        PhoneNumber.capabilities.any("sms"),
    )
    if number_id is not None:
        carrier = (
            await db.execute(query.where(PhoneNumber.id == number_id))
        ).scalar_one_or_none()
        if carrier is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Number not found among your active US local numbers "
                "that can text",
            )
        if name and name != carrier:
            raise unprocessable(
                "provider does not match the number", loc=[where, "provider"]
            )
        return carrier
    if name:
        return name
    carriers = (await db.execute(query.distinct())).scalars().all()
    if not carriers:
        raise unprocessable(
            "Buy a US number first, then register it for texting.",
            loc=[where, "provider"],
        )
    if len(carriers) > 1:
        raise unprocessable(
            "Your US numbers are with more than one carrier. Pass number_id: "
            "the id of the number to register.",
            loc=[where, "number_id"],
        )
    return carriers[0]


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


def _problems_422(problems: list[Problem]) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=[
            {
                "loc": ["body", *(p.field.split(".") if p.field else [])],
                "msg": p.message,
                "type": "verification_problem",
            }
            for p in problems
        ],
    )


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
    row: CarrierVerification, registry: Registry
) -> ProviderStatus | None:
    """Ask the carrier about a verification. None when there is nothing to
    apply."""
    provider = registry(row.provider, row.purpose)
    if provider is None:
        return None
    try:
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
        return True
    elif row.state == "submitting" and status.state == "draft":
        row.state, row.approved_by, row.updated_at = "awaiting_review", None, now
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
    attached = await attach_org_numbers(db, row, provider)
    failed = len(await registered_numbers(db, row)) - attached
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
    changed, or the plug-in wrote new ids into ``provider_refs`` (an SMS
    plug-in creates the campaign inside status() and stores its id there;
    the JSONB column does not track in-place changes, so mark it)."""
    status = await _settle_sms_approval(db, row, status, registry)
    changed = _apply_status(row, status)
    if row.provider_refs != refs_before:
        flag_modified(row, "provider_refs")
        changed = True
    return changed


async def _lock_for_poll(db: AsyncSession, row: CarrierVerification) -> bool:
    """Lock an SMS registration row before asking the carrier about it, and
    re-read it. False when another poll holds the lock: that poll is in
    flight, so this one skips the row. An SMS plug-in creates the campaign
    inside status(), so two polls at once would create two campaigns. The
    lock is held until the caller commits."""
    if row.purpose != "sms":
        return True
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
    db: AsyncSession, row: CarrierVerification, registry: Registry
) -> None:
    """Pull a submitted verification's outcome from the carrier.

    A row cut off in 'submitting' that the carrier reports as submitted (or
    already reviewed) gets the audit row its interrupted submit never wrote;
    approved_by was saved before the carrier call, so the trail names the
    admin when one approved it."""
    was_submitting = row.state == "submitting"
    if not _pollable(row):
        return
    if not await _lock_for_poll(db, row):
        return
    if not _pollable(row):  # another poll settled it before the lock
        await db.commit()
        return
    before = copy.deepcopy(row.provider_refs)
    status = await _fetch_status(row, registry)
    changed = await _poll_and_apply(db, row, status, registry, before)
    await db.commit()  # also ends the row lock
    if not changed:
        return
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


def _confirmation(row: CarrierVerification, registry: Registry) -> str | None:
    """How the person must confirm an SMS registration that waits on them:
    'code', 'reply', or None."""
    if row.purpose != "sms" or row.state not in _POLLED_STATES:
        return None
    provider = registry(row.provider, "sms")
    if not isinstance(provider, SmsRegistrationProvider):
        return None
    return provider.confirmation(row.provider_refs or {})


def _out(
    row: CarrierVerification,
    registry: Registry,
    model: type[VerificationResponse] = VerificationResponse,
) -> VerificationResponse:
    """The response for a row, with ``confirmation`` filled in."""
    return model.model_validate(row).model_copy(
        update={"confirmation": _confirmation(row, registry)}
    )


async def approved_purchase_handle(
    db: AsyncSession,
    registry: Registry,
    organization_id: UUID,
    provider_name: str,
    country_code: str,
    number_type: str,
) -> dict | None:
    """The carrier's purchase values for this org's approved verification, or
    None when there is none.

    Approved rows only, and no carrier call: the purchase route holds an
    advisory lock and must not commit or wait on the carrier here. A submitted
    verification becomes approved when the customer reads it (GET refreshes)."""
    row = (
        await db.execute(
            select(CarrierVerification).where(
                CarrierVerification.organization_id == organization_id,
                CarrierVerification.provider == provider_name,
                CarrierVerification.country_code == country_code,
                CarrierVerification.number_type == number_type,
                CarrierVerification.purpose == "number",
                CarrierVerification.state == "approved",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    provider = registry(provider_name, "number")
    if provider is None:
        return None
    return await provider.purchase_handle(row.provider_refs)


# -- customer routes ------------------------------------------------------


@router.get(
    "/requirements",
    response_model=Requirements,
    operation_id="get_verification_requirements",
)
async def get_requirements(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
    country_code: Annotated[str, Query(min_length=2, max_length=2)],
    number_type: str,
    default_provider: Annotated[str | None, Depends(get_default_provider_name)],
    subject_type: SubjectType = "person",
    provider: str | None = None,
    purpose: str = "number",
    quote_id: Annotated[
        UUID | None,
        Query(
            description="quote_id of an offer from POST /numbers/quotes that needs "
            "verification. The carrier is read from it."
        ),
    ] = None,
    number_id: Annotated[
        UUID | None,
        Query(
            description="For purpose=sms: id of one of your US numbers. The "
            "carrier is read from it. Needed when your US numbers are with "
            "more than one carrier."
        ),
    ] = None,
) -> Requirements:
    """What the carrier needs from you: the fields, the documents, and whether
    an address is needed. `purpose=number` (default) is for buying a number;
    `purpose=sms` is for registering to send SMS in the US; without
    `provider` it uses the carrier of your US numbers, or of the number
    passed as `number_id`. For `purpose=number`,
    pass the offer's `quote_id` to get that offer's carrier. Build your form
    from it."""
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
            db, principal.organization_id, provider, number_id, "query"
        )
    elif quote_id is not None:
        name = await _quote_provider_name(
            db,
            principal.organization_id,
            quote_id,
            provider,
            country_code,
            number_type,
            "query",
        )
    else:
        name = _resolve_provider_name(provider, default_provider)
    return await _requirements(
        _provider_or_404(registry, name, purpose),
        country_code,
        number_type,
        subject_type,
    )


class _DocumentPart(BaseModel):
    option: str = ""
    fields: dict[str, str] = Field(default_factory=dict)


class _Submission(BaseModel):
    provider: str | None = None
    quote_id: UUID | None = None
    number_id: UUID | None = None
    purpose: str = "number"
    country_code: str = Field(min_length=2, max_length=2)
    number_type: str
    subject_type: SubjectType = "person"
    fields: dict[str, str] = Field(default_factory=dict)
    address: Address | None = None
    documents: dict[str, _DocumentPart] = Field(default_factory=dict)


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


class _InMemoryMultiPartParser(MultiPartParser):
    """Starlette spools any file part over 1 MB to a temporary file on disk.
    The body is capped at MAX_REQUEST_BYTES (below), so with this limit no part
    can reach the spool size and every byte stays in memory."""

    spool_max_size = MAX_REQUEST_BYTES + 1


async def _capped_body(request: Request) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_REQUEST_BYTES:
            raise unprocessable("request is too large", loc=["body"])
        yield chunk


async def _parse_form(request: Request):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_REQUEST_BYTES:
        raise unprocessable("request is too large", loc=["body"])
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise unprocessable("send multipart/form-data", loc=["body"])
    try:
        return await _InMemoryMultiPartParser(
            request.headers, _capped_body(request), max_files=20, max_fields=20
        ).parse()
    except MultiPartException as exc:
        raise unprocessable(exc.message, loc=["body"]) from exc


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


@router.post(
    "",
    response_model=VerificationResponse,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="create_verification",
)
async def create_verification(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
    default_provider: Annotated[str | None, Depends(get_default_provider_name)],
) -> VerificationResponse:
    """Submit your details and documents (multipart/form-data).

    Parts: `purpose` (`number`, the default, or `sms` to register to send SMS
    in the US; `sms` takes `country_code=US` and `number_type=local` and charges
    the filing fee), `country_code`, `number_type`, `subject_type`, `provider`
    (optional; for `sms` it defaults to the carrier of your US numbers),
    `quote_id` (optional, for `number`: the offer to verify for; its carrier
    is used), `number_id` (optional, for `sms`: one of your US numbers; its
    carrier is used),
    `fields` (JSON object), `address` (JSON object, when required), `documents`
    (JSON object: slot name to `{"option": ..., "fields": {...}}`), and one file
    part per slot named `file.<slot>`. Get the slot and field names from
    `GET /verifications/requirements`.

    The carrier checks the details right away. Problems come back as 422 with
    the field named, and nothing is kept. When it passes, the verification is
    submitted to the carrier for its review; read it back to follow the state.
    """
    form = await _parse_form(request)

    try:
        sub = _Submission(
            provider=_text_part(form, "provider") or None,
            quote_id=_text_part(form, "quote_id") or None,
            number_id=_text_part(form, "number_id") or None,
            purpose=_text_part(form, "purpose", "number"),
            country_code=_text_part(form, "country_code").upper(),
            number_type=_text_part(form, "number_type"),
            subject_type=_text_part(form, "subject_type", "person"),  # type: ignore[arg-type]
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
        sub.country_code != "US" or sub.number_type != "local"
    ):
        raise unprocessable(
            "SMS registration is for US local numbers", loc=["body", "country_code"]
        )

    if sub.purpose == "sms" and sub.quote_id is not None:
        raise unprocessable(
            "quote_id is only for purpose=number", loc=["body", "quote_id"]
        )
    if sub.purpose != "sms" and sub.number_id is not None:
        raise unprocessable(
            "number_id is only for purpose=sms", loc=["body", "number_id"]
        )
    if sub.purpose == "sms":
        provider_name = await _sms_provider_name(
            db, principal.organization_id, sub.provider, sub.number_id, "body"
        )
    elif sub.quote_id is not None:
        provider_name = await _quote_provider_name(
            db,
            principal.organization_id,
            sub.quote_id,
            sub.provider,
            sub.country_code,
            sub.number_type,
            "body",
        )
    else:
        provider_name = _resolve_provider_name(sub.provider, default_provider)
    provider = _provider_or_404(registry, provider_name, sub.purpose)

    if sub.purpose == "sms":
        # Held until the row is saved: a second filing at the same time
        # waits here, then sees the first row and gets 409 before any
        # carrier call (two filings would register two brands).
        await org_lock(db, principal.organization_id)
    existing = (
        await db.execute(
            select(CarrierVerification).where(
                CarrierVerification.organization_id == principal.organization_id,
                CarrierVerification.provider == provider_name,
                CarrierVerification.purpose == sub.purpose,
                CarrierVerification.country_code == sub.country_code,
                CarrierVerification.number_type == sub.number_type,
                CarrierVerification.state.in_(_LIVE_STATES),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"a verification already exists ({existing.state}); id {existing.id}",
        )

    requirements = await _requirements(
        provider, sub.country_code, sub.number_type, sub.subject_type, where="body"
    )
    if not requirements.required:
        raise unprocessable(
            "this number does not need verification", loc=["body", "number_type"]
        )

    documents: dict[str, DocumentInput] = {}
    for slot, value in sub.documents.items():
        upload = form.get(f"file.{slot}")
        documents[slot] = DocumentInput(
            option=value.option,
            fields=value.fields,
            file=(
                await _read_file(upload)
                if upload is not None and hasattr(upload, "read")
                else None
            ),
        )

    fee_cents = 0
    draft_kwargs: dict = {}
    if sub.purpose == "sms":
        try:
            fee_cents = sms_registration_fees.filing_fee_cents(
                provider_name, sub.subject_type
            )
        except KeyError:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="no SMS registration for this carrier",
            )
        if principal.auth_kind != "shared":
            balance = await get_balance_cents(db, principal.organization_id)
            if balance < fee_cents:
                raise HTTPException(
                    status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                    detail=f"filing costs ${fee_cents / 100:.2f}; add credit first",
                )
        draft_kwargs["messaging_service_sid"] = (
            await sms_setup.org_messaging_service_sid(
                db, principal.organization_id, provider_name
            )
        )

    try:
        draft = await provider.create_draft(
            organization_id=str(principal.organization_id),
            contact_email=settings.hail_support_email,
            requirements=requirements,
            fields=sub.fields,
            address=sub.address,
            documents=documents,
            **draft_kwargs,
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
    if draft.problems:
        raise _problems_422(draft.problems)

    row = CarrierVerification(
        organization_id=principal.organization_id,
        provider=provider_name,
        purpose=sub.purpose,
        country_code=sub.country_code,
        number_type=sub.number_type,
        subject_type=sub.subject_type,
        state="awaiting_review",
        provider_refs=draft.refs,
        requirements_version=requirements.version,
    )
    db.add(row)
    try:
        # The shared self-hosted key is not billed (as in number_orders).
        if fee_cents and principal.auth_kind != "shared":
            await db.flush()  # row.id is a server default
            db.add(
                AccountCredit(
                    organization_id=principal.organization_id,
                    kind="debit",
                    channel="sms",
                    amount_cents=-fee_cents,
                    qty=1,
                    ref=f"sms_registration:{row.id}",
                    source="sms_registration_fee",
                )
            )
        await db.commit()
    except Exception as exc:
        # The carrier record exists but the row does not: remove it, or it
        # would be left behind with the customer's details.
        await db.rollback()
        await provider.discard(draft.refs)
        if isinstance(exc, IntegrityError):
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="a verification already exists for this number type",
            ) from exc
        raise
    await db.refresh(row)
    # No human gate: the draft goes to the carrier now. If the carrier is
    # down, the row stays 'awaiting_review' and the sweeper retries it.
    await _submit_row(
        db,
        row,
        provider,
        actor=actor_of(principal),
        action="verification.submit",
        extra={"fee_cents": fee_cents} if sub.purpose == "sms" else None,
    )
    return _out(row, registry)


async def _submit_row(
    db: AsyncSession,
    row: CarrierVerification,
    provider: VerificationProvider,
    *,
    actor: tuple[UUID | None, str],
    action: str,
    extra: dict | None = None,
) -> bool:
    """Send an 'awaiting_review' draft the carrier has already evaluated
    (create_draft or check). True when it is now 'submitted'; False when the
    carrier was unreachable (the row stays 'awaiting_review' for the next try).
    Never raises for carrier trouble: creation must still answer 201 and the
    sweeper must keep going.

    The row is saved as 'submitting' before the carrier call so a crash in
    between cannot submit twice; _refresh settles a stuck 'submitting' row.
    approved_by names the superadmin who approved, or nobody for the
    automatic submits."""
    actor_user_id, actor_kind = actor
    approved_by = actor_user_id if actor_kind == "superadmin" else None
    row.state, row.updated_at = "submitting", datetime.now(timezone.utc)
    row.approved_by = approved_by
    await db.commit()
    try:
        await provider.submit(row.provider_refs)
    except Exception:
        logger.exception("verification submit failed for %s", row.id)
        row.state, row.updated_at = "awaiting_review", datetime.now(timezone.utc)
        row.approved_by = None
        await db.commit()
        return False
    now = datetime.now(timezone.utc)
    row.state, row.submitted_at, row.updated_at = "submitted", now, now
    await db.commit()
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


async def _retry_submit(
    db: AsyncSession, verification_id: UUID, registry: Registry
) -> bool:
    """One sweeper retry of an unsent draft. The carrier evaluates it first:
    a draft it refuses becomes 'rejected' with the carrier's reason, so the
    customer sees why and can start over; it is not retried again."""
    row = await _lock_waiting(db, verification_id)
    if row is None:
        return False
    provider = registry(row.provider, row.purpose)
    if provider is None:
        await db.rollback()
        return False
    try:
        problems = await provider.check(row.provider_refs)
    except Exception:
        logger.exception("verification check failed for %s", row.id)
        await db.rollback()
        return False
    if problems:
        await _reject_row(db, row, provider, "; ".join(p.message for p in problems))
        return False
    return await _submit_row(
        db, row, provider, actor=(None, "system"), action="verification.submit"
    )


async def _reject_row(
    db: AsyncSession,
    row: CarrierVerification,
    provider: VerificationProvider,
    reason: str,
) -> None:
    """The carrier refused the draft before submission: record why, discard
    the draft so nothing of the customer's is left at the carrier, and audit
    it as a system action (no person rejected it). If the discard fails the
    row is left as it was and tried again next tick; the sweeper never dies
    on carrier trouble."""
    try:
        await provider.discard(row.provider_refs)
    except Exception:
        logger.exception("verification discard failed for %s", row.id)
        await db.rollback()
        return
    row.state, row.rejection_reason, row.provider_refs = "rejected", reason, {}
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await write_audit_log(
        row.organization_id,
        None,
        "verification.reject",
        "carrier_verification",
        row.id,
        {"rejected_by": None, "reason": reason},
        actor_user_id=None,
        actor_kind="system",
    )


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
    return {"submitted": submitted, "refreshed": refreshed}


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
    # SMS rows are locked and re-read first (see _lock_for_poll).
    due = [r for r in asked if await _lock_for_poll(db, r) and _pollable(r)]
    before = [copy.deepcopy(r.provider_refs) for r in due]
    statuses = await asyncio.gather(*(_fetch_status(r, registry) for r in due))
    for r, s, b in zip(due, statuses, before):
        await _poll_and_apply(db, r, s, registry, b)
    if asked:
        await db.commit()  # saves changes and ends the row locks
    return [_out(r, registry) for r in rows]


class SmsFee(BaseModel):
    filing_cents: int = Field(
        description="Charged once when the registration is filed."
    )
    monthly_cents: int = Field(description="Charged each month while approved.")
    minimum_months: int = Field(
        description=(
            "Monthly fees charged at least, counted from approval, even when "
            "the registration is cancelled sooner."
        )
    )


@router.get(
    "/sms-fees",
    response_model=dict[str, dict[str, SmsFee]],
    operation_id="get_sms_registration_fees",
)
async def get_sms_fees(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, dict[str, SmsFee]]:
    """Fees for US SMS registration, per carrier and subject type, in cents.
    Passed through at cost."""
    out: dict[str, dict[str, SmsFee]] = {}
    for provider in sms_registration_fees.providers():
        out[provider] = {
            subject: SmsFee(
                filing_cents=sms_registration_fees.filing_fee_cents(provider, subject),
                monthly_cents=sms_registration_fees.monthly_fee_cents(
                    provider, subject
                ),
                minimum_months=sms_registration_fees.minimum_months(provider, subject),
            )
            for subject in ("person", "business")
        }
    return out


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
    return _out(row, registry)


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
    a rejected one. The draft is discarded either way. One that is under
    review cannot be cancelled: wait for the result; if it is rejected,
    dismiss it.

    An approved US SMS registration (`purpose=sms`) can be cancelled too:
    its numbers leave the campaign, the campaign is deleted at the carrier,
    and US-to-US texts from those numbers get 422 again. Its monthly fee is
    charged through the month of the cancellation, then stops. Telnyx bills
    at least 3 monthly fees from approval (`minimum_months` in
    GET /verifications/sms-fees), so a Telnyx registration cancelled sooner
    is still charged for 3 months."""
    row = await _org_row_or_404(db, principal, verification_id)
    if row.state in ("submitting", "submitted"):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="this verification is under review; wait for the result",
        )
    approved_sms = row.state == "approved" and row.purpose == "sms"
    if row.state not in ("awaiting_review", "rejected") and not approved_sms:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"cannot cancel a verification that is {row.state}",
        )
    provider = registry(row.provider, row.purpose)
    if approved_sms:
        # Serialize with purchases, routing and releases: they attach and
        # detach numbers too.
        await org_lock(db, row.organization_id)
        await _detach_registration(db, row, provider)
    if provider is not None:
        await provider.discard(row.provider_refs)
    now = datetime.now(timezone.utc)
    row.state, row.provider_refs = "cancelled", {}
    row.updated_at = row.cancelled_at = now
    await db.commit()
    if approved_sms:
        actor_user_id, actor_kind = actor_of(principal)
        await write_audit_log(
            row.organization_id,
            principal.api_key_id,
            "verification.cancel",
            "carrier_verification",
            row.id,
            {"purpose": row.purpose, "state_before": "approved"},
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
    On success the campaign is filed at once; poll GET /verifications/{id}
    until `approved`. A wrong or expired code is 422 on `code`; after five
    wrong codes, 429 until the carrier sends a new code (once a day). 409
    when there is nothing to confirm."""
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
        raise _problems_422(problems)
    # File the campaign now rather than on the next poll.
    await _refresh(db, row, registry)
    return _out(row, registry)


def _nothing_to_confirm() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT, detail="nothing to confirm"
    )


# -- superadmin routes ----------------------------------------------------


class AdminVerification(VerificationResponse):
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
    return [_out(r, registry, AdminVerification) for r in rows]


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
    """Submit the draft to the carrier for its review."""
    row = await _admin_row(db, verification_id)
    provider = _provider_or_404(registry, row.provider, row.purpose)
    try:
        problems = await provider.check(row.provider_refs)
    except Exception as exc:
        logger.exception("verification check failed")
        raise _carrier_unavailable() from exc
    if problems:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="the carrier no longer accepts this draft: "
            + "; ".join(p.message for p in problems),
        )
    if not await _submit_row(
        db, row, provider, actor=actor_of(admin), action="verification.approve"
    ):
        raise _carrier_unavailable()
    return _out(row, registry, AdminVerification)


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
    if provider is not None:
        await provider.discard(row.provider_refs)
    row.state, row.rejection_reason, row.provider_refs = "rejected", body.reason, {}
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
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
