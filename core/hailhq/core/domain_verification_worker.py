"""Background poller that drives pending custom email domains to a terminal
verification state.

On-demand verification (POST /email-domains/{id}/verify) still exists; this
worker just removes the need for the tenant to click it. Each tick re-polls
the provider for every pending custom row, flips it to verified when DKIM
lands, and fails it once it has been pending past the TTL (72h, matching
Resend) so a never-published domain doesn't poll forever.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from hailhq.core.dns_lookup import custom_dns_records
from hailhq.core.models import EmailDomain
from hailhq.core.providers.email.base import EmailProvider
from sqlalchemy import select, update

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 72 * 3600  # 72h


class DomainVerificationWorker:
    def __init__(
        self,
        *,
        session_factory,
        provider_factory: Callable[[], EmailProvider],
        verify_ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        poll_interval: float = 120.0,
    ) -> None:
        self._session_factory = session_factory
        self._provider_factory = provider_factory
        self._verify_ttl = timedelta(seconds=verify_ttl_seconds)
        self._poll_interval = poll_interval
        self._provider: EmailProvider | None = None
        self._stop = asyncio.Event()

    def _get_provider(self) -> EmailProvider:
        if self._provider is None:
            self._provider = self._provider_factory()
        return self._provider

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # pragma: no cover — defensive; logged + retried
                logger.exception("domain verification worker tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> int:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(EmailDomain)
                        .where(EmailDomain.kind == "custom")
                        .where(EmailDomain.verification_status == "pending")
                    )
                )
                .scalars()
                .all()
            )

        processed = 0
        now = datetime.now(timezone.utc)
        # Poll each provider identity without holding a DB connection, then
        # apply every update in a single session at the end.
        updates: list[tuple[object, dict]] = []
        for row in rows:
            processed += 1
            try:
                identity = await self._get_provider().get_identity(row.domain)
            except Exception:
                logger.warning(
                    "get_identity failed for domain=%s", row.domain, exc_info=True
                )
                continue

            if identity.verification_status == "verified":
                # Same as the POST /verify endpoint: include the inbound-receipt
                # MX in the records and turn receiving on automatically.
                values = {
                    "verification_status": "verified",
                    "verified_at": now,
                    "dns_records": custom_dns_records(
                        row.domain, identity.dkim_records
                    ),
                    "mail_from_status": identity.mail_from_status,
                    "inbound_enabled": True,
                }
            elif identity.verification_status == "failed" or self._is_past_ttl(
                row.created_at, now
            ):
                values = {"verification_status": "failed"}
            else:
                values = {"mail_from_status": identity.mail_from_status}

            updates.append((row.id, values))

        if updates:
            async with self._session_factory() as session:
                for row_id, values in updates:
                    await session.execute(
                        update(EmailDomain)
                        .where(EmailDomain.id == row_id)
                        # Re-check pending so a concurrent verify isn't clobbered.
                        .where(EmailDomain.verification_status == "pending")
                        .values(**values)
                    )
                await session.commit()
        return processed

    def _is_past_ttl(self, created_at: datetime, now: datetime) -> bool:
        created = created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return now - created > self._verify_ttl


__all__ = ["DomainVerificationWorker"]
