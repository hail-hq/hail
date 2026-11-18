"""The files of a verification form: ``file.<slot>`` parts and
``upload.<slot>`` ids (a ``verification_document`` uploaded with
``POST /uploads``). One loader for every route that takes them, so the
rules are the same everywhere: JPG, PNG or PDF, 10 MB per file, 30 MB in
total, an upload of this organization less than 1 hour old, and a slot sent
one way or the other, not both.

Loading has two steps. ``collect_files`` reads the database rows and the
request parts. ``fetch_files`` reads the uploads from the bucket. A caller
can end its database transaction between the two. Files stay in memory here;
a filed number registration keeps an encrypted copy for a resubmit
(``verification_kept`` in ``hailhq.core.file_uploads``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import NamedTuple
from uuid import UUID

from fastapi import HTTPException
from fastapi import status as http_status
from hailhq.api.errors import unprocessable
from hailhq.core.file_uploads import (
    VERIFICATION_FILE_TYPES,
    VERIFICATION_MAX_FILE_BYTES,
    VERIFICATION_UPLOAD_TTL,
)
from hailhq.core.models import FileUpload
from hailhq.core.providers.verification import UploadedFile
from hailhq.core.s3_mail import S3MailClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ALLOWED_FILE_TYPES",
    "MAX_FILE_BYTES",
    "MAX_REQUEST_BYTES",
    "CollectedFiles",
    "collect_files",
    "fetch_files",
    "files_too_large",
    "read_file",
]

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = VERIFICATION_MAX_FILE_BYTES
MAX_REQUEST_BYTES = 30 * 1024 * 1024
ALLOWED_FILE_TYPES = set(VERIFICATION_FILE_TYPES)
_READ_CHUNK = 1024 * 1024
# ``upload.<slot>`` objects fetched from the bucket at once.
MAX_PARALLEL_FETCHES = 4


def files_too_large() -> HTTPException:
    return unprocessable("the files total more than 30 MB", loc=["body", "files"])


async def read_file(upload) -> UploadedFile:
    """A ``file.<slot>`` part, read into memory with the size cap."""
    if upload.content_type not in ALLOWED_FILE_TYPES:
        raise unprocessable(
            "files must be JPG, PNG or PDF",
            loc=["body", "files", upload.filename or ""],
        )
    buf = bytearray()
    while True:
        chunk = await upload.read(_READ_CHUNK)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_FILE_BYTES:
            raise unprocessable(
                "each file must be 10 MB or smaller", loc=["body", "files"]
            )
    return UploadedFile(
        filename="upload", content_type=upload.content_type, data=bytes(buf)
    )


async def usable_upload(
    db: AsyncSession, organization_id: UUID, slot: str, value: object
) -> FileUpload:
    """The ``upload.<slot>`` row: this organization's verification document,
    uploaded less than ``VERIFICATION_UPLOAD_TTL`` ago. Else 422 naming the
    slot only."""
    where = ["body", f"upload.{slot}"]
    try:
        upload_id = UUID(str(value))
    except ValueError:
        raise unprocessable(f"upload.{slot} must be an upload id", loc=where) from None
    row = (
        await db.execute(
            select(FileUpload).where(
                FileUpload.id == upload_id,
                FileUpload.organization_id == organization_id,
                FileUpload.purpose == "verification_document",
                FileUpload.uploaded_at.is_not(None),
                FileUpload.uploaded_at
                > func.statement_timestamp() - VERIFICATION_UPLOAD_TTL,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise unprocessable(
            f"upload.{slot}: no usable upload with this id; it must be a "
            "verification_document of this organization, uploaded less than "
            "1 hour ago",
            loc=where,
        )
    return row


async def read_upload(s3: S3MailClient, row: FileUpload) -> UploadedFile:
    """The upload's bytes from the mail bucket, in memory only."""
    try:
        data = await s3.fetch_raw(row.s3_key)
    except Exception as exc:
        logger.warning("verification upload read failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage unavailable; try again",
        ) from exc
    if row.content_type not in ALLOWED_FILE_TYPES or len(data) > MAX_FILE_BYTES:
        # The PUT checked both; this is a guard against an edited row.
        raise unprocessable("files must be JPG, PNG or PDF, 10 MB or smaller")
    return UploadedFile(filename="upload", content_type=row.content_type, data=data)


