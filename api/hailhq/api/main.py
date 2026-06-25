from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from functools import partial

from hailhq.core.config import settings
from hailhq.core.db import dispose_engine, session_scope
from hailhq.core.http_post import httpx_post
from hailhq.core.domain_verification_worker import DomainVerificationWorker
from hailhq.core.outbound_worker import OutboundForwardWorker
from hailhq.core.pool import sweep_pool_reservations
from hailhq.core.providers.email.ses import SesEmailProvider
from hailhq.core.reconcile import sweep_stale_calls
from hailhq.core.s3_inbound import S3InboundClient
from hailhq.core.secret_cipher import SecretCipher, SecretKeyMissing
from hailhq.core.webhook_worker import WebhookWorker
from hailhq.api.routes import calls as calls_routes
from hailhq.api.routes import emails as emails_routes
from hailhq.api.routes import events as events_routes
from hailhq.api.routes import email_domains as email_domains_routes
from hailhq.api.routes import webhooks as webhooks_routes
from hailhq.api.routes.internal import ses_events as internal_ses_events
from hailhq.api.usage import write_usage_event

logger = logging.getLogger(__name__)


async def _meter_forward_send(*, organization_id, forward_email_id) -> None:
    """Meter one delivered forward as a billable outbound send.

    A forward is an outbound message and bills like one: flat 1 unit on the
    ``email`` channel, same as ``POST /emails`` and the inbound meter. The
    ``email-forward:`` ref prefix keeps it distinct from the inbound
    ``email:{id}`` event for the same conversation in the ledger.
    """
    await write_usage_event(
        organization_id=organization_id,
        channel="email",
        units=1,
        ref=f"email-forward:{forward_email_id}",
    )


# How often the backstop sweepers run. Both the call reconciler and the pool
# release first happen on the hot path (the voicebot's on_call_end, ~immediate);
# these sweeps are purely the backstop for stuck rows, so 60s is fine —
# worst-case extra delay after a missed hot-path write is `interval + sweep
# query time`.
SWEEP_INTERVAL_SECONDS = 60


async def _backstop_sweeper_loop() -> None:
    """Forever-loop running the call reconciler + pool-reservation sweeper.

    Order matters: :func:`sweep_stale_calls` first force-closes calls stuck in
    a non-terminal status, so :func:`sweep_pool_reservations` sees them as
    terminal and releases their numbers in the same tick. Both share one
    transaction.

    Survives transient DB blips by logging and continuing — a sweep that fails
    once retries on the next tick, and a missed sweep just delays backstop
    cleanup by ``SWEEP_INTERVAL_SECONDS``. CancelledError propagates so the
    lifespan shutdown can stop the loop cleanly.
    """
    grace = settings.hail_pool_release_grace_seconds
    while True:
        try:
            async with session_scope() as session:
                stale_calls = await sweep_stale_calls(session, grace_seconds=grace)
                released = await sweep_pool_reservations(session, grace_seconds=grace)
                await session.commit()
            if stale_calls:
                logger.warning(
                    "call reconciler force-closed %d stale call(s): %s",
                    len(stale_calls),
                    stale_calls,
                )
            if released:
                logger.warning(
                    "pool sweeper force-released %d reservation(s): %s",
                    len(released),
                    released,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover — defensive; logged + retried
            logger.exception("backstop sweeper iteration failed; will retry")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


async def _stop_worker(worker, task: asyncio.Task) -> None:
    """Graceful-stop a polling worker task, hard-cancelling after 5s."""
    await worker.stop()
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start backstop sweepers + webhook worker on boot; tear them down on shutdown."""
    sweeper_task = asyncio.create_task(
        _backstop_sweeper_loop(), name="backstop-sweeper"
    )

    webhook_worker: WebhookWorker | None = None
    webhook_task: asyncio.Task | None = None
    try:
        cipher = SecretCipher(settings.hail_webhook_secret_key)
    except SecretKeyMissing:
        logger.warning(
            "HAIL_WEBHOOK_SECRET_KEY is unset or invalid; webhook delivery "
            "worker disabled and webhook routes will return 503. Generate a "
            'key with: python -c "from hailhq.core.secret_cipher import '
            'generate_key; print(generate_key())"'
        )
    else:
        webhook_worker = WebhookWorker(
            session_factory=session_scope,
            http_post=partial(
                httpx_post,
                allow_private_networks=settings.hail_webhook_allow_private_networks,
            ),
            decrypt=cipher.decrypt,
        )
        webhook_task = asyncio.create_task(
            webhook_worker.run_forever(), name="webhook-worker"
        )

    # Forward sender: drains the status='queued' + metadata.forwarded_from
    # rows that inbound ingest enqueues. Only useful (and only configured)
    # when inbound is on — direct POST /emails sends are inline in the route.
    forward_worker: OutboundForwardWorker | None = None
    forward_task: asyncio.Task | None = None
    if settings.hail_inbound_enabled and settings.hail_inbound_bucket:
        forward_worker = OutboundForwardWorker(
            session_factory=session_scope,
            provider_factory=SesEmailProvider,
            s3_factory=lambda: S3InboundClient(bucket=settings.hail_inbound_bucket),
            usage_callback=_meter_forward_send,
        )
        forward_task = asyncio.create_task(
            forward_worker.run_forever(), name="outbound-forward-worker"
        )

    verify_worker: DomainVerificationWorker | None = None
    verify_task: asyncio.Task | None = None
    if settings.hail_domain_verify_poll_seconds > 0:
        verify_worker = DomainVerificationWorker(
            session_factory=session_scope,
            provider_factory=SesEmailProvider,
            poll_interval=settings.hail_domain_verify_poll_seconds,
        )
        verify_task = asyncio.create_task(
            verify_worker.run_forever(), name="domain-verification-worker"
        )

    try:
        yield
    finally:
        sweeper_task.cancel()
        try:
            await sweeper_task
        except (asyncio.CancelledError, Exception):
            pass
        if webhook_worker is not None and webhook_task is not None:
            await _stop_worker(webhook_worker, webhook_task)
        if forward_worker is not None and forward_task is not None:
            await _stop_worker(forward_worker, forward_task)
        if verify_worker is not None and verify_task is not None:
            await _stop_worker(verify_worker, verify_task)
        await calls_routes.close_livekit_singleton()
        await dispose_engine()


app = FastAPI(
    title="Hail",
    version="0.1.0",
    description=(
        "Universal communication platform for AI agents.\n\n"
        "This file is the source of truth for the Go CLI. Regenerate it after\n"
        "changing API routes — see docs/contributing.md.\n"
    ),
    lifespan=lifespan,
)


@app.exception_handler(SecretKeyMissing)
async def _secret_key_missing(_request: Request, exc: SecretKeyMissing) -> JSONResponse:
    """Webhook routes need HAIL_WEBHOOK_SECRET_KEY; without it they 503, not 500."""
    return JSONResponse(
        status_code=503,
        content={
            "detail": "webhooks unavailable: server is missing HAIL_WEBHOOK_SECRET_KEY"
        },
    )


app.include_router(calls_routes.router)
app.include_router(emails_routes.router)
app.include_router(events_routes.router)
app.include_router(email_domains_routes.router)
app.include_router(webhooks_routes.router)
app.include_router(internal_ses_events.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
