"""Carrier verification: collect a customer's details and documents and build
the carrier-side record, then let a superadmin approve it.

Files and details pass through this process in memory to the carrier. Nothing
identifying is written to the database, disk or logs (see the design spec).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from hailhq.api.audit import write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.api.superadmin import require_superadmin
from hailhq.core.db import get_session
from hailhq.core.models import CarrierVerification
from hailhq.core.providers.verification import (
    Address,
    DocumentInput,
    Problem,
    Requirements,
    SubjectType,
    UnsupportedSubjectType,
    UploadedFile,
    VerificationProvider,
    VerificationProviderError,
    get_verification_provider,
)
from hailhq.core.schemas import VerificationResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 30 * 1024 * 1024
ALLOWED_FILE_TYPES = {"image/jpeg", "image/png", "application/pdf"}
NUMBER_TYPES = {"local", "mobile", "toll_free", "national"}
_READ_CHUNK = 1024 * 1024
_LIVE_STATES = ("draft", "awaiting_review", "submitted", "approved")

Registry = Callable[[str], VerificationProvider | None]


def get_verification_registry() -> Registry:
    """Dependency so tests can swap in a fake carrier."""
    return get_verification_provider


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
) -> Requirements:
    if number_type not in NUMBER_TYPES:
        raise unprocessable("unknown number_type", loc=["query", "number_type"])
    try:
        return await provider.requirements(
            country_code.upper(), number_type, subject_type
        )
    except UnsupportedSubjectType as exc:
        raise unprocessable(str(exc), loc=["query", "subject_type"]) from exc
    except Exception as exc:
        logger.exception("verification requirements lookup failed")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="carrier unavailable; try again later",
        ) from exc


async def _refresh(
    db: AsyncSession, row: CarrierVerification, registry: Registry
) -> None:
    """Pull a submitted verification's outcome from the carrier."""
    if row.state != "submitted":
        return
    provider = registry(row.provider)
    if provider is None:
        return
    try:
        status = await provider.status(row.provider_refs)
    except Exception:
        logger.warning("verification status poll failed", exc_info=True)
        return
    now = datetime.now(timezone.utc)
    if status.state == "approved":
        row.state, row.approved_at, row.updated_at = "approved", now, now
    elif status.state == "rejected":
        row.state, row.rejection_reason, row.updated_at = "rejected", status.reason, now
    else:
        return
    await db.commit()


async def approved_purchase_handle(
    db: AsyncSession,
    registry: Registry,
    organization_id: UUID,
    provider_name: str,
    country_code: str,
    number_type: str,
) -> dict | None:
    """The carrier's purchase values for this org's approved verification, or
    None when there is none (yet)."""
    row = (
        await db.execute(
            select(CarrierVerification).where(
                CarrierVerification.organization_id == organization_id,
                CarrierVerification.provider == provider_name,
                CarrierVerification.country_code == country_code,
                CarrierVerification.number_type == number_type,
                CarrierVerification.state.in_(("submitted", "approved")),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    await _refresh(db, row, registry)
    if row.state != "approved":
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
    subject_type: SubjectType = "person",
    provider: str = "twilio",
) -> Requirements:
    """What the carrier needs from you to buy this kind of number: the fields,
    the documents, and whether an address is needed. Build your form from it."""
    return await _requirements(
        _provider_or_404(registry, provider), country_code, number_type, subject_type
    )


class _Submission(BaseModel):
    provider: str = "twilio"
    country_code: str = Field(min_length=2, max_length=2)
    number_type: str
    subject_type: SubjectType = "person"
    fields: dict[str, str] = Field(default_factory=dict)
    address: Address | None = None
    documents: dict[str, dict] = Field(default_factory=dict)


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
) -> VerificationResponse:
    """Submit your details and documents (multipart/form-data).

    Parts: `country_code`, `number_type`, `subject_type`, `provider` (optional),
    `fields` (JSON object), `address` (JSON object, when required), `documents`
    (JSON object: slot name to `{"option": ..., "fields": {...}}`), and one file
    part per slot named `file.<slot>`. Get the slot and field names from
    `GET /verifications/requirements`.

    The carrier checks the details right away. Problems come back as 422 with
    the field named, and nothing is kept. When it passes, the verification waits
    for review and is then submitted to the carrier.
    """
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_REQUEST_BYTES:
        raise unprocessable("request is too large", loc=["body"])
    form = await request.form()

    try:
        sub = _Submission(
            provider=_text_part(form, "provider", "twilio"),
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

    provider = _provider_or_404(registry, sub.provider)
    requirements = await _requirements(
        provider, sub.country_code, sub.number_type, sub.subject_type
    )
    if not requirements.required:
        raise unprocessable(
            "this number does not need verification", loc=["body", "number_type"]
        )

    existing = (
        await db.execute(
            select(CarrierVerification).where(
                CarrierVerification.organization_id == principal.organization_id,
                CarrierVerification.provider == sub.provider,
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

    documents: dict[str, DocumentInput] = {}
    for slot, value in sub.documents.items():
        upload = form.get(f"file.{slot}")
        documents[slot] = DocumentInput(
            option=str(value.get("option", "")),
            fields={str(k): str(v) for k, v in (value.get("fields") or {}).items()},
            file=await _read_file(upload)
            if upload is not None and hasattr(upload, "read")
            else None,
        )

    try:
        draft = await provider.create_draft(
            organization_id=str(principal.organization_id),
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
        provider=sub.provider,
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
    except IntegrityError as exc:
        await db.rollback()
        await provider.discard(draft.refs)
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="a verification already exists for this number type",
        ) from exc
    await db.refresh(row)
    return VerificationResponse.model_validate(row)


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
    for row in rows:
        await _refresh(db, row, registry)
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
    """Cancel a verification that has not been submitted yet."""
    row = await _org_row_or_404(db, principal, verification_id)
    if row.state != "awaiting_review":
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
    row = (
        await db.execute(
            select(CarrierVerification).where(CarrierVerification.id == verification_id)
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
    problems = await provider.check(row.provider_refs)
    if problems:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="the carrier no longer accepts this draft: "
            + "; ".join(p.message for p in problems),
        )
    await provider.submit(row.provider_refs)
    now = datetime.now(timezone.utc)
    row.state, row.submitted_at, row.updated_at = "submitted", now, now
    row.approved_by = admin.user_id
    await db.commit()
    await write_audit_log(
        row.organization_id,
        None,
        "verification.approve",
        "carrier_verification",
        row.id,
        {"approved_by": str(admin.user_id) if admin.user_id else None},
    )
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
    await write_audit_log(
        row.organization_id,
        None,
        "verification.reject",
        "carrier_verification",
        row.id,
        {"rejected_by": str(admin.user_id) if admin.user_id else None},
    )
    return AdminVerification.model_validate(row)
