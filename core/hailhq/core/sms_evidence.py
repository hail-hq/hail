"""Where US SMS registration evidence files live in the mail bucket.

The API stores uploads under ``sms-evidence/<organization_id>/<token>``
(api/hailhq/api/routes/sms_evidence.py). The retention sweep deletes an
organization's folder 12 months after it closes (hailhq.core.retention).
"""

from __future__ import annotations

from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.s3_mail import S3MailClient

SMS_EVIDENCE_PREFIX = "sms-evidence/"


async def delete_org_evidence(
    organization_id: UUID, store: S3MailClient | None = None
) -> int:
    """Delete one organization's evidence files; returns how many."""
    if store is None:
        if not settings.hail_mail_bucket:
            return 0
        store = S3MailClient(bucket=settings.hail_mail_bucket)
    return await store.delete_prefix(f"{SMS_EVIDENCE_PREFIX}{organization_id}/")


__all__ = ["SMS_EVIDENCE_PREFIX", "delete_org_evidence"]
