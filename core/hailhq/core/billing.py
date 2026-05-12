"""Balance read primitives for the POST /v1/calls gate.

After the usage_events / rater split, hail/api no longer writes ledger rows
directly — the website's private rater is the sole producer of debit rows.
This module exists only to read the balance for the gate.

Self-host behavior: the seeded "Self-hosted" organization carries a large
initial credit from migration 0001. Master-key (HAIL_API_KEY) auth scopes to
that org, so ``has_funds`` always returns True for it without any rater
running.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import AccountCredit

logger = logging.getLogger(__name__)


async def get_balance_cents(db: AsyncSession, organization_id: uuid.UUID) -> int:
    """Current balance for an org, in cents. SUM over the ledger."""
    stmt = select(func.coalesce(func.sum(AccountCredit.amount_cents), 0)).where(
        AccountCredit.organization_id == organization_id
    )
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)


async def has_funds(db: AsyncSession, organization_id: uuid.UUID) -> bool:
    """True iff the org has a positive balance. Single source of truth for the gate."""
    return await get_balance_cents(db, organization_id) > 0
