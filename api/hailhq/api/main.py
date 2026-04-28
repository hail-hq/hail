from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from hailhq.core.db import dispose_engine
from hailhq.api.routes import calls as calls_routes
from hailhq.api.routes import events as events_routes


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Hold no resources on startup; dispose the DB engine on shutdown."""
    try:
        yield
    finally:
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
