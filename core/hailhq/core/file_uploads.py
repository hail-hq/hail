"""Rules shared by every uploaded file (the ``file_uploads`` table).

Three kinds of file share one table and one mail-bucket layout:

* ``email_attachment``: a file to attach to outbound email. Stored under
  ``outbound-attachments/<organization_id>/<id>.<attempt>``; its id works in
  ``POST /emails`` ``attachment_ids``.
* ``verification_document``: an identity or business document for a carrier
  verification. Stored under
  ``verification-uploads/<organization_id>/<id>.<attempt>``,
  read once by ``POST /verifications`` and deleted after; the GC removes any
  left over after ``VERIFICATION_UPLOAD_TTL``.
* ``knowledge_document``: a file for the workspace's knowledge base (PDF,
  DOCX, TXT, Markdown or CSV). Stored under
  ``knowledge-documents/<organization_id>/<id>.<attempt>``; its id works in
  ``POST /knowledge/documents``, which keeps it as long as the document
  lives. The GC removes one never used within ``UNUSED_TTL``.
* ``verification_kept``: a copy of a file a number registration was filed
  with, kept so a rejected filing can be fixed and sent again
  (``POST /verifications/{id}/resubmit``). Stored under
  ``verification-kept/<organization_id>/<id>`` with server-side encryption,
  linked to its ``carrier_verifications`` row and slot. Deleted
  ``REGISTRATION_KEPT_FOR`` after submit, or sooner when the registration is
  approved or cancelled or the account closes (the verification worker).
  Hail makes these itself; no client can upload one.

An upload link (``POST /uploads``) lets a caller with no Hail credentials
(an agent's shell) send the bytes once with ``PUT /uploads/<id>?token=...``.
Only the sha256 of the token is stored. Each PUT writes its own
``<attempt>`` key, and the update that marks the link used points the row at
it, so two PUTs to one link never overwrite each other. (A direct
``POST /email-attachments`` upload has no attempt suffix.)
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING, Literal, NamedTuple
from uuid import UUID, uuid4

from hailhq.core.config import settings
from hailhq.core.email_attachment_limits import MAX_EMAIL_ATTACHMENT_BYTES
from hailhq.core.s3_mail import S3MailClient
from sqlalchemy import delete, func, select

if TYPE_CHECKING:
    from hailhq.core.models import FileUpload
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# What a client may upload (``POST /uploads``).
Purpose = Literal["email_attachment", "verification_document", "knowledge_document"]
PURPOSES: tuple[str, ...] = (
    "email_attachment",
    "verification_document",
    "knowledge_document",
)
# Files Hail keeps itself: never a client upload.
KEPT_PURPOSE = "verification_kept"

# Carriers take these document types only.
VERIFICATION_FILE_TYPES: tuple[str, ...] = (
    "image/jpeg",
    "image/png",
    "application/pdf",
)
VERIFICATION_MAX_FILE_BYTES = 10 * 1024 * 1024

# What the knowledge base reads (hailhq.core.knowledge.decode).
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
KNOWLEDGE_TEXT_TYPES: tuple[str, ...] = ("text/plain", "text/markdown", "text/csv")
KNOWLEDGE_FILE_TYPES: tuple[str, ...] = (
    "application/pdf",
    DOCX_TYPE,
    *KNOWLEDGE_TEXT_TYPES,
)
# The largest knowledge file any workspace may hold: the body of an upload
# is read into memory, like an email attachment. A workspace's own limit
# (organization_knowledge.max_file_bytes) is at most this.
KNOWLEDGE_MAX_FILE_BYTES = 25 * 1024 * 1024

# How long an upload link works, and how many unused ones an org may hold.
LINK_TTL = timedelta(minutes=15)
MAX_PENDING_LINKS = 50
# The link sweep waits this long past a link's expiry, so a PUT whose body
# arrived in time can still write S3 and mark the link used.
LINK_SWEEP_GRACE = timedelta(minutes=15)
# An uploaded verification document is usable this long, then deleted.
VERIFICATION_UPLOAD_TTL = timedelta(hours=1)
# A number registration's answers and files are kept this long after
# submit, for a resubmit.
REGISTRATION_KEPT_FOR = timedelta(days=30)

_PREFIXES = {
    "email_attachment": "outbound-attachments/",
    "verification_document": "verification-uploads/",
    "knowledge_document": "knowledge-documents/",
    KEPT_PURPOSE: "verification-kept/",
}
EMAIL_ATTACHMENTS_PREFIX = _PREFIXES["email_attachment"]
VERIFICATION_UPLOADS_PREFIX = _PREFIXES["verification_document"]
VERIFICATION_KEPT_PREFIX = _PREFIXES[KEPT_PURPOSE]
KNOWLEDGE_DOCUMENTS_PREFIX = _PREFIXES["knowledge_document"]

# ``type/subtype`` only, no parameters: the value is put into a shell
# command (the curl line the MCP returns), so no quote or space may pass.
_CONTENT_TYPE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}"
)

# A PDF may carry a few bytes before its header; readers accept it within
# the first KiB.
_PDF_MARKER_WINDOW = 1024


def max_bytes(purpose: str) -> int:
    """The size limit of one file of this purpose."""
    if purpose in ("verification_document", KEPT_PURPOSE):
        return VERIFICATION_MAX_FILE_BYTES
    if purpose == "knowledge_document":
        # The ceiling; a workspace's own limit is checked on top of it.
        return KNOWLEDGE_MAX_FILE_BYTES
    return MAX_EMAIL_ATTACHMENT_BYTES


def _looks_like_text(content: bytes) -> bool:
    """UTF-8 text with no NUL byte in the first 64 KiB (a cut character at
    the end of the window is fine)."""
    head = content[: 64 * 1024]
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Only a character cut by the window, never a bad byte.
        return exc.start >= len(head) - 3 and len(head) == 64 * 1024
    return True


def matches_declared_type(content: bytes, content_type: str) -> bool:
    """True when the first bytes match the declared type: JPEG, PNG, PDF,
    DOCX (a zip), or UTF-8 text for TXT, Markdown and CSV."""
    if content_type in KNOWLEDGE_TEXT_TYPES:
        return _looks_like_text(content)
    if content_type == DOCX_TYPE:
        return content.startswith(b"PK\x03\x04")
    if content_type == "application/pdf":
        return b"%PDF-" in content[:_PDF_MARKER_WINDOW]
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    return False


def valid_content_type(value: str) -> bool:
    """A bare ``type/subtype`` MIME type."""
    return bool(_CONTENT_TYPE.fullmatch(value))


def object_key(purpose: str, organization_id: UUID, upload_id: UUID) -> str:
    """Where the upload's bytes live in the mail bucket."""
    return f"{_PREFIXES[purpose]}{organization_id}/{upload_id}"


