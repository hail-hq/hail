"""Direct knowledge uploads: a browser sends a file straight to storage.

For the console (Uppy's ``@uppy/aws-s3`` ``signRequest``). CLI, MCP and
curl keep the upload link (``POST /uploads``, ``routes/uploads.py``), whose
bytes go through the API.

1. ``POST /knowledge/uploads`` reserves an upload: a ``file_uploads`` row
   with ``uploaded_at`` null, no link token, and a storage key Hail picks
   (``knowledge-documents/<org>/<id>.<attempt>``). The client never names a
   key.
2. ``POST /knowledge/uploads/{id}/sign`` presigns one storage request for
   that key: put the whole file, or the multipart steps (start, one part,
   list parts, finish, cancel). Nothing else is signed. Each URL works for
   at most ``SIGN_TTL`` and never past the reservation's expiry.
3. ``POST /knowledge/uploads/{id}/complete`` checks the stored file and
   makes the document like ``POST /knowledge/documents``. The file is first
   copied to a new key no URL was signed for, so a signed PUT still working
   cannot change it after the checks. Size, type and first bytes are checked
   on that copy (``matches_declared_type``, the upload link's own check).
   Any mismatch deletes the files and the reservation.

Abandoned reservations: the upload-link sweep (``email_attachment_gc``)
deletes a row never completed ``LINK_SWEEP_GRACE`` past its expiry, and
every object under its key. S3 aborts multipart uploads left unfinished
after a day (``infra/terraform/s3_mail.tf``). Reservations count toward the
organization's ``MAX_PENDING_LINKS`` unused upload links.
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi import status as http_status
from hailhq.api.deps import Principal, get_current_principal, get_s3_mail_optional
from hailhq.api.errors import unprocessable
from hailhq.api.ratelimit import (
    GENERAL_RATE_LIMITED_RESPONSES,
    merge_rate_limited_responses,
)
from hailhq.api.routes.knowledge import (
    add_document,
    ensure_room,
    knowledge_on,
    lock_upload,
)
from hailhq.core.db import get_session, key_lock
from hailhq.core.file_uploads import (
    KNOWLEDGE_MAX_FILE_BYTES,
    LINK_SWEEP_GRACE,
    LINK_TTL,
    MAX_PENDING_LINKS,
    delete_upload_objects,
    matches_declared_type,
    object_key,
    object_prefix,
)
from hailhq.core.knowledge.access import (
    KNOWLEDGE_OFF,
    knowledge_limits,
    knowledge_lock,
)
from hailhq.core.models import FileUpload
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.schemas import (
    KnowledgeDocumentResponse,
    KnowledgeUploadComplete,
    KnowledgeUploadCreate,
    KnowledgeUploadResponse,
    KnowledgeUploadSign,
    KnowledgeUploadSignResponse,
)
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/knowledge/uploads",
    tags=["knowledge"],
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)

# The longest a signed URL works.
SIGN_TTL = timedelta(minutes=5)
# S3's smallest part (all but the last); a part number past
# ceil(size / PART_BYTES) cannot belong to this file.
PART_BYTES = 5 * 1024 * 1024
# The first bytes read for the type check (``matches_declared_type`` reads
# at most 64 KiB).
_HEAD_BYTES = 64 * 1024
_STORE_GRACE_MINUTES = int(LINK_SWEEP_GRACE.total_seconds() // 60)
_UNAVAILABLE = "file uploads are not configured on this server"
_NOT_FOUND = "knowledge upload not found or already completed"
_EXPIRED = "this upload expired; start a new one with POST /v1/knowledge/uploads"

# (method, has s3_upload_id, has part_number) -> boto3 operation, and
# whether the request carries the file's type (signed, so sent exactly).
_OPERATIONS: dict[tuple[str, bool, bool], tuple[str, bool]] = {
    ("PUT", False, False): ("put_object", True),
    ("POST", False, False): ("create_multipart_upload", True),
    ("PUT", True, True): ("upload_part", False),
    ("GET", True, False): ("list_parts", False),
    ("POST", True, False): ("complete_multipart_upload", False),
    ("DELETE", True, False): ("abort_multipart_upload", False),
}

_OFF = {
    403: {
        "description": (
            "The knowledge base is not turned on for this workspace. Contact "
            "Hail support."
        )
    }
}
_NO_STORAGE = {503: {"description": "File uploads are not configured on this server."}}


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=_UNAVAILABLE
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)


def _expired() -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_410_GONE, detail=_EXPIRED)


def _off() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN, detail=KNOWLEDGE_OFF
    )


def _reserved(org: UUID, upload_id: UUID) -> tuple[Any, ...]:
    """SQL: the organization's direct upload, not completed yet. A direct
    upload has no link token; ``first_used_at`` is set when the knowledge
    base is turned off (``hailhq.core.knowledge.access``)."""
    return (
        FileUpload.id == upload_id,
        FileUpload.organization_id == org,
        FileUpload.purpose == "knowledge_document",
        FileUpload.uploaded_at.is_(None),
        FileUpload.token_hash.is_(None),
        FileUpload.first_used_at.is_(None),
        FileUpload.link_expires_at.is_not(None),
    )


@router.post(
    "",
    response_model=KnowledgeUploadResponse,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="create_knowledge_upload",
    responses={
        **merge_rate_limited_responses(
            GENERAL_RATE_LIMITED_RESPONSES,
            {
                429: {
                    "description": (
                        f"Or: the organization already holds {MAX_PENDING_LINKS} "
                        "unused, unexpired uploads. Use or let some expire."
                    )
                }
            },
        ),
        **_OFF,
        409: {
            "description": (
                "The workspace holds its limit of documents, or Hail's "
                "knowledge storage is full."
            )
        },
        413: {"description": "`size_bytes` is over the workspace's file limit."},
        **_NO_STORAGE,
    },
)
async def create_knowledge_upload(
    body: KnowledgeUploadCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3MailClient | None, Depends(get_s3_mail_optional)],
) -> KnowledgeUploadResponse:
    """Reserve a storage key for one knowledge file a browser sends
    straight to storage.

    Then sign each storage request with `POST /v1/knowledge/uploads/{id}/sign`,
    send the file, and call `POST /v1/knowledge/uploads/{id}/complete`.
    Sign requests until `expires_at` (15 minutes). Without a browser, use
    an upload link (`POST /v1/uploads`) instead.
    """
    if s3 is None:
        raise _unavailable()
    org = principal.organization_id
    # Same lock order as POST /uploads: knowledge, then upload links.
    await knowledge_lock(db, org)
    limits = await knowledge_on(db, org)
    limit = await ensure_room(db, org, limits, body.size_bytes)
    await key_lock(db, f"upload-links:{org}")
    pending = (
        await db.execute(
            select(func.count())
            .select_from(FileUpload)
            .where(
                FileUpload.organization_id == org,
                FileUpload.uploaded_at.is_(None),
                FileUpload.link_open(),
            )
        )
    ).scalar_one()
    if pending >= MAX_PENDING_LINKS:
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"too many unused uploads ({MAX_PENDING_LINKS}); use them or "
                "wait for them to expire"
            ),
        )
    upload_id = uuid4()
    row = FileUpload(
        id=upload_id,
        organization_id=org,
        purpose="knowledge_document",
        filename=body.filename,
        content_type=body.content_type,
        # The declared size until /complete stores the real one.
        size_bytes=body.size_bytes,
        s3_key=f"{object_key('knowledge_document', org, upload_id)}.{uuid4().hex}",
        link_expires_at=func.statement_timestamp() + LINK_TTL,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return KnowledgeUploadResponse(
        id=row.id,
        key=row.s3_key,
        content_type=row.content_type,
        max_bytes=limit,
        expires_at=row.link_expires_at,
    )


@router.post(
    "/{upload_id}/sign",
    response_model=KnowledgeUploadSignResponse,
    operation_id="sign_knowledge_upload",
    responses={
        **_OFF,
        404: {"description": "No such upload in this workspace, or completed."},
        410: {"description": "The upload expired. Start a new one."},
        422: {
            "description": (
                "Not one of the six storage requests, or a part number past "
                "the file's size."
            )
        },
        **_NO_STORAGE,
    },
)
async def sign_knowledge_upload(
    upload_id: UUID,
    body: KnowledgeUploadSign,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3MailClient | None, Depends(get_s3_mail_optional)],
) -> KnowledgeUploadSignResponse:
    """Presign one storage request for the upload's own key.

    Six requests only: `PUT` (the whole file), `POST` (start a multipart
    upload), `PUT` with `s3_upload_id` and `part_number` (one part), `GET`
    with `s3_upload_id` (list parts), `POST` with `s3_upload_id` (finish)
    and `DELETE` with `s3_upload_id` (cancel). Send the request to `url`
    with `headers`, exactly. The URL works for 5 minutes at most, never
    past the upload's `expires_at`.
    """
    if s3 is None:
        raise _unavailable()
    org = principal.organization_id
    found = (
        await db.execute(
            select(
                FileUpload.s3_key,
                FileUpload.content_type,
                FileUpload.size_bytes,
                FileUpload.link_open(),
                FileUpload.link_expires_at,
                func.statement_timestamp(),
            ).where(*_reserved(org, upload_id))
        )
    ).one_or_none()
    if found is None:
        raise _not_found()
    key, content_type, size, is_open, expires, db_now = found
    if not is_open:
        raise _expired()
    if await knowledge_limits(db, org) is None:
        raise _off()
    await db.rollback()

    has_id = body.s3_upload_id is not None
    has_part = body.part_number is not None
    operation, typed = _OPERATIONS[(body.method, has_id, has_part)]
    params: dict[str, Any] = {}
    headers: dict[str, str] = {}
    if typed:
        params["ContentType"] = content_type
        headers["content-type"] = content_type
    if has_id:
        params["UploadId"] = body.s3_upload_id
    if has_part:
        most = max(1, math.ceil((size or 0) / PART_BYTES))
        if body.part_number > most:
            raise unprocessable(
                f"part_number must be at most {most} for this file's size",
                loc=["body", "part_number"],
            )
        params["PartNumber"] = body.part_number
    ttl = int(min(SIGN_TTL, expires - db_now).total_seconds())
    if ttl < 1:
        raise _expired()
    url = await s3.presign(
        operation, key, ttl_seconds=ttl, http_method=body.method, **params
    )
    return KnowledgeUploadSignResponse(
        url=url,
        key=key,
        headers=headers,
        expires_at=db_now + timedelta(seconds=ttl),
    )


class _Rejected(Exception):
    """The stored file does not match the upload: delete it, answer this."""

    def __init__(self, error: HTTPException) -> None:
        self.error = error


@router.post(
    "/{upload_id}/complete",
    response_model=KnowledgeDocumentResponse,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="complete_knowledge_upload",
    responses={
        **_OFF,
        404: {"description": "No such upload in this workspace, or completed."},
        409: {
            "description": (
                "The file is not in storage yet (finish the upload, then call "
                "again), the workspace holds its limit of documents, or Hail's "
                "knowledge storage is full."
            )
        },
        410: {
            "description": (
                f"The upload expired more than {_STORE_GRACE_MINUTES} minutes "
                "ago. Start a new one."
            )
        },
        413: {"description": "The file is larger than the workspace's file limit."},
        422: {
            "description": (
                "The stored file's size is not `size_bytes`, or it is not its "
                "declared type. The file is deleted; start a new upload."
            )
        },
        503: {
            "description": (
                "File uploads are not configured on this server, storage "
                "failed, or the upload is busy for a moment: try again."
            )
        },
    },
)
async def complete_knowledge_upload(
    upload_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3MailClient | None, Depends(get_s3_mail_optional)],
    background: BackgroundTasks,
    body: KnowledgeUploadComplete | None = None,
) -> KnowledgeDocumentResponse:
    """Check the file the browser stored and add it to the knowledge base.

    Call it after the upload (for a multipart upload, after it finished),
    at most 15 minutes after `expires_at`. Hail checks the size, the type
    and the first bytes, like an upload link's PUT. On a mismatch the file
    is deleted (422 or 413). Then it works like `POST /v1/knowledge/documents`:
    poll `GET /v1/knowledge/documents/{id}` until `status` is `ready` or
    `failed`.
    """
    if s3 is None:
        raise _unavailable()
    org = principal.organization_id
    found = (
        await db.execute(
            select(FileUpload, FileUpload.link_storable()).where(
                *_reserved(org, upload_id)
            )
        )
    ).one_or_none()
    if found is None:
        raise _not_found()
    row, storable = found
    if not storable:
        raise _expired()
    key, content_type, declared = row.s3_key, row.content_type, row.size_bytes or 0
    limits = await knowledge_limits(db, org)
    if limits is None:
        raise _off()
    limit = min(KNOWLEDGE_MAX_FILE_BYTES, limits.max_file_bytes)
    # No lock or transaction is held during the storage calls.
    await db.rollback()

    try:
        stored = await s3.head(key)
    except Exception as exc:
        raise _storage_failed() from exc
    if stored is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="the file is not in storage yet; finish the upload, then call again",
        )
    final = f"{object_prefix(key)}.{uuid4().hex}"
    try:
        size = await _check(s3, key, final, stored, content_type, declared, limit)
    except _Rejected as rejected:
        await _discard(db, s3, org, upload_id, final)
        raise rejected.error from None
    except Exception as exc:
        await _forget(s3, final)
        raise _storage_failed() from exc

    try:
        await knowledge_lock(db, org)
        limits = await knowledge_on(db, org)
        upload = await lock_upload(
            db, *_reserved(org, upload_id), FileUpload.link_storable()
        )
        if upload is None:
            # Completed by another request, swept, or the knowledge base was
            # turned off meanwhile: this copy is not used.
            raise _not_found()
        upload.uploaded_at = func.statement_timestamp()
        upload.size_bytes = size
        upload.s3_key = final
        doc = await add_document(db, principal, upload, limits, body and body.name)
    except HTTPException as exc:
        await db.rollback()
        if exc.status_code == http_status.HTTP_404_NOT_FOUND:
            await _forget(s3, final)
        elif exc.status_code != http_status.HTTP_503_SERVICE_UNAVAILABLE:
            await _discard(db, s3, org, upload_id, final)
        else:
            await _forget(s3, final)
        raise
    except BaseException:
        await db.rollback()
        await _forget(s3, final)
        raise
    # The browser's own objects (the signed key, any other attempt). After
    # the response; a failure only logs.
    background.add_task(_delete_others, s3, upload_id, final)
    return doc


async def _check(
    s3: S3MailClient,
    key: str,
    final: str,
    stored: dict[str, Any],
    content_type: str,
    declared: int,
    limit: int,
) -> int:
    """Copy the stored file to ``final`` and check the copy. Returns its
    size; raises ``_Rejected`` on a mismatch."""
    if stored["ContentLength"] > limit:
        raise _Rejected(
            HTTPException(
                status_code=http_status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"the file is larger than {limit} bytes",
            )
        )
    if stored["ContentType"] != content_type:
        raise _Rejected(
            unprocessable(f"the stored file's type is not {content_type}", loc=["body"])
        )
    await s3.copy(key, final, content_type)
    copied = await s3.head(final)
    size = copied["ContentLength"] if copied is not None else 0
    if size == 0:
        raise _Rejected(unprocessable("the file is empty", loc=["body"]))
    if size > limit:
        raise _Rejected(
            HTTPException(
                status_code=http_status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"the file is larger than {limit} bytes",
            )
        )
    if size != declared:
        raise _Rejected(
            unprocessable(
                f"the stored file has {size} bytes, not the {declared} declared",
                loc=["body"],
            )
        )
    first = await s3.fetch_head(final, _HEAD_BYTES)
    if not matches_declared_type(first, content_type):
        raise _Rejected(
            unprocessable(f"the file does not look like {content_type}", loc=["body"])
        )
    return size


def _storage_failed() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="storage unavailable; try again",
    )


async def _forget(s3: S3MailClient, key: str) -> None:
    """Delete one object this request made. Best effort."""
    try:
        await s3.delete(key)
    except Exception:
        logger.warning("knowledge upload: could not delete a copy", exc_info=True)


async def _discard(
    db: AsyncSession, s3: S3MailClient, org: UUID, upload_id: UUID, final: str
) -> None:
    """Delete a rejected upload: its row while still reserved, then every
    object under its key. When another request completed it first, only
    this request's own copy goes. Best effort for the objects: the
    reservation is gone, so the folder is swept with the knowledge base."""
    gone = (
        await db.execute(
            delete(FileUpload)
            .where(*_reserved(org, upload_id))
            .returning(FileUpload.s3_key)
        )
    ).scalar_one_or_none()
    await db.commit()
    if gone is None:
        await _forget(s3, final)
        return
    try:
        await delete_upload_objects(s3, gone)
    except Exception:
        logger.warning(
            "knowledge upload %s: could not delete rejected files",
            upload_id,
            exc_info=True,
        )


async def _delete_others(s3: S3MailClient, upload_id: UUID, keep: str) -> None:
    try:
        await s3.delete_prefix(object_prefix(keep), keep=keep)
    except Exception:
        logger.warning(
            "knowledge upload %s: could not delete the uploaded original",
            upload_id,
            exc_info=True,
        )


@router.delete(
    "/{upload_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    operation_id="delete_knowledge_upload",
    responses={
        404: {"description": "No such upload in this workspace, or completed."},
        **_NO_STORAGE,
    },
)
async def delete_knowledge_upload(
    upload_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3MailClient | None, Depends(get_s3_mail_optional)],
) -> Response:
    """Cancel an upload not completed yet and delete what it stored. A
    completed upload is a document: delete it with
    `DELETE /v1/knowledge/documents/{id}`. Uploads never completed are
    deleted by themselves after they expire."""
    if s3 is None:
        raise _unavailable()
    org = principal.organization_id
    key = (
        await db.execute(
            delete(FileUpload)
            .where(*_reserved(org, upload_id))
            .returning(FileUpload.s3_key)
        )
    ).scalar_one_or_none()
    await db.commit()
    if key is None:
        raise _not_found()
    try:
        await delete_upload_objects(s3, key)
    except Exception:
        logger.warning(
            "knowledge upload %s: could not delete its files", upload_id, exc_info=True
        )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
