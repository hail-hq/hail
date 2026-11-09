"""One-time upload links: send a file from a shell that has no Hail key.

POST /uploads (signed in) makes a link. PUT /uploads/{id}?token=... (no
Authorization header) sends the raw bytes to it, once, within 15 minutes.
The token is the only credential for the PUT; only its sha256 is stored,
and it is never logged (``url.full`` loses its query in telemetry, see
``hailhq.core.telemetry``; the access log redacts it, see main.py).

What the file is for decides where it goes and how big it may be: an
``email_attachment`` is stored like ``POST /email-attachments`` and its id
works in ``POST /emails`` ``attachment_ids``; a ``verification_document``
must be a JPEG, PNG or PDF of at most 10 MB and its id works as the
``upload.<slot>`` part of ``POST /verifications`` and its ``/check``.

A body over the limit is 413 (a raw PUT, so the HTTP status for a body too
large; ``POST /email-attachments`` keeps its 422).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from hailhq.api.deps import Principal, get_current_principal, get_s3_mail_optional
from hailhq.api.errors import unprocessable
from hailhq.api.ratelimit import (
    GENERAL_RATE_LIMITED_RESPONSES,
    exempt_prefix,
    merge_rate_limited_responses,
)
from hailhq.core.config import settings
from hailhq.core.db import get_session, org_lock
from hailhq.core.file_uploads import (
    LINK_TTL,
    MAX_PENDING_LINKS,
    hash_token,
    matches_declared_type,
    max_bytes,
    new_token,
    object_key,
)
from hailhq.core.models import FileUpload
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.schemas import (
    FileUploadCreate,
    FileUploadLinkResponse,
    FileUploadResponse,
)
from hailhq.core.urls import join_url
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/uploads",
    tags=["uploads"],
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)

# The link is its own credential, like an unsubscribe link. Every caller
# with no bearer shares the proxy's IP, so a per-IP limit would let one
# scanner block every customer's upload. The token is checked before the
# body is read, and making links is limited per caller.
exempt_prefix("/uploads/", methods=frozenset({"PUT"}))

_NOT_FOUND = "upload link not found, or already used"
_UNAVAILABLE = "file uploads are not configured on this server"

_PENDING_CAP_RESPONSES = merge_rate_limited_responses(
    GENERAL_RATE_LIMITED_RESPONSES,
    {
        429: {
            "description": (
                f"Or: the organization already holds {MAX_PENDING_LINKS} "
                "unused, unexpired upload links. Use or let some expire."
            )
        }
    },
)


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=_UNAVAILABLE
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)


def _expired() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_410_GONE,
        detail="upload link expired; make a new one with POST /v1/uploads",
    )


def _too_large(limit: int) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_413_CONTENT_TOO_LARGE,
        detail=f"file is larger than {limit} bytes",
    )


@router.post(
    "",
    response_model=FileUploadLinkResponse,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="create_file_upload",
    responses={
        **_PENDING_CAP_RESPONSES,
        503: {"description": "File uploads are not configured on this server."},
    },
)
async def create_file_upload(
    body: FileUploadCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3MailClient | None, Depends(get_s3_mail_optional)],
) -> FileUploadLinkResponse:
    """Make a one-time link to upload one file with a plain HTTP PUT.

    For callers whose shell has no Hail key (an agent using the MCP). Send
    the raw bytes to `upload_url` with `PUT` and no Authorization header,
    once, before `expires_at` (15 minutes):

    ```
    curl -sS -X PUT --data-binary @passport.jpg '<upload_url>'
    ```

    The PUT answers with `size_bytes` and `sha256` of what was stored. An
    organization may hold at most 50 unused, unexpired links.
    """
    if s3 is None:
        raise _unavailable()
    org = principal.organization_id
    now = datetime.now(timezone.utc)
    # Two links made at once must not both pass the cap.
    await org_lock(db, org)
    pending = (
        await db.execute(
            select(func.count())
            .select_from(FileUpload)
            .where(
                FileUpload.organization_id == org,
                FileUpload.uploaded_at.is_(None),
                FileUpload.link_expires_at > now,
            )
        )
    ).scalar_one()
    if pending >= MAX_PENDING_LINKS:
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"too many unused upload links ({MAX_PENDING_LINKS}); use them "
                "or wait for them to expire"
            ),
        )
    token = new_token()
    upload_id = uuid4()
    row = FileUpload(
        id=upload_id,
        organization_id=org,
        purpose=body.purpose,
        filename=body.filename,
        content_type=body.content_type,
        s3_key=object_key(body.purpose, org, upload_id),
        token_hash=hash_token(token),
        link_expires_at=now + LINK_TTL,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    upload_url = (
        join_url(settings.hail_api_url, f"v1/uploads/{row.id}")
        + "?"
        + urlencode({"token": token})
    )
    return FileUploadLinkResponse(
        id=row.id,
        purpose=row.purpose,
        upload_url=upload_url,
        method="PUT",
        content_type=row.content_type,
        max_bytes=max_bytes(row.purpose),
        expires_at=row.link_expires_at,
    )


async def _read_body(request: Request, limit: int) -> bytes:
    """The raw body, stopped as soon as it passes ``limit``."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise _too_large(limit)
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > limit:
            raise _too_large(limit)
    return bytes(buf)