async def store_upload(
    db: AsyncSession, s3: S3MailClient, row: FileUpload, payload: bytes
) -> FileUpload:
    """Store a direct upload with its cleanup record committed before S3.

    Callers supply the file's owner, purpose, name and MIME type. This owner
    computes its identity, key, size and digest. A failed write stays tracked
    and unused so the existing upload sweep can clean it up.
    """
    if row.purpose not in PURPOSES or len(payload) > max_bytes(row.purpose):
        raise ValueError("File exceeds its upload limit or has an invalid purpose")
    row.id = row.id or uuid4()
    row.s3_key = object_key(row.purpose, row.organization_id, row.id)
    row.size_bytes = len(payload)
    row.sha256 = hashlib.sha256(payload).hexdigest()
    row.uploaded_at = func.statement_timestamp()
    db.add(row)
    await db.commit()
    await s3.put_attachment(row.s3_key, payload, row.content_type)
    await db.refresh(row)
    return row


class KeptFile(NamedTuple):
    """One file of a number registration, to keep for a resubmit."""

    slot: str
    filename: str
    content_type: str
    data: bytes


def kept_filename(name: str | None) -> str:
    """The name shown back for a kept file: the last path part, at most
    200 characters, ``document`` when there is none."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return base[:200] or "document"


async def keep_verification_files(
    db: AsyncSession,
    s3: S3MailClient,
    organization_id: UUID,
    verification_id: UUID,
    files: list[KeptFile],
) -> int:
    """Keep encrypted copies of a registration's files; returns how many.

    Each row is committed first with ``uploaded_at`` null, so a crash after
    the S3 write leaves a row the worker cleans up; ``uploaded_at`` is set
    once the object is written. A failed write deletes its row and the rows
    not written yet, keeps the ones written, and raises: the caller decides
    what a failure means. Never logs a name."""
    # Here, not at the top: hailhq.core.models imports this module.
    from hailhq.core.models import FileUpload

    rows = []
    for file in files:
        row_id = uuid4()
        rows.append(
            (
                FileUpload(
                    id=row_id,
                    organization_id=organization_id,
                    purpose=KEPT_PURPOSE,
                    verification_id=verification_id,
                    slot=file.slot,
                    filename=kept_filename(file.filename),
                    content_type=file.content_type,
                    s3_key=object_key(KEPT_PURPOSE, organization_id, row_id),
                ),
                file,
            )
        )
    db.add_all([row for row, _ in rows])
    await db.commit()
    kept = 0
    try:
        for row, file in rows:
            await s3.put_attachment(
                row.s3_key, file.data, file.content_type, encrypt=True
            )
            row.size_bytes = len(file.data)
            row.sha256 = hashlib.sha256(file.data).hexdigest()
            row.uploaded_at = func.statement_timestamp()
            await db.commit()
            kept += 1
    except Exception:
        await db.rollback()
        # Only the files not written: the ones written stay kept.
        await delete_kept_files(db, s3, [row.id for row, _ in rows[kept:]])
        raise
    return kept


async def delete_kept_files(
    db: AsyncSession,
    s3: S3MailClient | None,
    upload_ids: list[UUID],
    *,
    verification_ids: list[UUID] | None = None,
) -> int:
    """Delete kept files (object, then row) one at a time; returns how many.
    A failed object delete keeps its row, so the worker tries again. With
    no bucket configured only the rows go: there is no object. With
    ``verification_ids``, only files still linked to one of them go."""
    # Here, not at the top: hailhq.core.models imports this module.
    from hailhq.core.models import FileUpload

    if not upload_ids:
        return 0
    query = select(FileUpload.id, FileUpload.s3_key).where(
        FileUpload.id.in_(upload_ids),
        FileUpload.purpose == KEPT_PURPOSE,
    )
    if verification_ids is not None:
        query = query.where(FileUpload.verification_id.in_(verification_ids))
    rows = (await db.execute(query)).all()
    await db.commit()
    deleted = 0
    for row_id, s3_key in rows:
        if s3 is not None:
            try:
                # One object per kept file, with no attempt suffix.
                await s3.delete(s3_key)
            except Exception as exc:
                logger.warning(
                    "could not delete kept registration file %s (%s); the "
                    "verification worker tries again",
                    row_id,
                    type(exc).__name__,
                )
                continue
        await db.execute(delete(FileUpload).where(FileUpload.id == row_id))
        await db.commit()
        deleted += 1
    return deleted


def object_prefix(s3_key: str) -> str:
    """The prefix holding every object of one upload: its key without the
    ``.<attempt>`` suffix. A PUT cut off between its S3 write and its update
    can leave an attempt object behind, so cleanups delete this prefix, not
    one key. Upload ids are UUIDs, so no other upload shares the prefix."""
    head, sep, name = s3_key.rpartition("/")
    return f"{head}{sep}{name.split('.', 1)[0]}"


async def delete_upload_objects(s3: S3MailClient, s3_key: str) -> None:
    """Delete every object of one upload. Listing the prefix needs
    ``s3:ListBucket`` on the bucket ARN with an ``s3:prefix`` condition for
    the upload folders (``infra/terraform/iam.tf``)."""
    await s3.delete_prefix(object_prefix(s3_key))


def new_token() -> str:
    """32 random bytes, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """The stored form of a link token (sha256 hex)."""
    return hashlib.sha256(token.encode()).hexdigest()


