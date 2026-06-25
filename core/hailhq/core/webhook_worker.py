"""Background webhook delivery worker.

A single asyncio task in the API service polls pending deliveries,
claims them with ``SELECT ... FOR UPDATE SKIP LOCKED``, POSTs via the
injected ``http_post`` callable, and updates each row's status. After
the last retry slot, a delivery is marked ``dead`` and its owning
subscription's ``consecutive_failures`` counter ticks — at 50 the
subscription auto-disables.

The worker doesn't mint payloads — Phase 8's fan-out service writes
``webhook_deliveries`` rows with payload + target already chosen. The
worker only delivers.

Secrets aren't stored as plaintext in the DB. They're Fernet-encrypted
at rest (see ``hailhq.core.secret_cipher``); the worker reads the
ciphertext off the subscription/domain row and decrypts it via the
injected ``decrypt`` callable on each delivery. This survives restarts
and works across processes — no in-process cache to go cold.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import WebhookDelivery, WebhookSubscription
from hailhq.core.webhooks import build_event_payload, next_attempt_delay, sign_payload

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 50
RESPONSE_BODY_CAP = 4096
DEFAULT_CONCURRENCY = 32
POLL_BATCH = 100

HttpPostFn = Callable[[str, bytes, dict[str, str]], Awaitable[tuple[int, str]]]
SessionFactory = Callable[[], "asynccontextmanager[AsyncSession]"]


def _next_delivery_state(
    delivery: WebhookDelivery, *, ok: bool
) -> tuple[str, datetime | None, int]:
    """Compute the new (status, next_attempt_at, attempt) for a delivery row."""
    attempt = delivery.attempt + 1
    if ok:
        return "succeeded", None, attempt
    delay = next_attempt_delay(delivery.attempt + 1)
    if delay is None:
        return "dead", None, attempt
    return (
        "pending",
        datetime.now(timezone.utc) + timedelta(seconds=delay),
        attempt,
    )


class WebhookWorker:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        http_post: HttpPostFn,
        decrypt: Callable[[str], str],
        concurrency: int = DEFAULT_CONCURRENCY,
        poll_interval: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._http_post = http_post
        self._decrypt = decrypt
        self._sem = asyncio.Semaphore(concurrency)
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        """Drive ``tick()`` until ``stop()`` is called."""
        while not self._stop.is_set():
            try:
                processed = await self.tick()
            except Exception:  # pragma: no cover
                logger.exception("webhook worker tick failed")
                processed = 0
            if not processed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

    async def tick(self) -> int:
        """Claim and dispatch one batch. Returns the number claimed."""
        async with self._session_factory() as session:
            claimed = await self._claim_batch(session)
        if not claimed:
            return 0
        tasks: list[asyncio.Task] = []
        for delivery_id in claimed:
            await self._sem.acquire()
            tasks.append(asyncio.create_task(self._deliver(delivery_id)))
        await asyncio.gather(*tasks, return_exceptions=True)
        return len(claimed)

    async def stop(self) -> None:
        self._stop.set()

    async def _claim_batch(self, db: AsyncSession) -> list[UUID]:
        stmt = (
            select(WebhookDelivery)
            .where(WebhookDelivery.status == "pending")
            .where(WebhookDelivery.next_attempt_at <= datetime.now(timezone.utc))
            .order_by(WebhookDelivery.next_attempt_at.asc())
            .limit(POLL_BATCH)
            .with_for_update(skip_locked=True)
        )
        rows = list((await db.execute(stmt)).scalars().all())
        if not rows:
            return []
        # Defer next_attempt_at so other workers don't re-claim while
        # we're POSTing. SKIP LOCKED handles within-tx contention; this
        # handles cross-tx.
        deferred = datetime.now(timezone.utc) + timedelta(minutes=10)
        ids = [r.id for r in rows]
        for rid in ids:
            await db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == rid)
                .values(next_attempt_at=deferred)
            )
        await db.commit()
        return ids

    async def _deliver(self, delivery_id: UUID) -> None:
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
                    )
                ).scalar_one()

                resolved = await self._resolve_target_url(session, row)
                if resolved is None:
                    await self._record_failure(session, row, None, "no target url")
                    return
                target_url, secret_encrypted = resolved

                try:
                    secret = self._decrypt(secret_encrypted)
                except Exception:
                    await self._record_failure(
                        session, row, None, "secret decrypt failed"
                    )
                    return

                try:
                    org_id = row.payload["organization_id"]
                    event_data = row.payload["data"]
                except (KeyError, TypeError):
                    await self._record_failure(session, row, None, "malformed payload")
                    return
                body = build_event_payload(
                    delivery_id=row.id,
                    event_type=row.event_type,
                    organization_id=org_id,
                    data=event_data,
                    created_at=row.created_at,
                )
                sig = sign_payload(body, secret)
                headers = {
                    "Content-Type": "application/json",
                    "X-Hail-Signature": sig,
                    "X-Hail-Event": row.event_type,
                    "X-Hail-Delivery": str(row.id),
                }
                if row.subscription_id:
                    headers["X-Hail-Subscription"] = str(row.subscription_id)
                if row.email_domain_id:
                    headers["X-Hail-Email-Domain"] = str(row.email_domain_id)

                try:
                    status, resp_body = await self._http_post(target_url, body, headers)
                except Exception as exc:  # pragma: no cover
                    await self._record_failure(
                        session, row, None, str(exc)[:RESPONSE_BODY_CAP]
                    )
                    return

                ok = 200 <= status < 300
                if ok:
                    await self._record_success(session, row, status, resp_body)
                else:
                    await self._record_failure(
                        session, row, status, (resp_body or "")[:RESPONSE_BODY_CAP]
                    )
        finally:
            self._sem.release()

    async def _resolve_target_url(
        self, db: AsyncSession, row: WebhookDelivery
    ) -> tuple[str, str] | None:
        """Return (target_url, secret_encrypted) or None if undeliverable."""
        if row.subscription_id:
            sub = (
                await db.execute(
                    select(WebhookSubscription).where(
                        WebhookSubscription.id == row.subscription_id
                    )
                )
            ).scalar_one_or_none()
            if sub is None or sub.status != "active":
                return None
            return sub.target_url, sub.secret_encrypted
        return None

    async def _record_success(
        self,
        db: AsyncSession,
        row: WebhookDelivery,
        status_code: int,
        body: str,
    ) -> None:
        new_status, _, attempt = _next_delivery_state(row, ok=True)
        await db.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id == row.id)
            .values(
                status=new_status,
                attempt=attempt,
                response_status=status_code,
                response_body=(body or "")[:RESPONSE_BODY_CAP],
                succeeded_at=datetime.now(timezone.utc),
            )
        )
        if row.subscription_id is not None:
            await db.execute(
                update(WebhookSubscription)
                .where(WebhookSubscription.id == row.subscription_id)
                .values(
                    consecutive_failures=0,
                    last_success_at=datetime.now(timezone.utc),
                )
            )
        await db.commit()

    async def _record_failure(
        self,
        db: AsyncSession,
        row: WebhookDelivery,
        status_code: int | None,
        body: str,
    ) -> None:
        new_status, next_at, attempt = _next_delivery_state(row, ok=False)
        values: dict[str, Any] = {
            "status": new_status,
            "attempt": attempt,
            "response_status": status_code,
            "response_body": body,
        }
        if next_at is not None:
            values["next_attempt_at"] = next_at
        await db.execute(
            update(WebhookDelivery).where(WebhookDelivery.id == row.id).values(**values)
        )
        if row.subscription_id is not None:
            await self._update_subscription_on_failure(
                db, row.subscription_id, terminal=(new_status == "dead")
            )
        await db.commit()

    async def _update_subscription_on_failure(
        self, db: AsyncSession, sub_id: UUID, *, terminal: bool
    ) -> None:
        now = datetime.now(timezone.utc)
        if not terminal:
            await db.execute(
                update(WebhookSubscription)
                .where(WebhookSubscription.id == sub_id)
                .values(last_failure_at=now)
            )
            return
        sub = (
            await db.execute(
                select(WebhookSubscription).where(WebhookSubscription.id == sub_id)
            )
        ).scalar_one()
        new_failures = sub.consecutive_failures + 1
        values: dict[str, Any] = {
            "consecutive_failures": new_failures,
            "last_failure_at": now,
        }
        if new_failures >= MAX_CONSECUTIVE_FAILURES:
            values["status"] = "disabled"
        await db.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == sub_id)
            .values(**values)
        )


__all__ = ["WebhookWorker", "_next_delivery_state"]
