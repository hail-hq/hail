"""Consent evidence for US SMS registration.

POST /verifications/sms/evidence - upload a screenshot, get back a public link.
GET  /verifications/sms/evidence/{token} - public: carrier reviewers open it.

Carriers take consent evidence only as a public link inside the campaign
text; they have no upload field. So Hail keeps the file and gives the link.
The random token in the link is the only credential. Bytes live in the
shared mail bucket under sms-evidence/{token}; no table tracks them.
"""

from __future__ import annotations

import re
import secrets
from typing import Annotated

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi import status as http_status
from fastapi.responses import Response
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.core.config import settings
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.urls import join_url
from pydantic import BaseModel, Field

router = APIRouter(
    prefix="/verifications/sms/evidence",
    tags=["verifications"],
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)

MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
_READ_CHUNK = 1024 * 1024
_KEY_PREFIX = "sms-evidence/"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")
_MISSING = ("NoSuchKey", "404", "AccessDenied", "403")
# The file's first bytes decide its type, never the name or the header the
# client sent. Only these types are kept or served: no HTML or SVG on the
# API's origin.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"%PDF-", "application/pdf"),
)


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


@router.post(
    "",
    response_model=EvidenceUploadResponse,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="upload_sms_evidence",
)
async def upload_sms_evidence(
    principal: Annotated[Principal, Depends(get_current_principal)],
    store: Annotated[S3MailClient | None, Depends(get_evidence_store)],
    file: Annotated[
        UploadFile,
        File(description="A PNG, JPG or PDF of 10 MB or less."),
    ],
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
    buf = bytearray()
    while chunk := await file.read(_READ_CHUNK):
        buf += chunk
        if len(buf) > MAX_EVIDENCE_BYTES:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="The file must be 10 MB or smaller.",
            )
    data = bytes(buf)
    content_type = _sniff(data)
    if content_type is None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Upload a PNG, JPG or PDF.",
        )
    token = secrets.token_urlsafe(24)
    await store.put_attachment(
        _KEY_PREFIX + token,
        data,
        content_type,
        metadata={"organization-id": str(principal.organization_id)},
    )
    return EvidenceUploadResponse(
        url=join_url(settings.hail_api_url, f"v1/verifications/sms/evidence/{token}"),
        content_type=content_type,
        size_bytes=len(data),
    )


@router.get(
    "/{token}",
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
    token: str,
    store: Annotated[S3MailClient | None, Depends(get_evidence_store)],
) -> Response:
    """Public: the file behind a link from `POST /verifications/sms/evidence`.

    No sign-in. Carrier reviewers open this link.
    """
    if store is None or not _TOKEN.fullmatch(token):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        data = await store.fetch_raw(_KEY_PREFIX + token)
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
            "Cache-Control": "public, max-age=86400, immutable",
            "X-Robots-Tag": "noindex",
        },
    )


__all__ = ["get_evidence_store", "router"]
