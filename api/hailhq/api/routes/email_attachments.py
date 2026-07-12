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
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.deps import Principal, get_current_principal
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.email_attachment_limits import (
    ATTACHMENT_TOO_LARGE_DETAIL,
    MAX_EMAIL_ATTACHMENT_BYTES,
)
from hailhq.core.models import EmailAttachmentUpload
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.schemas import EmailAttachmentUploadResponse

router = APIRouter(prefix="/email-attachments", tags=["email-attachments"])


def _get_s3_mail() -> S3MailClient:
    return S3MailClient(bucket=settings.hail_mail_bucket)


@router.post(
    "",
    response_model=EmailAttachmentUploadResponse,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="upload_email_attachment",
)
async def create_email_attachment(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3MailClient, Depends(_get_s3_mail)],
    file: Annotated[UploadFile, File()],
) -> EmailAttachmentUploadResponse:
    payload = await file.read()
    if len(payload) > MAX_EMAIL_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=ATTACHMENT_TOO_LARGE_DETAIL,
        )

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
