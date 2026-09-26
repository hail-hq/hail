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
from hailhq.api.superadmin import require_superadmin
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.models import CarrierVerification
from hailhq.core.providers.verification import (
    Address,
    DocumentInput,
    Problem,
    ProviderStatus,
    Requirements,
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
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
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
# A row left in 'submitting' this long was cut off mid-submit (see
# _submit_row). Reads then ask the carrier what really happened.
_SUBMITTING_STUCK_AFTER = timedelta(minutes=5)
# When this process last asked the carrier about a submitted verification.
_last_polled: dict[UUID, float] = {}

Registry = Callable[[str], VerificationProvider | None]


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


def _provider_or_404(registry: Registry, name: str) -> VerificationProvider:
    provider = registry(name)
    if provider is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"no verification provider named {name!r}",
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
    provider = registry(row.provider)
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
    if not _apply_status(row, await _fetch_status(row, registry)):
        return
    await db.commit()
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
                CarrierVerification.state == "approved",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    provider = registry(provider_name)
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
    registry: Annotated[Registry, Depends(get_verification_registry)],
    country_code: Annotated[str, Query(min_length=2, max_length=2)],
    number_type: str,
    default_provider: Annotated[str | None, Depends(get_default_provider_name)],
    subject_type: SubjectType = "person",
    provider: str | None = None,
) -> Requirements:
    """What the carrier needs from you to buy this kind of number: the fields,
    the documents, and whether an address is needed. Build your form from it."""
    return await _requirements(
        _provider_or_404(registry, _resolve_provider_name(provider, default_provider)),
        country_code,
        number_type,
        subject_type,
    )


class _DocumentPart(BaseModel):
    option: str = ""
    fields: dict[str, str] = Field(default_factory=dict)


class _Submission(BaseModel):
    provider: str | None = None
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

    Parts: `country_code`, `number_type`, `subject_type`, `provider` (optional),
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

    provider_name = _resolve_provider_name(sub.provider, default_provider)
    provider = _provider_or_404(registry, provider_name)

    existing = (
        await db.execute(
            select(CarrierVerification).where(
                CarrierVerification.organization_id == principal.organization_id,
                CarrierVerification.provider == provider_name,
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

    try:
        draft = await provider.create_draft(
            organization_id=str(principal.organization_id),
            contact_email=settings.hail_support_email,
            requirements=requirements,
            fields=sub.fields,
            address=sub.address,
            documents=documents,
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
        country_code=sub.country_code,
        number_type=sub.number_type,
        subject_type=sub.subject_type,
        state="awaiting_review",
        provider_refs=draft.refs,
        requirements_version=requirements.version,
    )
    db.add(row)
    try:
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
        db, row, provider, actor=actor_of(principal), action="verification.submit"
    )
    return VerificationResponse.model_validate(row)


async def _submit_row(
    db: AsyncSession,
    row: CarrierVerification,
    provider: VerificationProvider,
    *,
    actor: tuple[UUID | None, str],
    action: str,
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
        {"approved_by": str(approved_by) if approved_by else None},
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
    provider = registry(row.provider)
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
) -> list[VerificationResponse]:
    """Your organization's verifications, newest first."""
    rows = (
        (
            await db.execute(
                select(CarrierVerification)
                .where(CarrierVerification.organization_id == principal.organization_id)
                .order_by(CarrierVerification.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    # Ask the carrier about rows not asked in the last minute, all at once.
    # Only the carrier calls run concurrently; the session is used in order.
    now = time.monotonic()
    due = [r for r in rows if _poll_due(r, now)]
    statuses = await asyncio.gather(*(_fetch_status(r, registry) for r in due))
    changed = [_apply_status(r, s) for r, s in zip(due, statuses)]
    if any(changed):
        await db.commit()
    return [VerificationResponse.model_validate(r) for r in rows]


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
    return VerificationResponse.model_validate(row)


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
    dismiss it."""
    row = await _org_row_or_404(db, principal, verification_id)
    if row.state in ("submitting", "submitted"):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="this verification is under review; wait for the result",
        )
    if row.state not in ("awaiting_review", "rejected"):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"cannot cancel a verification that is {row.state}",
        )
    provider = registry(row.provider)
    if provider is not None:
        await provider.discard(row.provider_refs)
    row.state, row.provider_refs = "cancelled", {}
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return VerificationResponse.model_validate(row)


# -- superadmin routes ----------------------------------------------------


class AdminVerification(VerificationResponse):
    organization_id: UUID


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@admin_router.get("", response_model=list[AdminVerification])
async def admin_list(
    db: Annotated[AsyncSession, Depends(get_session)],
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
    return [AdminVerification.model_validate(r) for r in rows]


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
    provider = _provider_or_404(registry, row.provider)
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
    return AdminVerification.model_validate(row)


@admin_router.post("/{verification_id}/reject", response_model=AdminVerification)
async def admin_reject(
    verification_id: UUID,
    body: RejectRequest,
    admin: Annotated[Principal, Depends(require_superadmin)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
) -> AdminVerification:
    row = await _admin_row(db, verification_id)
    provider = registry(row.provider)
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
    return AdminVerification.model_validate(row)
