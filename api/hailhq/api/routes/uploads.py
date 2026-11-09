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
    merge_rate_limited_responses,
)
from hailhq.core.config import settings
from hailhq.core.db import get_session, key_lock, session_scope
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

# The PUT has no bearer, so the general limit keys it on the caller's IP.
# In prod the API reads that IP from Caddy's X-Forwarded-For
# (docker-compose.prod.yml), so each caller gets its own bucket.

logger = logging.getLogger(__name__)

_NOT_FOUND = "upload link not found, already used, or in use"
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


def _take_read(org: UUID) -> bool:
    """Count one more body read, for the process and for ``org``. False,
    counting nothing, when either cap is reached. Synchronous: no await
    between the check and the count."""
    global _reads_total
    if (
        _reads_total >= MAX_CONCURRENT_BODY_READS
        or _org_reads.get(org, 0) >= MAX_BODY_READS_PER_ORG
    ):
        return False
    _reads_total += 1
    _org_reads[org] = _org_reads.get(org, 0) + 1
    return True


def _drop_read(org: UUID) -> None:
    """Undo ``_take_read``. Never below zero; an org at zero has no entry."""
    global _reads_total
    _reads_total = max(0, _reads_total - 1)
    left = _org_reads.get(org, 0) - 1
    if left > 0:
        _org_reads[org] = left
    else:
        _org_reads.pop(org, None)


async def _give_back(upload_id: UUID, busy: str, token_hash: str) -> None:
    """Make a claimed link usable again. On its own session, so a broken
    request session cannot stop it; never raises into the request."""
    try:
        async with session_scope() as session:
            await session.execute(
                update(FileUpload)
                .where(FileUpload.id == upload_id, FileUpload.token_hash == busy)
                .values(token_hash=token_hash)
            )
            await session.commit()
    except BaseException:
        logger.warning("upload link %s was not given back", upload_id)


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
                "Unknown link, wrong token, already used, or another PUT is "
                "sending to it."
            )
        },
        408: {
            "description": (
                "The body did not arrive before the link expired, or within "
                "10 minutes. The link is not used up."
            )
        },
        410: {"description": "The link expired, also while its body was sent."},
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
) -> FileUploadResponse:
    """Send the file's bytes to an upload link. No Authorization header.

    Works once. Unknown id, wrong token, a used link or one another PUT is
    sending to are all 404 with one message; an expired link is 410. A body
    that does not arrive before the link expires (10 minutes at most) is
    408. Empty body: 422. A verification document whose first bytes are not
    the declared JPEG, PNG or PDF: 422.
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
    if row.link_expires_at is None or row.link_expires_at <= _now():
        raise _expired()
    org, expires = row.organization_id, row.link_expires_at

    # Each body read can hold up to the purpose's limit in memory, so this
    # process runs only a few at a time, and one organization only 2 of
    # them. Counted before the claim, so a busy server keeps the link.
    if not _take_read(org):
        raise _busy()
    try:
        return await _claim_and_store(db, s3, request, upload_id, token_hash, expires)
    finally:
        _drop_read(org)


async def _claim_and_store(
    db: AsyncSession,
    s3: S3MailClient,
    request: Request,
    upload_id: UUID,
    token_hash: str,
    expires: datetime,
) -> FileUploadResponse:
    """Claim the link, read the body, store it and mark the link used.

    The claim turns the token hash into a busy marker in one atomic update,
    so only one body read runs per link; a second PUT meanwhile gets 404.
    Anything that fails after the claim gives the link back, so the same
    link can be tried again."""
    busy = LINK_BUSY_PREFIX + token_hash
    claim_now = _now()
    claimed = (
        await db.execute(
            update(FileUpload)
            .where(
                FileUpload.id == upload_id,
                FileUpload.token_hash == token_hash,
                FileUpload.uploaded_at.is_(None),
                FileUpload.link_expires_at > claim_now,
            )
            .values(token_hash=busy)
            .returning(FileUpload.purpose, FileUpload.content_type, FileUpload.s3_key)
        )
    ).one_or_none()
    if claimed is None:
        await db.rollback()
        # Expired between the check above and this claim: say so.
        raise _expired() if expires <= claim_now else _not_found()
    # Committed, so nothing is held open while the body arrives.
    await db.commit()
    purpose, content_type, s3_key = claimed

    try:
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
        # Mark it used only while it is still this PUT's claim.
        stored = (
            await db.execute(
                update(FileUpload)
                .where(FileUpload.id == upload_id, FileUpload.token_hash == busy)
                .values(
                    token_hash=None,
                    uploaded_at=_now(),
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
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
    except BaseException:
        # Includes a client that hangs up mid-body and a failed commit. The
        # request's own transaction ends first, so it holds no row lock the
        # give-back would wait on; the give-back is shielded so a cancelled
        # request still returns the link.
        try:
            await asyncio.shield(db.rollback())
        except BaseException:
            logger.warning("upload %s: request rollback failed", upload_id)
        await asyncio.shield(_give_back(upload_id, busy, token_hash))
        raise

    if stored is None:
        # The link went away while the object was written (a sweep, or by
        # hand). Nothing references the object: remove it.
        try:
            await s3.delete(s3_key)
        except Exception:
            logger.warning("upload %s: could not delete an orphaned object", upload_id)
        raise _expired()
    return FileUploadResponse.model_validate(stored._mapping)


__all__ = ["router"]
