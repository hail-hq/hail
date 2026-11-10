"""Rules shared by every uploaded file (the ``file_uploads`` table).

Two kinds of upload share one table and one mail-bucket layout:

* ``email_attachment``: a file to attach to outbound email. Stored under
  ``outbound-attachments/<organization_id>/<id>.<attempt>``; its id works in
  ``POST /emails`` ``attachment_ids``.
* ``verification_document``: an identity or business document for a carrier
  verification. Stored under
  ``verification-uploads/<organization_id>/<id>.<attempt>``,
  read once by ``POST /verifications`` and deleted after; the GC removes any
  left over after ``VERIFICATION_UPLOAD_TTL``.

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
from typing import Literal
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.email_attachment_limits import MAX_EMAIL_ATTACHMENT_BYTES
from hailhq.core.s3_mail import S3MailClient

logger = logging.getLogger(__name__)

Purpose = Literal["email_attachment", "verification_document"]
PURPOSES: tuple[str, ...] = ("email_attachment", "verification_document")

# Carriers take these document types only.
VERIFICATION_FILE_TYPES: tuple[str, ...] = (
    "image/jpeg",
    "image/png",
    "application/pdf",
)
VERIFICATION_MAX_FILE_BYTES = 10 * 1024 * 1024

# How long an upload link works, and how many unused ones an org may hold.
LINK_TTL = timedelta(minutes=15)
MAX_PENDING_LINKS = 50
# The link sweep waits this long past a link's expiry. A PUT's body read
# ends by the expiry, so this leaves its S3 write and final update time to
# finish before the sweep deletes the link's objects.
LINK_SWEEP_GRACE = timedelta(minutes=15)
# An uploaded verification document is usable this long, then deleted.
VERIFICATION_UPLOAD_TTL = timedelta(hours=1)

_PREFIXES = {
    "email_attachment": "outbound-attachments/",
    "verification_document": "verification-uploads/",
}
VERIFICATION_UPLOADS_PREFIX = _PREFIXES["verification_document"]

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
    if purpose == "verification_document":
        return VERIFICATION_MAX_FILE_BYTES
    return MAX_EMAIL_ATTACHMENT_BYTES


def matches_declared_type(content: bytes, content_type: str) -> bool:
    """True when the first bytes match the declared JPEG, PNG or PDF type."""
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


def object_prefix(s3_key: str) -> str:
    """The prefix holding every object of one upload: its key without the
    ``.<attempt>`` suffix. A PUT cut off between its S3 write and its update
    can leave an attempt object behind, so cleanups delete this prefix, not
    one key. Upload ids are UUIDs, so no other upload shares the prefix."""
    head, sep, name = s3_key.rpartition("/")
    return f"{head}{sep}{name.split('.', 1)[0]}"


async def delete_upload_objects(s3: S3MailClient, s3_key: str) -> None:
    """Delete every object of one upload. Listing a prefix needs
    ``s3:ListBucket``; where the bucket policy lacks it, the one key the row
    points at is deleted instead. Raises only if that fails too."""
    try:
        await s3.delete_prefix(object_prefix(s3_key))
    except Exception:
        logger.warning("upload prefix delete failed; deleting the one key")
        await s3.delete(s3_key)


def new_token() -> str:
    """32 random bytes, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """The stored form of a link token (sha256 hex)."""
    return hashlib.sha256(token.encode()).hexdigest()


async def delete_org_verification_uploads(
    organization_id: UUID, store: S3MailClient | None = None
) -> int:
    """Delete one organization's verification documents; returns how many."""
    if store is None:
        if not settings.hail_mail_bucket:
            return 0
        store = S3MailClient(bucket=settings.hail_mail_bucket)
    return await store.delete_prefix(f"{VERIFICATION_UPLOADS_PREFIX}{organization_id}/")


__all__ = [
    "LINK_SWEEP_GRACE",
    "LINK_TTL",
    "MAX_PENDING_LINKS",
    "PURPOSES",
    "VERIFICATION_FILE_TYPES",
    "VERIFICATION_MAX_FILE_BYTES",
    "VERIFICATION_UPLOADS_PREFIX",
    "VERIFICATION_UPLOAD_TTL",
    "Purpose",
    "delete_org_verification_uploads",
    "delete_upload_objects",
    "hash_token",
    "matches_declared_type",
    "max_bytes",
    "new_token",
    "object_key",
    "object_prefix",
    "valid_content_type",
]
