"""Consent evidence for US SMS registration.

POST /verifications/sms/evidence - upload a screenshot, get back a public link.
GET  /verifications/sms/evidence/{organization_id}/{token} - public: carrier
reviewers open it.

Carriers take consent evidence only as a public link inside the campaign
text; they have no upload field. So Hail keeps the file and gives the link.
The random token in the link is the only credential. Bytes live in the
shared mail bucket under sms-evidence/{organization_id}/{token}; no table
tracks them. The retention sweep deletes an organization's folder 12 months
after it closes (hailhq.core.retention).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from fastapi.responses import Response
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.core.config import settings
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.sms_evidence import SMS_EVIDENCE_PREFIX
from hailhq.core.urls import join_url
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

router = APIRouter(
    prefix="/verifications/sms/evidence",
    tags=["verifications"],
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)

MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
# Room for the multipart boundaries and part headers around the file.
_MAX_BODY_BYTES = MAX_EVIDENCE_BYTES + 64 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")
_MISSING = ("NoSuchKey", "404", "AccessDenied", "403")
_TOO_BIG = "The file must be 10 MB or smaller."
# The file's first bytes decide its type, never the name or the header the
# client sent. Only these types are kept or served: no HTML or SVG on the
# API's origin.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"%PDF-", "application/pdf"),
)
_UPLOAD_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {
                            "type": "string",
                            "format": "binary",
                            "description": "A PNG, JPG or PDF of 10 MB or less.",
                        }
                    },
                }
            }
        },
    }
}


class EvidenceUploadResponse(BaseModel):
    url: str = Field(
        description="Public link to the file. Put it in an `*_evidence_url` field."
    )
    content_type: str = Field(
        description="`image/png`, `image/jpeg` or `application/pdf`, read from the file."
    )
    size_bytes: int = Field(description="File size in bytes.")


def get_evidence_store() -> S3MailClient | None:
    """The mail bucket, or None when this install has no bucket."""
    bucket = settings.hail_mail_bucket
    return S3MailClient(bucket=bucket) if bucket else None


def _sniff(data: bytes) -> str | None:
    return next((kind for sig, kind in _SIGNATURES if data.startswith(sig)), None)


def _too_big() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_TOO_BIG
    )


class _InMemoryParser(MultiPartParser):
    """The body is capped below, so no part reaches the disk spool."""

    spool_max_size = _MAX_BODY_BYTES + 1


async def _capped(request: Request) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise _too_big()
        yield chunk


async def _read_upload(request: Request) -> bytes:
    """The one file part, read with the body capped before parsing."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > _MAX_BODY_BYTES:
        raise _too_big()
    if (
        not request.headers.get("content-type", "")
        .lower()
        .startswith("multipart/form-data")
    ):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Send the file as multipart/form-data in a part named file.",
        )
    try:
        form = await _InMemoryParser(
            request.headers, _capped(request), max_files=1, max_fields=1
        ).parse()
    except MultiPartException as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.message,
        ) from exc
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Send the file in a part named file.",
        )
    data = await upload.read()
    if len(data) > MAX_EVIDENCE_BYTES:
        raise _too_big()
    return data


@router.post(
    "",
    response_model=EvidenceUploadResponse,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="upload_sms_evidence",
    openapi_extra=_UPLOAD_BODY,
)
async def upload_sms_evidence(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    store: Annotated[S3MailClient | None, Depends(get_evidence_store)],
) -> EvidenceUploadResponse:
    """Upload a consent screenshot and get back a public link.

    Put the returned `url` in an SMS registration field that asks for an
    evidence URL (for example `website_evidence_url`). Carrier reviewers open
    it without signing in, so anyone with the link can see the file. Remove
    personal information first. Pasting your own public link still works.
    """
    if store is None:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This server cannot store files. Paste a public link instead.",
        )
    data = await _read_upload(request)
    content_type = _sniff(data)
    if content_type is None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Upload a PNG, JPG or PDF.",
        )
    org = principal.organization_id
    token = secrets.token_urlsafe(24)
    await store.put_attachment(
        f"{SMS_EVIDENCE_PREFIX}{org}/{token}", data, content_type
    )
    return EvidenceUploadResponse(
        url=join_url(
            settings.hail_api_url, f"v1/verifications/sms/evidence/{org}/{token}"
        ),
        content_type=content_type,
        size_bytes=len(data),
    )


@router.get(
    "/{organization_id}/{token}",
    response_class=Response,
    operation_id="get_sms_evidence",
    responses={
        200: {
            "content": {
                "image/png": {},
                "image/jpeg": {},
                "application/pdf": {},
            },
            "description": "The uploaded file.",
        },
        404: {"description": "No file has this link."},
    },
)
async def get_sms_evidence(
    organization_id: UUID,
    token: str,
    store: Annotated[S3MailClient | None, Depends(get_evidence_store)],
) -> Response:
    """Public: the file behind a link from `POST /verifications/sms/evidence`.

    No sign-in. Carrier reviewers open this link.
    """
    if store is None or not _TOKEN.fullmatch(token):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        data = await store.fetch_raw(f"{SMS_EVIDENCE_PREFIX}{organization_id}/{token}")
    except ClientError as exc:
        # Without s3:ListBucket, S3 answers a missing key with AccessDenied.
        if exc.response.get("Error", {}).get("Code") in _MISSING:
            raise HTTPException(status_code=404, detail="Not found") from exc
        raise
    content_type = _sniff(data)
    if content_type is None:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "public, max-age=86400",
            "X-Robots-Tag": "noindex",
        },
    )


__all__ = ["get_evidence_store", "router"]
