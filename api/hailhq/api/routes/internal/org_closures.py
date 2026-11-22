"""POST /internal/org-closures — hail-website → API.

hail-website's account deletion inserts the ``org_closures`` row inside its
own deletion transaction (hail-website and hail share the same database),
then calls this endpoint, which upserts the same row (see
``hailhq.core.models.OrgClosure``). hail's retention sweep
(``hailhq.core.retention.purge_expired_data``) reads it later.

After the response, for that org, it releases the numbers and cancels the
carrier registrations (``hailhq.api.org_close.close_org_carrier_items``),
then deletes the registration answers
(``hailhq.core.retention.clear_closed_org_registrations``) and the files a
number registration kept for a resubmit
(``hailhq.api.routes.verifications.forget_kept_registrations``), and the
knowledge base (``hailhq.core.knowledge.access.delete_org_knowledge``; the
knowledge ingest worker retries it while rows remain). The carrier
calls can take longer than the caller waits, so they run after the
response. The verification worker runs both for every closed org on each
pass, so a failure here is retried there, and a closure row with no call is
handled too.

Idempotent: re-notifying the same ``organization_id`` (e.g. a retry, or a
corrected ``closed_at``) updates the existing row rather than erroring.

Shared-secret HMAC auth, same as the rest of ``routes/internal/`` — see
``routes/internal/auth.py``. Not in the public OpenAPI spec: operator
infrastructure → API, like ``ses_events.py``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends
from hailhq.api.org_close import close_org_carrier_items
from hailhq.api.routes.calls import get_livekit_optional
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.api.routes.verifications import forget_kept_registrations
from hailhq.core.config import settings
from hailhq.core.db import background_context, get_session, session_scope
from hailhq.core.knowledge.access import delete_org_knowledge
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import OrgClosure
from hailhq.core.providers.verification import get_verification_provider
from hailhq.core.retention import clear_closed_org_registrations, closed_org_ids
from hailhq.core.s3_mail import S3MailClient
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


async def close_org(organization_id: UUID, lk: LiveKitClient | None) -> None:
    """Release the org's numbers, cancel its registrations, then delete its
    texting answers, in a fresh session. Never raises: the verification
    worker retries whatever is left."""
    now = datetime.now(timezone.utc)
    async with session_scope() as db:
        try:
            await close_org_carrier_items(
                db, now, get_verification_provider, lk, organization_id
            )
        except Exception:
            await db.rollback()
            logger.exception("org closure: carrier close step failed")
        try:
            cleared = await clear_closed_org_registrations(db, now, organization_id)
            logger.info("org closure: %d verification(s) cleared", cleared)
        except Exception:
            await db.rollback()
            logger.exception("org closure: could not clear verification answers")
        try:
            _, files = await forget_kept_registrations(
                db, now, organization_id=organization_id
            )
            logger.info("org closure: %d kept registration file(s) deleted", files)
        except Exception:
            await db.rollback()
            logger.exception("org closure: could not delete kept registration files")
        try:
            store = (
                S3MailClient(bucket=settings.hail_mail_bucket)
                if settings.hail_mail_bucket
                else None
            )
            # Same rule as the steps above: closed by now, never the
            # self-hosted org. The ingest worker deletes it later otherwise.
            if await db.scalar(closed_org_ids(now, organization_id).exists().select()):
                documents = await delete_org_knowledge(db, organization_id, store)
                logger.info("org closure: %d knowledge document(s) deleted", documents)
        except Exception:
            await db.rollback()
            logger.exception("org closure: could not delete the knowledge base")


async def _close_after_response(
    organization_id: UUID, lk: LiveKitClient | None
) -> None:
    # On the background pool: the carrier calls hold a session for seconds.
    await asyncio.create_task(
        close_org(organization_id, lk), context=background_context()
    )


@router.post("/org-closures")
async def record_org_closure(
    body: OrgClosureIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    background: BackgroundTasks,
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)] = None,
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
    background.add_task(_close_after_response, body.organization_id, lk)
    return {
        "organization_id": str(body.organization_id),
        "closed_at": body.closed_at.isoformat(),
        "source": body.source,
    }
