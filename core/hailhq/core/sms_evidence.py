"""Where US SMS registration evidence files live in the mail bucket.

The API stores uploads under ``sms-evidence/<organization_id>/<token>``, from
``POST /verifications/sms/evidence`` (api/hailhq/api/routes/sms_evidence.py)
or from an upload link with purpose ``sms_evidence``
(api/hailhq/api/routes/uploads.py). Both follow the rules here. The
retention sweep deletes an organization's folder 12 months after it closes
(hailhq.core.retention).
"""

from __future__ import annotations

import ipaddress
import secrets
from urllib.parse import urlparse
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.urls import join_url

SMS_EVIDENCE_PREFIX = "sms-evidence/"
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
# Files one organization may keep. Normal use needs a few per registration;
# the cap stops a leaked key from filling storage.
MAX_EVIDENCE_FILES = 100
# The file's first bytes decide its type, never the name or the header the
# client sent. Only these types are kept or served: no HTML or SVG on the
# API's origin.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"%PDF-", "application/pdf"),
)
_PRIVATE_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
    ".test",
    ".example",
    ".invalid",
)


def sniff_evidence_type(data: bytes) -> str | None:
    """``image/png``, ``image/jpeg`` or ``application/pdf`` from the first
    bytes, else None."""
    return next((kind for sig, kind in _SIGNATURES if data.startswith(sig)), None)


def evidence_folder(organization_id: UUID) -> str:
    """The folder that holds one organization's evidence files."""
    return f"{SMS_EVIDENCE_PREFIX}{organization_id}/"


def new_evidence_token() -> str:
    """The random last part of a link: 32 URL-safe characters."""
    return secrets.token_urlsafe(24)


def public_evidence_base() -> str | None:
    """HAIL_API_URL when carrier reviewers can reach it, else None."""
    parsed = urlparse(settings.hail_api_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        return None
    try:
        return settings.hail_api_url if ipaddress.ip_address(host).is_global else None
    except ValueError:
        pass
    # A name must have a dot and must not use a suffix that only resolves
    # inside a network.
    if "." not in host or host.endswith(_PRIVATE_SUFFIXES):
        return None
    return settings.hail_api_url


def evidence_key(organization_id: UUID, token: str) -> str:
    """The storage key of one evidence file."""
    return f"{evidence_folder(organization_id)}{token}"


def evidence_url(base: str, organization_id: UUID, token: str) -> str:
    """The public link carrier reviewers open."""
    return join_url(base, f"v1/verifications/sms/evidence/{organization_id}/{token}")


async def delete_org_evidence(
    organization_id: UUID, store: S3MailClient | None = None
) -> int:
    """Delete one organization's evidence files; returns how many."""
    if store is None:
        if not settings.hail_mail_bucket:
            return 0
        store = S3MailClient(bucket=settings.hail_mail_bucket)
    return await store.delete_prefix(evidence_folder(organization_id))


__all__ = [
    "MAX_EVIDENCE_BYTES",
    "MAX_EVIDENCE_FILES",
    "SMS_EVIDENCE_PREFIX",
    "delete_org_evidence",
    "evidence_folder",
    "evidence_key",
    "evidence_url",
    "new_evidence_token",
    "public_evidence_base",
    "sniff_evidence_type",
]
