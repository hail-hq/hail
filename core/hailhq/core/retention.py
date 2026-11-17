"""Post-account-closure retention sweep.

Founder policy: transcripts and stored email content are retained for the
account's duration + 12 months after account closure. hail does not own
account/org lifecycle state: hail-website owns the ``organizations`` table
(better-auth schema) in the same database and deletes the row on account
deletion, so hail cannot tell from it whether an org is closed or when.
That's what ``org_closures`` (see ``hailhq.core.models.OrgClosure``)
records. hail-website's account deletion inserts the row in its own
deletion transaction, then calls ``POST /internal/org-closures`` (see
``api/hailhq/api/routes/internal/org_closures.py``).

An org with no ``org_closures`` row, or one closed less than 12 months
ago, is left completely untouched by this sweep.

Deliberately does **not** touch ``audit_log`` rows: that's a separate,
intentionally longer-retained trail (security/legal-obligation record),
not customer content — see ``hailhq.core.models.AuditLog``.

Also hard-deletes ``contacts`` rows for closed orgs. The contacts-v2 design
originally specced ``contacts.organization_id`` as
``references organizations(id) on delete cascade``; ``organizations`` is a
website-owned table in the shared Postgres, and this repo's convention is no
FKs to website-owned tables (see ``hailhq.core.models.Contact`` / the same
convention this
module's docstring already leans on for ``org_closures``). This sweep is the
explicit replacement for that cascade. Unlike Call/Sms/Email, contacts have
no separate "content" field to scrub — a ``Contact`` row *is* identifying
data (name, phone, email) — so it's deleted outright, not content-cleared.

Also clears the registration answers on ``carrier_verifications`` at close,
with no 12-month wait: see ``clear_closed_org_registrations``. The files a
number registration kept for a resubmit go at close too
(``hailhq.api.routes.verifications.forget_kept_registrations``).

Run as a one-shot script::

    cd core && uv run python -m hailhq.core.retention

No cron / systemd timer is wired up by this change — scheduling the sweep
is a separate ops/infra decision left for later.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from hailhq.core.config import SELF_HOSTED_ORG_ID
from hailhq.core.file_uploads import delete_org_verification_uploads
from hailhq.core.models import (
    Call,
    CarrierVerification,
    Contact,
    Email,
    FileUpload,
    OrgClosure,
    Sms,
)
from hailhq.core.recordings import delete_recording
from hailhq.core.sms_evidence import delete_org_evidence
from sqlalchemy import Text, and_, case, cast, delete, exists, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

__all__ = [
    "REGISTRATION_ANSWER_REFS",
    "RETENTION_PERIOD_AFTER_CLOSURE",
    "PurgeSummary",
    "clear_closed_org_registrations",
    "closed_org_ids",
    "org_closed",
    "org_is_closed",
    "purge_expired_data",
]

# "12 months" approximated as a fixed day-count. This is a compliance
# boundary, not a billing-precision figure, so a plain timedelta avoids
# pulling in a calendar-math dependency (e.g. dateutil.relativedelta) for
# one constant.
RETENTION_PERIOD_AFTER_CLOSURE = timedelta(days=365)


# The ``provider_refs`` keys that hold what the customer typed into a US
# texting registration or a number registration, or text made from it: the
# answers (names, address, tax ID, contacts; a number registration's
# document options too), a hash of the last amendment, the texting program text,
# the filed STOP/HELP replies (sender name, contact email) and the sender
# name. Every other key is a carrier id, a flag or a time
# (core/tests/test_retention.py checks the list).
REGISTRATION_ANSWER_REFS = (
    "application",
    "application_amendment_hash",
    "campaign",
    "replies",
    "subject_name",
)


def closed_org_ids(now: datetime, organization_id: UUID | None = None):
    """SELECT of the organizations closed by ``now`` (one org when
    ``organization_id`` is given). Only an ``org_closures`` row closes an
    org: a missing ``organizations`` row does not (the self-hosted org has
    none). The self-hosted org is never closed, even with a row: what reads
    this releases numbers and deletes data."""
    closed = select(OrgClosure.organization_id).where(
        OrgClosure.closed_at <= now,
        OrgClosure.organization_id != SELF_HOSTED_ORG_ID,
    )
    if organization_id is not None:
        closed = closed.where(OrgClosure.organization_id == organization_id)
    return closed


def org_closed(org_column, now: datetime):
    """SQL test: the org in ``org_column`` is closed by ``now``. NOT EXISTS
    (``~org_closed(...)``) keeps rows whose org is NULL (pool numbers),
    which ``NOT IN`` would drop. Never true for the self-hosted org, as in
    ``closed_org_ids``."""
    return exists().where(
        OrgClosure.organization_id == org_column,
        OrgClosure.closed_at <= now,
        OrgClosure.organization_id != SELF_HOSTED_ORG_ID,
    )


async def org_is_closed(
    session: AsyncSession, organization_id: UUID | None, now: datetime | None = None
) -> bool:
    """True when ``organization_id`` has an ``org_closures`` row closed by
    ``now`` (default: the current time)."""
    if organization_id is None:
        return False
    at = now or datetime.now(timezone.utc)
    return bool(await session.scalar(select(org_closed(organization_id, at))))


async def clear_closed_org_registrations(
    session: AsyncSession, now: datetime, organization_id: UUID | None = None
) -> int:
    """Delete the registration answers of closed organizations, at close.

    For every ``carrier_verifications`` row of an org in ``org_closures``
    (``closed_at`` reached; one org when ``organization_id`` is given):

    * drops ``REGISTRATION_ANSWER_REFS`` from ``provider_refs``;
    * clears ``rejection_reason`` (a carrier's text can quote the filing);
    * clears ``subject_name``, except on a live number verification: two
      nameless live ones would break the
      ``carrier_verifications_number_nameless_live_uniq`` index.

    Keeps the state, timestamps, carrier ids and the billing keys
    (``brand_reused``, the filed campaign ids). Safe to run again: a second
    run changes nothing. Commits. Returns how many rows changed. Never logs
    a value.
    """
    closed = closed_org_ids(now, organization_id)
    keys = cast(array(REGISTRATION_ANSWER_REFS), ARRAY(Text))
    v = CarrierVerification
    name_goes = or_(v.purpose == "sms", v.state.in_(("cancelled", "rejected")))
    result = await session.execute(
        update(v)
        .where(
            v.organization_id.in_(closed),
            or_(
                v.provider_refs.has_any(keys),
                v.rejection_reason.isnot(None),
                and_(v.subject_name.isnot(None), name_goes),
            ),
        )
        .values(
            provider_refs=v.provider_refs.op("-")(keys),
            rejection_reason=None,
            subject_name=case((name_goes, None), else_=v.subject_name),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount or 0


@dataclass
class PurgeSummary:
    organizations_purged: list[UUID] = field(default_factory=list)
    calls_scrubbed: int = 0
    sms_scrubbed: int = 0
    emails_scrubbed: int = 0
    contacts_deleted: int = 0
    evidence_files_deleted: int = 0
    verification_uploads_deleted: int = 0
    # Compatibility counter for older operator consumers. Audio is now optional.
    calls_with_unexpected_recording: int = 0


async def purge_expired_data(session: AsyncSession, now: datetime) -> PurgeSummary:
    """Scrub transcript/email content for orgs closed over 12 months ago.

    For every ``organization_id`` in ``org_closures`` with
    ``closed_at < now - 12 months``:

    * ``Call`` rows: delete optional audio, clear recording references and transcripts.
    * ``Sms`` rows: ``body`` is set to ``""`` (the column is NOT NULL; an
      empty string clears the content, same convention as
      ``Email.body_text`` below).
    * ``Email`` rows: ``body_text``, ``body_html``, and ``raw_s3_key``
      (the stored-content fields on ``Email`` — see
      ``hailhq.core.models.Email``) are cleared. ``body_text`` is set to
      ``""`` rather than ``NULL`` because the ``emails_body_required``
      CHECK constraint requires at least one of ``body_text``/
      ``body_html`` to be non-NULL; an empty string satisfies the
      constraint while still clearing the actual content.
    * SMS registration evidence files (public consent screenshots) and
      verification documents (``verification-uploads/``; the GC normally
      removes them within the hour) are deleted from the mail bucket, and
      the documents' ``file_uploads`` rows with them.
    * ``Contact`` rows scoped to the org are deleted outright — see module
      docstring; this is the explicit replacement for the FK cascade that
      isn't possible across the two databases.

    The row shell (ids, addresses, subject, status, timestamps) is left
    intact on the Call/Sms/Email tables for aggregate/audit purposes.
    ``audit_log`` is never touched (see module docstring).

    Commits internally — this is meant to be run as a standalone sweep,
    not composed mid-transaction with other work.
    """
    cutoff = now - RETENTION_PERIOD_AFTER_CLOSURE
    org_ids = [
        row[0]
        for row in (
            await session.execute(
                select(OrgClosure.organization_id).where(
                    OrgClosure.closed_at < cutoff,
                    OrgClosure.organization_id != SELF_HOSTED_ORG_ID,
                )
            )
        ).all()
    ]

    summary = PurgeSummary(organizations_purged=org_ids)
    if not org_ids:
        return summary

    # Audio has a shorter independent lifecycle. Erase any remaining objects
    # before scrubbing closed-account content; never leave a dangling public URL.
    recordings = (
        (
            await session.execute(
                select(Call).where(
                    Call.organization_id.in_(org_ids),
                    or_(
                        Call.recording_s3_key.isnot(None),
                        # In-flight recordings only carry the object key in
                        # metadata until Egress completes.
                        Call.metadata_.has_key("recording_object_key"),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for call in recordings:
        await delete_recording(call)

    call_result = await session.execute(
        update(Call)
        .where(
            Call.organization_id.in_(org_ids),
            Call.transcript.isnot(None),
        )
        .values(transcript=None)
        .execution_options(synchronize_session=False)
    )
    summary.calls_scrubbed = call_result.rowcount or 0

    sms_result = await session.execute(
        update(Sms)
        .where(
            Sms.organization_id.in_(org_ids),
            Sms.body != "",
        )
        .values(body="")
        .execution_options(synchronize_session=False)
    )
    summary.sms_scrubbed = sms_result.rowcount or 0

    email_result = await session.execute(
        update(Email)
        .where(
            Email.organization_id.in_(org_ids),
            (Email.body_text.isnot(None) & (Email.body_text != ""))
            | Email.body_html.isnot(None)
            | Email.raw_s3_key.isnot(None),
        )
        .values(body_text="", body_html=None, raw_s3_key=None)
        .execution_options(synchronize_session=False)
    )
    summary.emails_scrubbed = email_result.rowcount or 0

    contact_result = await session.execute(
        delete(Contact)
        .where(Contact.organization_id.in_(org_ids))
        .execution_options(synchronize_session=False)
    )
    summary.contacts_deleted = contact_result.rowcount or 0

    await session.execute(
        delete(FileUpload)
        .where(
            FileUpload.organization_id.in_(org_ids),
            FileUpload.purpose == "verification_document",
        )
        .execution_options(synchronize_session=False)
    )

    await session.commit()

    # Public links must stop working once the account's data is gone. After
    # the commit, so an S3 failure never blocks the database scrub; every
    # sweep retries it, since closed orgs stay past the cutoff. One org's
    # failure never stops the others.
    # Each folder in its own try, so one failure never skips the other.
    failed: set[UUID] = set()
    for org_id in org_ids:
        try:
            summary.evidence_files_deleted += await delete_org_evidence(org_id)
        except Exception:
            logger.exception("could not delete evidence for org %s", org_id)
            failed.add(org_id)
        try:
            summary.verification_uploads_deleted += (
                await delete_org_verification_uploads(org_id)
            )
        except Exception:
            logger.exception("could not delete verification uploads for org %s", org_id)
            failed.add(org_id)
    if failed:
        # The database scrub is committed; log what was done before failing.
        _log_summary(summary)
        raise RuntimeError(f"evidence or upload delete failed for {len(failed)} org(s)")
    return summary


def _log_summary(summary: PurgeSummary) -> None:
    logger.info(
        "retention sweep: %d org(s) past retention, %d call(s) scrubbed, "
        "%d sms scrubbed, %d email(s) scrubbed, %d contact(s) deleted, "
        "%d evidence file(s) deleted, %d verification upload(s) deleted, "
        "%d unexpected recording anomaly(ies)",
        len(summary.organizations_purged),
        summary.calls_scrubbed,
        summary.sms_scrubbed,
        summary.emails_scrubbed,
        summary.contacts_deleted,
        summary.evidence_files_deleted,
        summary.verification_uploads_deleted,
        summary.calls_with_unexpected_recording,
    )


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    from hailhq.core.db import session_scope

    async with session_scope() as session:
        summary = await purge_expired_data(session, datetime.now(timezone.utc))

    _log_summary(summary)


if __name__ == "__main__":
    asyncio.run(_main())
