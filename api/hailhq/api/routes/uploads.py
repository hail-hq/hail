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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from hailhq.api.deps import Principal, get_current_principal, get_s3_mail_optional
from hailhq.api.errors import unprocessable
from hailhq.api.ratelimit import (
    GENERAL_RATE_LIMITED_RESPONSES,
    merge_rate_limited_responses,
)
from hailhq.core.config import settings
from hailhq.core.db import get_session, key_lock
from hailhq.core.file_uploads import (
    LINK_TTL,
    MAX_PENDING_LINKS,
    STORE_TIMEOUT,
    hash_token,
    matches_declared_type,
    max_bytes,
    new_token,
    object_key,
    object_prefix,
)
from hailhq.core.models import FileUpload
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.schemas import (
    FileUploadCreate,
    FileUploadLinkResponse,
    FileUploadResponse,
)
from hailhq.core.urls import join_url
from sqlalchemy import Row, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/uploads",
    tags=["uploads"],
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)

# The PUT has no bearer, so the general limit keys it on the caller's IP.
# In prod the API reads that IP from Caddy's X-Forwarded-For
# (docker-compose.prod.yml), so each caller gets its own bucket.

logger = logging.getLogger(__name__)

_NOT_FOUND = "upload link not found, or already used"
# Body reads one process runs at once. Each can buffer up to the purpose's
# size limit (25 MB for an email attachment), so this caps that memory.
# Over the cap a PUT answers 503 at once; nothing waits in a queue.
MAX_CONCURRENT_BODY_READS = 8
_reads_total = 0
# Body reads one organization runs at once in this process, so one tenant's
# slow uploads cannot hold every slot above.
MAX_BODY_READS_PER_ORG = 2
_org_reads: dict[UUID, int] = {}
# The longest one body read may take. Never past the link's expiry either.
MAX_BODY_READ_SECONDS = 600.0
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _server_full() -> bool:
    return _reads_total >= MAX_CONCURRENT_BODY_READS


def _take_read(org: UUID) -> bool:
    """Count one more body read, for the process and for ``org``. False,
    counting nothing, when either cap is reached. Synchronous: no await
    between the check and the count."""
    global _reads_total
    if _server_full() or _org_reads.get(org, 0) >= MAX_BODY_READS_PER_ORG:
        return False
    _reads_total += 1
    _org_reads[org] = _org_reads.get(org, 0) + 1
    return True


def _drop_read(org: UUID) -> None:
    """Undo ``_take_read``; an org at zero has no entry."""
    global _reads_total
    _reads_total -= 1
    left = _org_reads.get(org, 0) - 1
    if left > 0:
        _org_reads[org] = left
    else:
        _org_reads.pop(org, None)


