"""Routes for outbound email attachment uploads.

POST /email-attachments - upload a file, get back a reusable id.

Uploads are org-scoped and reusable across many POST /emails calls (see
EmailCreate.attachment_ids) until garbage-collected — see
hailhq.core.email_attachment_gc. Bytes live in the shared mail S3 bucket
under outbound-attachments/{organization_id}/{id}; the row purely tracks
metadata + first-use so the GC worker knows what's safe to delete.
"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi import status as http_status
from hailhq.api.deps import Principal, get_current_principal, get_s3_mail
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.core.db import get_session
from hailhq.core.email_attachment_limits import (
    ATTACHMENT_TOO_LARGE_DETAIL,
    MAX_EMAIL_ATTACHMENT_BYTES,
)
from hailhq.core.models import EmailAttachmentUpload
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.schemas import EmailAttachmentUploadResponse
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/email-attachments", tags=["email-attachments"])

_READ_CHUNK_BYTES = 1024 * 1024  # 1MB


@router.post(
    "",
    response_model=EmailAttachmentUploadResponse,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="upload_email_attachment",
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def create_email_attachment(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3MailClient, Depends(get_s3_mail)],
    file: Annotated[
        UploadFile,
        File(
            description=(
                "The file to upload, as multipart/form-data. Size-limited; "
                "an oversize upload is rejected with 422."
            )
        ),
    ],
) -> EmailAttachmentUploadResponse:
    """Upload a file and get back a reusable attachment id.

    The returned id can be referenced from attachment_ids on many later
    POST /v1/emails calls until it is garbage-collected for being unused; it is
    not deleted immediately after first use. Uploads are size-limited and
    scoped to the caller's organization.
    """
    # Read in bounded chunks so an oversize body is rejected without ever
    # buffering more than ~MAX_EMAIL_ATTACHMENT_BYTES in memory — no layer
    # in front of this endpoint (ASGI server, reverse proxy) caps request
    # body size, so a single unbounded `await file.read()` would let an
    # arbitrarily large upload fully materialize before the check ran.
    buf = bytearray()
    while True:
        chunk = await file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_EMAIL_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=ATTACHMENT_TOO_LARGE_DETAIL,
            )
    payload = bytes(buf)

    content_type = file.content_type or "application/octet-stream"
    upload_id = uuid4()
    key = f"outbound-attachments/{principal.organization_id}/{upload_id}"
    await s3.put_attachment(key, payload, content_type)

    row = EmailAttachmentUpload(
        id=upload_id,
        organization_id=principal.organization_id,
        filename=file.filename or "attachment",
        content_type=content_type,
        size_bytes=len(payload),
        s3_key=key,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    return EmailAttachmentUploadResponse.model_validate(row)


__all__ = ["router"]
