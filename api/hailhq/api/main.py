from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from hailhq.core.config import settings
from hailhq.core.db import dispose_engine, session_scope
from hailhq.core.pool import sweep_pool_reservations
from hailhq.api.routes import calls as calls_routes
from hailhq.api.routes import events as events_routes

logger = logging.getLogger(__name__)

# How often the pool sweeper runs. Pool reservations are first released by
# the API/voicebot on terminal status (hot path, ~immediate); the sweeper
# is purely a backstop for stuck rows, so 60s is fine — worst-case extra
# lock time after a missed release is `interval + sweep query time`.
POOL_SWEEP_INTERVAL_SECONDS = 60


async def _pool_sweeper_loop() -> None:
    """Forever-loop that force-releases stuck pool reservations.

    Survives transient DB blips by logging and continuing — a sweep that
    fails once will retry on the next tick, and a missed sweep just delays
    backstop release by ``POOL_SWEEP_INTERVAL_SECONDS``. CancelledError
    propagates so the lifespan shutdown can stop the loop cleanly.
    """
    grace = settings.hail_pool_release_grace_seconds
    while True:
        try:
            async with session_scope() as session:
                released = await sweep_pool_reservations(session, grace_seconds=grace)
                await session.commit()
            if released:
                logger.warning(
                    "pool sweeper force-released %d reservation(s): %s",
                    len(released),
                    released,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover — defensive; logged + retried
            logger.exception("pool sweeper iteration failed; will retry")
        await asyncio.sleep(POOL_SWEEP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start the pool sweeper on boot; release DB engine + LiveKit on shutdown."""
    sweeper_task = asyncio.create_task(_pool_sweeper_loop(), name="pool-sweeper")
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
app.include_router(events_routes.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