def _read_deadline(expires_at: datetime, now: datetime) -> float:
    """Seconds a body read may take: until the link expires, at most
    ``MAX_BODY_READ_SECONDS``."""
    return max(0.0, min(MAX_BODY_READ_SECONDS, (expires_at - now).total_seconds()))


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
                "Unknown link, wrong token, or already used (also when another "
                "PUT to the same link finished first)."
            )
        },
        408: {
            "description": (
                "The body did not arrive before the link expired, or within "
                "10 minutes. The link is not used up."
            )
        },
        410: {
            "description": (
                "The link had expired when the PUT started. A PUT that starts "
                "in time is stored, also if it ends after the expiry."
            )
        },
        413: {"description": "The file is larger than the link's `max_bytes`."},
        503: {
            "description": (
                "File uploads are not configured on this server, storage "
                "failed, or too many uploads are running, in all or for this "
                "organization (`Retry-After`). The link is not used up."
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
    background: BackgroundTasks,
) -> FileUploadResponse:
    """Send the file's bytes to an upload link. No Authorization header.

    Works once. Unknown id, wrong token or a used link are all 404 with one
    message; a link already expired when the PUT starts is 410. A PUT that
    starts in time is stored, also if it ends after the expiry. A body that
    does not arrive before the link expires (10 minutes at most) is 408.
    Empty body: 422. A verification document whose first bytes are not the
    declared JPEG, PNG or PDF: 422.
    """
    if s3 is None:
        raise _unavailable()
    # A full server refuses before it touches the database.
    if _server_full():
        raise _busy()
    token_hash = hash_token(token)
    row = await db.get(FileUpload, upload_id)
    if (
        row is None
        or row.token_hash is None
        or not hmac.compare_digest(row.token_hash, token_hash)
    ):
        raise _not_found()
    if row.link_expires_at is None or row.link_expires_at <= _now():
        raise _expired()
    org = row.organization_id
    link = {
        "expires": row.link_expires_at,
        "purpose": row.purpose,
        "content_type": row.content_type,
        "base_key": row.s3_key,
    }
    # Nothing is held open while the body arrives.
    await db.rollback()

    # Each body read can hold up to the purpose's limit in memory, so this
    # process runs only a few at a time, and one organization only 2 of
    # them. That also bounds how many PUTs one link can have at once.
    if not _take_read(org):
        raise _busy()
    try:
        return await _store(
            db,
            s3,
            request,
            background,
            upload_id=upload_id,
            token_hash=token_hash,
            **link,
        )
    finally:
        _drop_read(org)


async def _store(
    db: AsyncSession,
    s3: S3MailClient,
    request: Request,
    background: BackgroundTasks,
    *,
    upload_id: UUID,
    token_hash: str,
    expires: datetime,
    purpose: str,
    content_type: str,
    base_key: str,
) -> FileUploadResponse:
    """Read the body, store it, then mark the link used in one atomic update.

    The link has no in-between state. Each PUT writes its own object key, so
    two PUTs to one link never overwrite each other. The update that marks
    the link used also points it at this PUT's key, and the winner then
    deletes any other attempt objects. If another PUT won, or the link went
    away, this PUT deletes its own object and answers 404. A failure before
    the update leaves the link usable; the link sweep removes what it left.
    Expiry is checked once, when the PUT starts."""
    try:
        async with asyncio.timeout(_read_deadline(expires, _now())):
            payload = await _read_body(request, max_bytes(purpose))
    except TimeoutError:
        raise HTTPException(
            status_code=http_status.HTTP_408_REQUEST_TIMEOUT,
            detail=(
                "the file did not arrive in time; the link still works "
                "until it expires"
            ),
        ) from None
    if not payload:
        raise unprocessable("the file is empty", loc=["body"])
    if purpose == "verification_document" and not matches_declared_type(
        payload, content_type
    ):
        raise unprocessable(f"the file does not look like {content_type}", loc=["body"])

    key = f"{base_key}.{uuid4().hex}"
    try:
        # Bounded, so the link sweep (LINK_SWEEP_GRACE past the expiry) never
        # runs while this PUT is still writing.
        async with asyncio.timeout(STORE_TIMEOUT.total_seconds()):
            stored = await _write_and_mark(
                db, s3, upload_id, token_hash, key, payload, content_type
            )
    except TimeoutError:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage unavailable; try again",
        ) from None
    if stored is None:
        # Nothing references this PUT's object: remove it.
        try:
            await s3.delete(key)
        except Exception:
            logger.warning(
                "upload %s: could not delete an unused object", upload_id, exc_info=True
            )
        # Another PUT finished first, or the link went away.
        raise _not_found()
    # Objects of earlier PUTs to this link that were cut off after their S3
    # write. Usually none. After the response, so no read slot waits on it.
    background.add_task(_delete_earlier_attempts, s3, upload_id, key)
    return FileUploadResponse.model_validate(stored)


async def _write_and_mark(
    db: AsyncSession,
    s3: S3MailClient,
    upload_id: UUID,
    token_hash: str,
    key: str,
    payload: bytes,
    content_type: str,
) -> Row | None:
    """Write this PUT's object, then mark the link used and point it there.
    None when the link is already used or gone."""
    try:
        await s3.put_attachment(key, payload, content_type)
    except Exception as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage unavailable; try again",
        ) from exc

    # If this fails, the object stays: the row may point at it if the commit
    # reached the database before the error. Otherwise the next PUT that
    # wins, or the link sweep, deletes it.
    now = _now()
    stored = (
        await db.execute(
            update(FileUpload)
            .where(
                FileUpload.id == upload_id,
                FileUpload.token_hash == token_hash,
                FileUpload.uploaded_at.is_(None),
            )
            .values(
                token_hash=None,
                uploaded_at=now,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                s3_key=key,
            )
            .returning(
                FileUpload.id,
                FileUpload.purpose,
                FileUpload.filename,
                FileUpload.content_type,
                FileUpload.size_bytes,
                FileUpload.sha256,
            )
        )
    ).one_or_none()
    await db.commit()
    return stored


async def _delete_earlier_attempts(s3: S3MailClient, upload_id: UUID, key: str) -> None:
    """Best effort: a failure only logs."""
    try:
        await s3.delete_prefix(object_prefix(key), keep=key)
    except Exception:
        logger.warning(
            "upload %s: could not delete earlier attempts", upload_id, exc_info=True
        )


__all__ = ["router"]
