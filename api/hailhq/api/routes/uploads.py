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

import asyncio
import hashlib
import hmac
import logging
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
from hailhq.core.db import get_session, key_lock
from hailhq.core.file_uploads import (
    LINK_BUSY_PREFIX,
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
from sqlalchemy import func, select, update
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

logger = logging.getLogger(__name__)

_NOT_FOUND = "upload link not found, already used, or in use"
# Body reads one process runs at once. Each can buffer up to the purpose's
# size limit (25 MB for an email attachment), so this caps that memory.
MAX_CONCURRENT_BODY_READS = 8
_BODY_READS = asyncio.Semaphore(MAX_CONCURRENT_BODY_READS)
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


def _busy() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="too many uploads at once; try again in a few seconds",
        headers={"Retry-After": "5"},
    )


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
    # Two links made at once must not both pass the cap. Not org_lock: that
    # one is held through SMS carrier calls.
    await key_lock(db, f"upload-links:{org}")
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
        404: {
            "description": (
                "Unknown link, wrong token, already used, or another PUT is "
                "sending to it."
            )
        },
        410: {"description": "The link expired, also while its body was sent."},
        413: {"description": "The file is larger than the link's `max_bytes`."},
        503: {
            "description": (
                "File uploads are not configured on this server, storage "
                "failed, or too many uploads are running (`Retry-After`). "
                "The link is not used up."
            )
        },
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

    Works once. Unknown id, wrong token, a used link or one another PUT is
    sending to are all 404 with one message; an expired link is 410. Empty body: 422. A verification
    document whose first bytes are not the declared JPEG, PNG or PDF: 422.
    """
    if s3 is None:
        raise _unavailable()
    # Each body read can hold up to the purpose's limit in memory, so this
    # process runs only a few at a time. Checked before the link is claimed,
    # so a busy server never uses up a link.
    if _BODY_READS.locked():
        raise _busy()
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

    # Claim the link before reading the body: the token hash becomes a busy
    # marker in one atomic update, so only one body read runs per link. A
    # second PUT meanwhile gets 404. A failed read gives the link back, so
    # the same link can be tried again with a smaller or correct file.
    busy = LINK_BUSY_PREFIX + token_hash
    claimed = (
        await db.execute(
            update(FileUpload)
            .where(
                FileUpload.id == upload_id,
                FileUpload.token_hash == token_hash,
                FileUpload.uploaded_at.is_(None),
                FileUpload.link_expires_at > now,
            )
            .values(token_hash=busy)
            .returning(FileUpload.purpose, FileUpload.content_type, FileUpload.s3_key)
        )
    ).one_or_none()
    if claimed is None:
        await db.rollback()
        raise _not_found()
    # Committed, so nothing is held open while the body arrives.
    await db.commit()
    purpose, content_type, s3_key = claimed

    async def give_back() -> None:
        await db.rollback()
        await db.execute(
            update(FileUpload)
            .where(FileUpload.id == upload_id, FileUpload.token_hash == busy)
            .values(token_hash=token_hash)
        )
        await db.commit()

    try:
        async with _BODY_READS:
            payload = await _read_body(request, max_bytes(purpose))
        if not payload:
            raise unprocessable("the file is empty", loc=["body"])
        if purpose == "verification_document" and not matches_declared_type(
            payload, content_type
        ):
            raise unprocessable(
                f"the file does not look like {content_type}", loc=["body"]
            )
        try:
            await s3.put_attachment(s3_key, payload, content_type)
        except Exception as exc:
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="storage unavailable; try again",
            ) from exc
    except BaseException:
        # Includes a client that hangs up mid-body. The give-back is shielded
        # so a cancelled request still returns the link.
        try:
            await asyncio.shield(give_back())
        except Exception:
            logger.warning("upload link %s was not given back", upload_id)
        raise

    row = (
        await db.execute(
            select(FileUpload).where(
                FileUpload.id == upload_id, FileUpload.token_hash == busy
            )
        )
    ).scalar_one_or_none()
    if row is None:
        # The GC swept the link while the body was sent (a PUT over an hour
        # past expiry). Nothing references the object: remove it.
        await db.rollback()
        try:
            await s3.delete(s3_key)
        except Exception:
            logger.warning("upload %s: could not delete an orphaned object", upload_id)
        raise _expired()
    row.token_hash = None
    row.uploaded_at = datetime.now(timezone.utc)
    row.size_bytes = len(payload)
    row.sha256 = hashlib.sha256(payload).hexdigest()
    await db.commit()
    await db.refresh(row)
    return FileUploadResponse.model_validate(row)


__all__ = ["router"]