@router.put(
    "/{upload_id}",
    response_model=FileUploadResponse,
    operation_id="put_file_upload",
    responses={
        404: {"description": "Unknown link, wrong token, or already used."},
        410: {"description": "The link expired."},
        413: {"description": "The file is larger than the link's `max_bytes`."},
        503: {"description": "File uploads are not configured on this server."},
    },
    openapi_extra={
        "security": [],
        "requestBody": {
            "required": True,
            "description": (
                "The raw file bytes. The `Content-Type` header is ignored: "
                "the type given when the link was made is stored."
            ),
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
    },
)
async def put_file_upload(
    upload_id: UUID,
    request: Request,
    token: Annotated[
        str, Query(description="The token from `upload_url`. The only credential.")
    ],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3MailClient | None, Depends(get_s3_mail_optional)],
) -> FileUploadResponse:
    """Send the file's bytes to an upload link. No Authorization header.

    Works once. Unknown id, wrong token or a used link are all 404 with one
    message; an expired link is 410. Empty body: 422. A verification
    document whose first bytes are not the declared JPEG, PNG or PDF: 422.
    """
    if s3 is None:
        raise _unavailable()
    token_hash = hash_token(token)
    row = await db.get(FileUpload, upload_id)
    if (
        row is None
        or row.token_hash is None
        or not hmac.compare_digest(row.token_hash, token_hash)
    ):
        raise _not_found()
    now = datetime.now(timezone.utc)
    if row.link_expires_at is None or row.link_expires_at <= now:
        raise _expired()
    purpose, content_type = row.purpose, row.content_type
    # Nothing is held open while the body arrives.
    await db.rollback()

    limit = max_bytes(purpose)
    payload = await _read_body(request, limit)
    if not payload:
        raise unprocessable("the file is empty", loc=["body"])
    if purpose == "verification_document" and not matches_declared_type(
        payload, content_type
    ):
        raise unprocessable(f"the file does not look like {content_type}", loc=["body"])

    # Claim the link before the S3 write: a second PUT at the same time
    # skips the locked row and gets 404, so the object always holds the
    # bytes of the one PUT that succeeds.
    row = (
        await db.execute(
            select(FileUpload)
            .where(
                FileUpload.id == upload_id,
                FileUpload.token_hash == token_hash,
                FileUpload.uploaded_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if row is None:
        await db.rollback()
        raise _not_found()
    now = datetime.now(timezone.utc)
    if row.link_expires_at is None or row.link_expires_at <= now:
        await db.rollback()
        raise _expired()
    try:
        await s3.put_attachment(row.s3_key, payload, row.content_type)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage unavailable; try again",
        ) from exc
    row.token_hash = None
    row.uploaded_at = now
    row.size_bytes = len(payload)
    row.sha256 = hashlib.sha256(payload).hexdigest()
    await db.commit()
    await db.refresh(row)
    return FileUploadResponse.model_validate(row)


__all__ = ["router"]
