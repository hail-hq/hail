"""Post-account-closure retention sweep.

Founder policy: transcripts and stored email content are retained for the
account's duration + 12 months after account closure. hail's own database
does not own account/org lifecycle state (organizations/accounts live in
the separate hail-website repo's Postgres, better-auth schema), so it
cannot on its own tell whether an org is closed or when — that's what
``org_closures`` (see ``hailhq.core.models.OrgClosure``) exists to record.
hail-website calls ``POST /internal/org-closures`` on account close/delete
to populate it (see
``api/hailhq/api/routes/internal/org_closures.py`` for the receiving
endpoint and integration note — this module does not make that call).

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

from hailhq.core.models import Call, Contact, Email, OrgClosure, Sms
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

__all__ = ["RETENTION_PERIOD_AFTER_CLOSURE", "PurgeSummary", "purge_expired_data"]

# "12 months" approximated as a fixed day-count. This is a compliance
# boundary, not a billing-precision figure, so a plain timedelta avoids
# pulling in a calendar-math dependency (e.g. dateutil.relativedelta) for
# one constant.
RETENTION_PERIOD_AFTER_CLOSURE = timedelta(days=365)


@dataclass
class PurgeSummary:
    organizations_purged: list[UUID] = field(default_factory=list)
    calls_scrubbed: int = 0
    sms_scrubbed: int = 0
    emails_scrubbed: int = 0
    contacts_deleted: int = 0
    # Rows that should already satisfy the transcript-only storage
    # guarantee (voicebot never persists audio) but didn't — logged as a
    # warning, not silently overwritten, since clearing them wasn't asked
    # for and their presence signals a bug elsewhere worth investigating.
    calls_with_unexpected_recording: int = 0


async def purge_expired_data(session: AsyncSession, now: datetime) -> PurgeSummary:
    """Scrub transcript/email content for orgs closed over 12 months ago.

    For every ``organization_id`` in ``org_closures`` with
    ``closed_at < now - 12 months``:

    * ``Call`` rows: ``transcript`` is set to ``NULL``. ``recording_s3_key``
      / ``recording_duration_ms`` are expected to already be ``NULL`` per
      the transcript-only storage guarantee — this function checks that
      and logs a warning if it doesn't hold, it does not mutate those
      columns (clearing them wasn't part of the ask, and if they're
      unexpectedly populated that's a separate bug to chase down, not
      paper over here).
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
                select(OrgClosure.organization_id).where(OrgClosure.closed_at < cutoff)
            )
        ).all()
    ]

    summary = PurgeSummary(organizations_purged=org_ids)
    if not org_ids:
        return summary

    anomaly_count = (
        await session.execute(
            select(func.count())
            .select_from(Call)
            .where(
                Call.organization_id.in_(org_ids),
                Call.recording_s3_key.isnot(None),
            )
        )
    ).scalar_one()
    summary.calls_with_unexpected_recording = anomaly_count
    if anomaly_count:
        logger.warning(
            "retention: %d Call row(s) for closed orgs unexpectedly carry "
            "recording_s3_key (transcript-only storage guarantee violated)",
            anomaly_count,
        )

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

    await session.commit()
    return summary


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    from hailhq.core.db import session_scope

    async with session_scope() as session:
        summary = await purge_expired_data(session, datetime.now(timezone.utc))

    logger.info(
        "retention sweep: %d org(s) past retention, %d call(s) scrubbed, "
        "%d sms scrubbed, %d email(s) scrubbed, %d contact(s) deleted, "
        "%d unexpected recording anomaly(ies)",
        len(summary.organizations_purged),
        summary.calls_scrubbed,
        summary.sms_scrubbed,
        summary.emails_scrubbed,
        summary.contacts_deleted,
        summary.calls_with_unexpected_recording,
    )


if __name__ == "__main__":
    asyncio.run(_main())
