"""Background sender for ingest-queued forward emails.

``enqueue_outbound_forward`` (api/) writes Email rows with
``status='queued'`` and ``metadata.forwarded_from`` set. This worker is
the only consumer: it claims those rows with ``FOR UPDATE SKIP LOCKED``,
re-attaches the inbound row's attachments from S3, sends via the
EmailProvider (headers ride the SESv2 Raw/Headers path), and marks each
row ``sent`` or ``failed``.

Scope guard: rows WITHOUT ``metadata.forwarded_from`` are direct
``POST /emails`` rows, sent synchronously inline by the route between
its own commit and status update — the filter below must never claim
them or mail double-sends.

Single attempt per row (no retry ladder, absent a crash between send
and commit — which re-queues at most the one in-flight row): a forward
is best-effort relay; the inbound row and raw MIME survive in S3 for
manual replay. Transient S3 fetch failures are the one exception — the
row stays queued (no send was attempted) and the next tick retries.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from hailhq.core.agent_caps import agent_outbound_halted
from hailhq.core.email_delivery_events import record_sent_event
from hailhq.core.models import Email, EmailAttachment
from hailhq.core.providers.email.base import EmailProvider, ProviderAttachment
from hailhq.core.s3_mail import S3MailClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

POLL_BATCH = 20

SessionFactory = Callable[[], "asynccontextmanager[AsyncSession]"]


class UsageCallback(Protocol):
    """Called once per delivered forward so the API layer can meter it.

    Core stays billing-semantics-agnostic — channel, units, and ledger ref are
    the api wiring's business; core only reports "this row was sent" and which
    org/row it was. The keyword-only signature is the actual contract, so a
    mismatched callback is a type error here rather than a runtime AttributeError.
    """

    async def __call__(
        self, *, organization_id: UUID, forward_email_id: UUID
    ) -> None: ...


def _is_missing_object_error(exc: Exception) -> bool:
    """True when S3 says the object is permanently gone (NoSuchKey/404)."""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in {"NoSuchKey", "404", "NotFound"}


class OutboundForwardWorker:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        provider_factory: Callable[[], EmailProvider],
        s3_factory: Callable[[], S3MailClient],
        usage_callback: UsageCallback | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._provider_factory = provider_factory
        self._s3_factory = s3_factory
        self._usage_callback = usage_callback
        self._provider: EmailProvider | None = None
        self._s3: S3MailClient | None = None
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    def _get_provider(self) -> EmailProvider:
        if self._provider is None:
            self._provider = self._provider_factory()
        return self._provider

    def _get_s3(self) -> S3MailClient:
        if self._s3 is None:
            self._s3 = self._s3_factory()
        return self._s3

    async def run_forever(self) -> None:
        """Drive ``tick()`` until ``stop()`` is called."""
        while not self._stop.is_set():
            try:
                processed = await self.tick()
            except Exception:  # pragma: no cover — defensive; logged + retried
                logger.exception("outbound forward worker tick failed")
                processed = 0
            if not processed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> int:
        """Send queued forwards, one row per transaction.

        Claiming a single row per transaction keeps the duplicate-send
        crash window to one in-flight send (irreducible without provider
        idempotency) and stays safe under multiple API replicas — a batch
        claim would release sibling row locks on each commit.
        """
        processed = 0
        while processed < POLL_BATCH:
            async with self._session_factory() as session:
                stmt = (
                    select(Email)
                    .where(Email.status == "queued")
                    .where(Email.direction == "outbound")
                    .where(Email.metadata_["forwarded_from"].astext.isnot(None))
                    .order_by(Email.created_at.asc())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return processed
                outcome = await self._send_one(session, row)
                # Read the ids we need to meter with before commit, so the meter
                # never depends on the row staying loaded post-commit (a session
                # factory with expire_on_commit would otherwise re-fetch them).
                forward_org_id = row.organization_id
                forward_email_id = row.id
                await session.commit()
            if outcome == "deferred":
                # Transient infra failure (S3) — stop the tick; retry next poll.
                return processed
            if outcome == "sent":
                # Meter the delivered forward as a billable outbound send. Only
                # after commit, so we never bill mail that didn't durably send.
                await self._meter_forward(forward_org_id, forward_email_id)
            processed += 1
        return processed

    async def _meter_forward(
        self, organization_id: UUID, forward_email_id: UUID
    ) -> None:
        """Report one delivered forward to the usage callback, best-effort.

        Metering must never break delivery: the row is already committed
        ``sent`` here, so a callback failure just drops one meter (logged) — it
        cannot re-send the mail or roll anything back.
        """
        if self._usage_callback is None:
            return
        try:
            await self._usage_callback(
                organization_id=organization_id,
                forward_email_id=forward_email_id,
            )
        except (
            Exception
        ):  # metering is best-effort; a dropped meter must not break delivery
            logger.warning(
                "forward usage meter failed for email_id=%s",
                forward_email_id,
                exc_info=True,
            )

    async def _send_one(self, session: AsyncSession, row: Email) -> str:
        """Attempt one forward. Returns ``"sent" | "failed" | "deferred"``.

        ``deferred`` means a transient infra failure before any send was
        attempted — the row is left ``queued`` for the next tick.
        """
        meta: dict[str, Any] = row.metadata_ or {}
        headers: dict[str, str] = meta.get("forward_headers") or {}
        now = datetime.now(timezone.utc)

        inbound_id = meta.get("forwarded_from")
        try:
            inbound_uuid = UUID(inbound_id) if inbound_id else None
        except (ValueError, TypeError) as exc:
            row.status = "failed"
            row.end_reason = type(exc).__name__
            row.failed_at = now
            return "failed"

        # Emergency kill switch covers forwarded mail too: an agent-origin org
        # whose outbound is disabled must not relay forwards on shared sender
        # domains. Defer (leave queued) rather than fail — forwards resume when
        # an operator clears the switch. (Per-workspace velocity caps do NOT
        # apply here: a forward is inbound-triggered, not agent-initiated.)
        if await agent_outbound_halted(session, row.organization_id):
            logger.info(
                "agent outbound kill switch on; deferring forward email_id=%s org=%s",
                row.id,
                row.organization_id,
            )
            return "deferred"

        try:
            attachments = await self._load_attachments(session, inbound_uuid)
        except Exception as exc:
            if _is_missing_object_error(exc):
                # The attachment object is gone for good — fail the row.
                row.status = "failed"
                row.end_reason = type(exc).__name__
                row.failed_at = now
                return "failed"
            # Transient infra failure (S3 down): leave the row queued and
            # let the next tick retry — no send was attempted.
            logger.warning(
                "attachment fetch failed for email_id=%s; deferring",
                row.id,
                exc_info=True,
            )
            return "deferred"

        try:
            result = await self._get_provider().send_email(
                from_address=row.from_address,
                to_addresses=row.to_addresses,
                subject=row.subject,
                body_text=row.body_text,
                body_html=row.body_html,
                cc=row.cc_addresses,
                bcc=row.bcc_addresses,
                reply_to=row.reply_to,
                headers=headers,
                attachments=attachments or None,
            )
        except Exception as exc:
            logger.warning("forward send failed for email_id=%s", row.id, exc_info=True)
            row.status = "failed"
            row.end_reason = type(exc).__name__
            row.failed_at = now
            return "failed"
        row.status = "sent"
        row.provider_message_id = result.provider_message_id
        row.sent_at = now
        record_sent_event(
            session,
            email_id=row.id,
            organization_id=row.organization_id,
            occurred_at=now,
        )
        return "sent"

    async def _load_attachments(
        self, session: AsyncSession, inbound_uuid: UUID | None
    ) -> list[ProviderAttachment]:
        if inbound_uuid is None:
            return []
        atts = (
            (
                await session.execute(
                    select(EmailAttachment).where(
                        EmailAttachment.email_id == inbound_uuid
                    )
                )
            )
            .scalars()
            .all()
        )
        out: list[ProviderAttachment] = []
        for att in atts:
            payload = await self._get_s3().fetch_raw(att.s3_key)
            out.append(
                ProviderAttachment(
                    filename=att.filename,
                    content_type=att.content_type,
                    payload=payload,
                )
            )
        return out


__all__ = ["OutboundForwardWorker"]