class CollectedFiles(NamedTuple):
    """The upload rows and the file parts of a form, by slot, before any
    upload is fetched."""

    rows: dict[str, FileUpload]
    parts: dict[str, UploadedFile]


async def collect_files(
    form,
    slots: Iterable[str],
    db: AsyncSession,
    organization_id: UUID,
    s3: S3MailClient | None,
    *,
    require_s3: bool = True,
) -> CollectedFiles:
    """The ``upload.<slot>`` rows (one session: one query at a time) and the
    ``file.<slot>`` parts of ``slots``, then the total size from both, all
    before any object is fetched. An ``upload.`` part for another slot is
    a 422. With ``require_s3`` False, uploads are collected even without a
    bucket (the caller then leaves them unread) instead of a 503."""
    slots = list(slots)
    for key in form:
        if key.startswith("upload.") and key[len("upload.") :] not in slots:
            raise unprocessable(
                "this upload names a slot that is not in documents",
                loc=["body", key],
            )
    rows: dict[str, FileUpload] = {}
    parts: dict[str, UploadedFile] = {}
    total = 0
    for slot in slots:
        part = form.get(f"file.{slot}")
        has_part = part is not None and hasattr(part, "read")
        upload_ref = form.get(f"upload.{slot}")
        if upload_ref is not None and upload_ref != "":
            if has_part:
                raise unprocessable(
                    f"send file.{slot} or upload.{slot}, not both",
                    loc=["body", f"upload.{slot}"],
                )
            rows[slot] = await usable_upload(db, organization_id, slot, upload_ref)
        elif has_part:
            parts[slot] = await read_file(part)
            total += len(parts[slot].data)
            if total > MAX_REQUEST_BYTES:
                raise files_too_large()
    # Per slot: one upload in two slots is counted (and used) twice.
    total += sum(row.size_bytes or 0 for row in rows.values())
    if total > MAX_REQUEST_BYTES:
        raise files_too_large()
    if rows and s3 is None and require_s3:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="file uploads are not configured on this server",
        )
    return CollectedFiles(rows=rows, parts=parts)


async def fetch_files(
    collected: CollectedFiles,
    slots: Iterable[str],
    s3: S3MailClient | None,
    *,
    gate: asyncio.Semaphore | None = None,
) -> dict[str, UploadedFile]:
    """The files of ``slots``, by slot, in that order. Each distinct upload
    is fetched once, a few at a time. Every fetch finishes (the total was
    checked, so this is bounded), then the first failure in slot order is
    raised, with its cause: the same request always gets the same error.
    The total is checked again on the bytes read: an object may differ from
    its row. ``gate`` limits the fetches of several calls together; without
    it, this call has its own limit of ``MAX_PARALLEL_FETCHES``."""
    slots = list(slots)
    # Only the uploads of these slots: a caller may read fewer slots than
    # it collected.
    rows = {slot: row for slot, row in collected.rows.items() if slot in slots}
    parts = collected.parts
    fetches = gate or asyncio.Semaphore(MAX_PARALLEL_FETCHES)

    async def fetch(row: FileUpload) -> UploadedFile:
        async with fetches:
            assert s3 is not None
            return await read_upload(s3, row)

    distinct = {row.id: row for row in rows.values()}
    results = await asyncio.gather(
        *(fetch(row) for row in distinct.values()), return_exceptions=True
    )
    fetched = dict(zip(distinct, results))
    # The first failed fetch in slot order first, then the sizes.
    for slot in slots:
        if slot in rows and isinstance(fetched[rows[slot].id], BaseException):
            raise fetched[rows[slot].id]
    files: dict[str, UploadedFile] = {}
    total = 0
    for slot in slots:
        if slot in rows:
            file: UploadedFile | None = fetched[rows[slot].id]
        else:
            file = parts.get(slot)
        if file is None:
            continue
        total += len(file.data)
        if total > MAX_REQUEST_BYTES:
            raise files_too_large()
        files[slot] = file
    return files
