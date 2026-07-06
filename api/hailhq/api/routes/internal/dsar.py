"""POST /internal/dsar/{lookup,export,delete} — operator DSAR tooling.

Minimal internal surface over ``hailhq.core.dsar`` so a data-subject
access/erasure request can be handled without shelling into a Python REPL
in production. No admin UI — this is tooling, not a dashboard.

Shared-secret HMAC auth, same as the rest of ``routes/internal/`` — see
``routes/internal/auth.py``. Not in the public OpenAPI spec.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.core.dsar import delete_recipient_data, export_recipient_data
from hailhq.core.db import get_session

router = APIRouter(
    prefix="/internal/dsar",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)


class DSARIdentifierIn(BaseModel):
    identifier: str


@router.post("/lookup")
async def dsar_lookup(
    body: DSARIdentifierIn,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Same underlying lookup as ``/export`` — over HTTP both necessarily
    return the same JSON-able shape; ``lookup_recipient`` vs.
    ``export_recipient_data`` only differ for in-process Python callers
    (raw ORM rows vs. a serialized dict)."""
    return await export_recipient_data(db, body.identifier)


@router.post("/export")
async def dsar_export(
    body: DSARIdentifierIn,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    return await export_recipient_data(db, body.identifier)


@router.post("/delete")
async def dsar_delete(
    body: DSARIdentifierIn,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    summary = await delete_recipient_data(db, body.identifier)
    return {
        "identifier": summary.identifier,
        "calls_scrubbed": summary.calls_scrubbed,
        "emails_scrubbed": summary.emails_scrubbed,
        "suppressions_preserved": summary.suppressions_preserved,
    }
