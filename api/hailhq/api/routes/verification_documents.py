"""``POST /verifications/document-check``: read the documents of a number
verification form before it is filed. Returns the values the files show,
by the carrier's field names, and warnings about what the carrier may
refuse (an expired or upside-down document, a document of another type, a
typed name that differs from the printed one).

Warnings only, and fail open: a file the model cannot read, a slow read or
no model at all gives no warning, never an error. Nothing is filed, saved,
held or charged, and uploads are not used up.

The files go to the model provider (Anthropic, or Amazon Bedrock when
``ANTHROPIC_BEDROCK_REGION`` is set) for this read and nowhere else. Nothing
here logs a value, a name, a file or the model's answer: only counts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from hailhq.api.deps import Principal, get_current_principal, get_s3_mail_optional
from hailhq.api.errors import unprocessable
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.api.routes.verifications import (
    _MAX_PARALLEL_FETCHES,
    MAX_REQUEST_BYTES,
    Registry,
    _DocumentPart,
    _files_too_large,
    _json_part,
    _parse_form,
    _provider_or_404,
    _quote_offer,
    _quote_required,
    _read_file,
    _read_upload,
    _requirements,
    _text_part,
    _usable_upload,
    get_verification_registry,
)
from hailhq.core.db import get_session
from hailhq.core.models import FileUpload
from hailhq.core.providers.verification import DocumentInput, UploadedFile
from hailhq.core.providers.verification.document_check import check_documents
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.schemas import VerificationDocumentCheckResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/verifications",
    tags=["verifications"],
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)


class _DocumentCheckForm(BaseModel):
    quote_id: UUID | None = None
    country_code: str = Field(min_length=2, max_length=2)
    number_type: str
    subject_type: str
    fields: dict[str, str] = Field(default_factory=dict)
    address: dict[str, str] = Field(default_factory=dict)
    documents: dict[str, _DocumentPart] = Field(default_factory=dict)


@router.post(
    "/document-check",
    response_model=VerificationDocumentCheckResponse,
    operation_id="check_verification_documents",
)
async def check_verification_documents(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
    s3: Annotated[S3MailClient | None, Depends(get_s3_mail_optional)],
) -> VerificationDocumentCheckResponse:
    """Read the documents of a number verification form before you submit it
    (multipart/form-data).

    Send the documents first: the same parts as `POST /verifications`, with
    a file per slot (`file.<slot>`, or `upload.<slot>` with the id of a
    `verification_document` upload). `quote_id`, `country_code`,
    `number_type` and `subject_type` are required. `documents` may leave
    out `option` while the type is not chosen; `fields` and `address` may
    be empty or partial.

    Returns the values the files show, by the carrier's field names, to
    fill the form (`fields`, `address`, `documents.<slot>.fields`, and the
    document type each file looks like in `documents.<slot>.option`). Also
    returns `warnings`: an unreadable, upside-down or expired document, a
    file that does not match the chosen document type, or a typed name that
    differs from the printed one. Show each warning with its fix. You can
    still submit.

    A file that could not be read has `read` false and gives no warning.
    Nothing is filed, saved, held or charged, and uploads are not used up.
    The files are read by an AI model (Anthropic) for this check only.
    """
    form = await _parse_form(request)
    try:
        sub = _DocumentCheckForm(
            quote_id=_text_part(form, "quote_id") or None,
            country_code=_text_part(form, "country_code").upper(),
            number_type=_text_part(form, "number_type"),
            subject_type=_text_part(form, "subject_type"),
            fields=_json_part(form, "fields", {}),
            address=_json_part(form, "address", {}) or {},
            documents=_json_part(form, "documents", {}),
        )
    except ValidationError as exc:
        raise unprocessable(
            "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()
            ),
        ) from exc
    if sub.subject_type not in ("person", "business"):
        raise unprocessable(
            "subject_type is required: 'person' (you) or 'business' (your company)",
            loc=["body", "subject_type"],
        )
    if sub.quote_id is None:
        raise _quote_required("body")
    offer = await _quote_offer(
        db,
        principal.organization_id,
        sub.quote_id,
        sub.country_code,
        sub.number_type,
        "body",
    )
    provider = _provider_or_404(registry, offer["provider"], "number")
    for key in form:
        prefix, _, slot = key.partition(".")
        if prefix in ("file", "upload") and slot not in sub.documents:
            raise unprocessable(
                "this file names a slot that is not in documents", loc=["body", key]
            )

    # The upload rows and the file parts, then the total size from both.
    rows: dict[str, FileUpload] = {}
    files: dict[str, UploadedFile] = {}
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
            total += rows[slot].size_bytes or 0
        elif has_part:
            files[slot] = await _read_file(part)
            total += len(files[slot].data)
        if total > MAX_REQUEST_BYTES:
            raise _files_too_large()
    if rows and s3 is None:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="file uploads are not configured on this server",
        )
    # Nothing is written. End the read transaction before the slow calls
    # (the carrier's form, the bucket, the model).
    await db.rollback()

    requirements = await _requirements(
        provider,
        sub.country_code,
        sub.number_type,
        sub.subject_type,  # type: ignore[arg-type]
        where="body",
    )
    slots = {s.name for s in requirements.documents}
    for slot in sub.documents:
        if slot not in slots:
            raise unprocessable(
                "this slot is not in the form", loc=["body", "documents", slot]
            )

    # Each distinct upload once, a few at a time, as submit fetches them.
    # Every fetch finishes, then the first failure in slot order is raised:
    # the same request always gets the same error.
    fetches = asyncio.Semaphore(_MAX_PARALLEL_FETCHES)

    async def fetch(row: FileUpload) -> UploadedFile:
        async with fetches:
            return await _read_upload(s3, row)

    distinct = {row.id: row for row in rows.values()}
    results = await asyncio.gather(
        *(fetch(row) for row in distinct.values()), return_exceptions=True
    )
    fetched = dict(zip(distinct, results))
    for slot, row in rows.items():
        result = fetched[row.id]
        if isinstance(result, BaseException):
            raise result
        files[slot] = result
    # Again with the bytes read: an object may differ from its row.
    if sum(len(f.data) for f in files.values()) > MAX_REQUEST_BYTES:
        raise _files_too_large()

    documents = {
        slot: DocumentInput(
            option=part.option, fields=part.fields, file=files.get(slot)
        )
        for slot, part in sub.documents.items()
    }
    return await check_documents(
        requirements,
        sub.fields,
        sub.address,
        documents,
        today=datetime.now(timezone.utc).date(),
    )
