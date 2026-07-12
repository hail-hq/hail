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

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import EmailAttachmentUpload
from hailhq.core.s3_mail import S3MailClient

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
        """Delete stale never-used uploads (S3 object + row). Returns count deleted."""
        cutoff = datetime.now(timezone.utc) - UNUSED_TTL
        async with self._session_factory() as session:
            stmt = (
                select(EmailAttachmentUpload)
                .where(EmailAttachmentUpload.first_used_at.is_(None))
                .where(EmailAttachmentUpload.created_at < cutoff)
                .limit(GC_BATCH)
            )
            rows = (await session.execute(stmt)).scalars().all()
            if not rows:
                return 0
            s3 = self._get_s3()
            for row in rows:
                try:
                    await s3.delete(row.s3_key)
                except Exception:
                    logger.warning(
                        "GC: failed to delete S3 object for upload_id=%s; "
                        "skipping row deletion this tick",
                        row.id,
                        exc_info=True,
                    )
                    continue
                await session.execute(
                    delete(EmailAttachmentUpload).where(
                        EmailAttachmentUpload.id == row.id
                    )
                )
            await session.commit()
            return len(rows)


__all__ = ["EmailAttachmentGcWorker", "UNUSED_TTL"]
