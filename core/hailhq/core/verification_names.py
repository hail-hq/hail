"""Operator backfill: fill ``carrier_verifications.subject_name`` from the
carrier for rows made before Hail kept the name.

Run once after the migration that adds the column, on the host that runs
the API (see docs/operations/carrier-verification.md):

    python -m hailhq.core.verification_names

Every row with no name is asked, whatever its state, so cancelled and
rejected rows get a name too when the carrier still has the record. Each
plug-in reads the name from its own record (``fetch_subject_name``). A row
whose carrier has no plug-in configured, whose record is gone, or whose
record holds no name keeps null. The script only logs counts and row ids,
never a name.

A live number row whose name would equal another live row of the same kind
(the unique index ``carrier_verifications_number_live_uniq``) keeps null,
and is counted as a duplicate.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from hailhq.core.models import CarrierVerification
from hailhq.core.providers.verification import (
    VerificationProvider,
    get_verification_provider,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# (carrier, purpose) -> plug-in, or None when it is not configured.
Registry = Callable[[str, str], VerificationProvider | None]


@dataclass
class BackfillSummary:
    named: int = 0
    no_name: int = 0
    no_plugin: int = 0
    failed: int = 0
    duplicate: int = 0


async def backfill_subject_names(
    session: AsyncSession, registry: Registry = get_verification_provider
) -> BackfillSummary:
    """Ask the carrier for the name of every row that has none. Commits
    after each row, so one failure does not undo the others."""
    summary = BackfillSummary()
    rows = (
        await session.execute(
            select(
                CarrierVerification.id,
                CarrierVerification.provider,
                CarrierVerification.purpose,
                CarrierVerification.subject_type,
                CarrierVerification.provider_refs,
            )
            .where(CarrierVerification.subject_name.is_(None))
            .order_by(CarrierVerification.created_at)
        )
    ).all()
    # The read transaction must not stay open through the carrier calls.
    await session.commit()
    for row in rows:
        plugin = registry(row.provider, row.purpose)
        if plugin is None:
            summary.no_plugin += 1
            continue
        try:
            name = await plugin.fetch_subject_name(
                row.provider_refs or {}, row.subject_type  # type: ignore[arg-type]
            )
        except Exception as exc:
            # The exception text can quote the carrier's payload: log its
            # type only.
            logger.warning(
                "verification name lookup failed: id=%s provider=%s error=%s",
                row.id,
                row.provider,
                type(exc).__name__,
            )
            summary.failed += 1
            continue
        if name is None:
            summary.no_name += 1
            continue
        try:
            await session.execute(
                update(CarrierVerification)
                .where(
                    CarrierVerification.id == row.id,
                    CarrierVerification.subject_name.is_(None),
                )
                .values(subject_name=name)
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            logger.warning("verification name duplicates a live row: id=%s", row.id)
            summary.duplicate += 1
            continue
        summary.named += 1
    return summary


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    from hailhq.core.db import session_scope

    async with session_scope() as session:
        summary = await backfill_subject_names(session)
    logger.info(
        "verification names: %d named, %d without a name at the carrier, "
        "%d carrier not configured, %d lookup failed, %d duplicate",
        summary.named,
        summary.no_name,
        summary.no_plugin,
        summary.failed,
        summary.duplicate,
    )


if __name__ == "__main__":
    asyncio.run(_main())
