from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from hailhq.core.config import settings
from hailhq.core.db import dispose_engine, session_scope
from hailhq.core.pool import sweep_pool_reservations
from hailhq.core.reconcile import sweep_stale_calls
from hailhq.api.routes import calls as calls_routes
from hailhq.api.routes import emails as emails_routes
from hailhq.api.routes import events as events_routes
from hailhq.api.routes import sender_domains as sender_domains_routes

logger = logging.getLogger(__name__)

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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start the backstop sweepers on boot; release DB engine + LiveKit on shutdown."""
    sweeper_task = asyncio.create_task(
        _backstop_sweeper_loop(), name="backstop-sweeper"
    )
    try:
        yield
    finally:
        sweeper_task.cancel()
        try:
            await sweeper_task
        except (asyncio.CancelledError, Exception):
            pass
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
app.include_router(calls_routes.router)
app.include_router(emails_routes.router)
app.include_router(events_routes.router)
app.include_router(sender_domains_routes.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
