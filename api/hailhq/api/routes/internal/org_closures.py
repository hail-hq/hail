"""POST /internal/org-closures — hail-website → API.

hail-website must call this endpoint when an account is closed/deleted,
so hail's own retention sweep (``hailhq.core.retention.purge_expired_data``)
can find orgs past the account-duration + 12-months retention window —
hail's DB otherwise has no way to know an org closed at all (see
``hailhq.core.models.OrgClosure``). That call is a separate change in the
hail-website repo, not built here — this is only the receiving endpoint.

At close it also deletes the org's US texting registration answers
(``hailhq.core.retention.clear_closed_org_registrations``). The
verification background pass runs the same delete for every closed org,
so a failure here is retried there.

Idempotent: re-notifying the same ``organization_id`` (e.g. a retry, or a
corrected ``closed_at``) updates the existing row rather than erroring.

Shared-secret HMAC auth, same as the rest of ``routes/internal/`` — see
``routes/internal/auth.py``. Not in the public OpenAPI spec: operator
infrastructure → API, like ``ses_events.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.core.db import get_session
from hailhq.core.models import OrgClosure
from hailhq.core.retention import clear_closed_org_registrations
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)


class OrgClosureIn(BaseModel):
    organization_id: UUID
    closed_at: datetime
    source: str = "hail_website"


@router.post("/org-closures")
async def record_org_closure(
    body: OrgClosureIn,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    stmt = (
        pg_insert(OrgClosure)
        .values(
            organization_id=body.organization_id,
            closed_at=body.closed_at,
            source=body.source,
        )
        .on_conflict_do_update(
            index_elements=["organization_id"],
            set_={"closed_at": body.closed_at, "source": body.source},
        )
    )
    await db.execute(stmt)
    await db.commit()
    # Not part of recording the closure: the background pass retries it.
    try:
        cleared = await clear_closed_org_registrations(
            db, datetime.now(timezone.utc), body.organization_id
        )
        logger.info("org closure: %d verification(s) cleared", cleared)
    except Exception:
        await db.rollback()
        logger.exception("org closure: could not clear verification answers")
    return {
        "organization_id": str(body.organization_id),
        "closed_at": body.closed_at.isoformat(),
        "source": body.source,
    }
