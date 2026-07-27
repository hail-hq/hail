"""Background sweep deleting never-used outbound attachment uploads.

Uploads are reusable across sends (see EmailAttachmentUpload) so nothing
deletes them on use — only rows that were uploaded and never referenced
by any POST /emails within 24h are garbage. Same run_forever/tick shape
as OutboundForwardWorker (core/hailhq/core/outbound_worker.py), gated in
api/hailhq/api/main.py's lifespan on the mail bucket being configured.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from hailhq.core.models import EmailAttachmentUpload
from hailhq.core.s3_mail import S3MailClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

UNUSED_TTL = timedelta(hours=24)
GC_BATCH = 100

SessionFactory = Callable[[], "asynccontextmanager[AsyncSession]"]


class EmailAttachmentGcWorker:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        s3_factory: Callable[[], S3MailClient],
        poll_interval: float = 3600.0,
    ) -> None:
        self._session_factory = session_factory
        self._s3_factory = s3_factory
        self._s3: S3MailClient | None = None
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    def _get_s3(self) -> S3MailClient:
        if self._s3 is None:
            self._s3 = self._s3_factory()
        return self._s3

    async def run_forever(self) -> None:
        """Drive ``tick()`` until ``stop()`` is called."""
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # pragma: no cover — defensive; logged + retried
                logger.exception("email attachment GC tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> int:
        """Delete stale never-used uploads (S3 object + row). Returns count deleted.

        Each row's S3 delete and DB row-delete commit together, one row at a
        time — a crash between rows leaves already-processed rows fully gone
        from both S3 and the DB, and untouched rows intact for the next tick
        to retry (see ``OutboundForwardWorker``'s module docstring for the
        same "commit per row" rationale).

        Unlike that worker, GC isn't racing other GC replicas over who
        "claims" a row — a duplicate ``s3.delete`` and a duplicate
        ``DELETE ... WHERE id = x`` are both harmless no-ops — so there's no
        need for ``FOR UPDATE SKIP LOCKED`` or a re-query per row. The batch
        of stale candidates is listed once, up front, in its own short-lived
        session, then processed one row (its own S3 delete + DB delete +
        commit) at a time.
        """
        cutoff = datetime.now(timezone.utc) - UNUSED_TTL
        async with self._session_factory() as session:
            stmt = (
                select(EmailAttachmentUpload.id, EmailAttachmentUpload.s3_key)
                .where(EmailAttachmentUpload.first_used_at.is_(None))
                .where(EmailAttachmentUpload.created_at < cutoff)
                .order_by(EmailAttachmentUpload.created_at.asc())
                .limit(GC_BATCH)
            )
            candidates = (await session.execute(stmt)).all()
        if not candidates:
            return 0

        s3 = self._get_s3()
        deleted = 0
        for row_id, s3_key in candidates:
            try:
                await s3.delete(s3_key)
            except Exception:
                logger.warning(
                    "GC: failed to delete S3 object for upload_id=%s; "
                    "skipping row deletion this tick",
                    row_id,
                    exc_info=True,
                )
                continue
            async with self._session_factory() as session:
                await session.execute(
                    delete(EmailAttachmentUpload).where(
                        EmailAttachmentUpload.id == row_id
                    )
                )
                await session.commit()
            deleted += 1
        return deleted


__all__ = ["UNUSED_TTL", "EmailAttachmentGcWorker"]