async def delete_org_verification_uploads(
    organization_id: UUID, store: S3MailClient | None = None
) -> int:
    """Delete one organization's verification documents, uploaded and kept;
    returns how many."""
    if store is None:
        if not settings.hail_mail_bucket:
            return 0
        store = S3MailClient(bucket=settings.hail_mail_bucket)
    deleted = 0
    for prefix in (VERIFICATION_UPLOADS_PREFIX, VERIFICATION_KEPT_PREFIX):
        deleted += await store.delete_prefix(f"{prefix}{organization_id}/")
    return deleted


__all__ = [
    "DOCX_TYPE",
    "EMAIL_ATTACHMENTS_PREFIX",
    "KEPT_PURPOSE",
    "KNOWLEDGE_DOCUMENTS_PREFIX",
    "KNOWLEDGE_FILE_TYPES",
    "KNOWLEDGE_MAX_FILE_BYTES",
    "KNOWLEDGE_TEXT_TYPES",
    "LINK_SWEEP_GRACE",
    "LINK_TTL",
    "MAX_PENDING_LINKS",
    "PURPOSES",
    "REGISTRATION_KEPT_FOR",
    "VERIFICATION_FILE_TYPES",
    "VERIFICATION_KEPT_PREFIX",
    "VERIFICATION_MAX_FILE_BYTES",
    "VERIFICATION_UPLOADS_PREFIX",
    "VERIFICATION_UPLOAD_TTL",
    "KeptFile",
    "Purpose",
    "delete_kept_files",
    "delete_org_verification_uploads",
    "delete_upload_objects",
    "hash_token",
    "keep_verification_files",
    "kept_filename",
    "matches_declared_type",
    "max_bytes",
    "new_token",
    "object_key",
    "object_prefix",
    "store_upload",
    "valid_content_type",
]
