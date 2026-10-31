"""Platform activity reads. No delivery state or schema is written here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .report import CREATION_ACTIONS, REPORT_KIND, ActivityData, ReportWindow

# The key owner's oldest membership, the workspace the API resolves for a key.
_KEY_ORGANIZATION = """
  JOIN LATERAL (
    SELECT organization_id FROM members WHERE user_id::text = k.reference_id
     ORDER BY created_at, organization_id LIMIT 1
  ) m ON TRUE
"""
_ACTOR_SOURCE = """
  CASE WHEN a.actor_kind IN ('user','api_key','superadmin','system') THEN a.actor_kind
    WHEN a.api_key_id IS NOT NULL THEN 'api_key'
    WHEN a.actor_user_id IS NOT NULL THEN 'user'
    ELSE 'unknown' END
"""


async def read_activity(
    db: AsyncConnection, window: ReportWindow, reporter_hash: str | None
) -> ActivityData:
    params = {
        "start": window.start,
        "end": window.end,
        "kind": REPORT_KIND,
        "reporter_hash": reporter_hash,
        "creation_actions": CREATION_ACTIONS,
    }
    organizations = (await db.execute(text("""
            SELECT o.id, o.name, o.created_at,
              COALESCE(array_agg(DISTINCT u.email ORDER BY u.email)
                FILTER (WHERE m.role = 'owner' AND u.email IS NOT NULL), '{}') AS emails
              FROM organizations o
              LEFT JOIN members m ON m.organization_id = o.id
              LEFT JOIN users u ON u.id = m.user_id
             GROUP BY o.id ORDER BY o.name
            """))).mappings().all()
    traffic = (
        (
            await db.execute(
                text(f"""
            WITH reporter AS (
              SELECT m.organization_id FROM api_keys k {_KEY_ORGANIZATION}
               WHERE k.key = :reporter_hash LIMIT 1
            ), traffic AS (
              SELECT organization_id, id, 'call' AS resource_type, 'Calls' AS channel,
                     direction, status, requested_at, FALSE AS forwarded, FALSE AS automated
                FROM calls WHERE requested_at >= :start AND requested_at < :end
              UNION ALL
              SELECT organization_id, id, 'sms', 'SMS', direction, status, requested_at, FALSE, FALSE
                FROM sms WHERE requested_at >= :start AND requested_at < :end
              UNION ALL
              SELECT organization_id, id, 'email', 'Emails', direction, status, requested_at,
                     metadata ? 'forwarded_from', metadata ? 'system_kind'
                FROM emails WHERE requested_at >= :start AND requested_at < :end
                 AND (metadata->>'hail_report' IS DISTINCT FROM :kind
                   OR organization_id IS DISTINCT FROM (SELECT organization_id FROM reporter))
            ), attributed AS (
              SELECT t.*, CASE
                WHEN direction = 'inbound' THEN 'incoming'
                WHEN forwarded THEN 'forward'
                WHEN automated THEN 'system'
                ELSE {_ACTOR_SOURCE} END AS source
              FROM traffic t LEFT JOIN LATERAL (
                SELECT actor_kind, api_key_id, actor_user_id FROM audit_log a
                 WHERE a.organization_id = t.organization_id AND a.resource_id = t.id
                   AND a.resource_type = t.resource_type
                   AND a.action = ANY(CAST(:creation_actions AS text[]))
                 ORDER BY a.occurred_at DESC LIMIT 1
              ) a ON TRUE
            )
            SELECT organization_id AS org_id, channel, direction, status, source, count(*)::int AS count
              FROM attributed GROUP BY organization_id, channel, direction, status, source
             ORDER BY organization_id, channel, direction, status, source
            """),
                params,
            )
        )
        .mappings()
        .all()
    )
    actions = (
        (
            await db.execute(
                text(f"""
            SELECT organization_id AS org_id, action, {_ACTOR_SOURCE} AS source,
              count(*)::int AS count
              FROM audit_log a WHERE occurred_at >= :start AND occurred_at < :end
               AND NOT (action = ANY(CAST(:creation_actions AS text[])))
             GROUP BY organization_id, action, source ORDER BY organization_id, action, source
            """),
                params,
            )
        )
        .mappings()
        .all()
    )
    accesses = (
        (
            await db.execute(
                text(f"""
            SELECT m.organization_id AS org_id, max(k.last_request) AS last_at
              FROM api_keys k {_KEY_ORGANIZATION}
             WHERE k.last_request >= :start AND k.last_request < :end
               AND k.key IS DISTINCT FROM :reporter_hash
             GROUP BY m.organization_id
            """),
                params,
            )
        )
        .mappings()
        .all()
    )
    return ActivityData(
        organizations=[{**row, "id": str(row["id"])} for row in organizations],
        traffic=[{**row, "org_id": str(row["org_id"])} for row in traffic],
        actions=[{**row, "org_id": str(row["org_id"])} for row in actions],
        accesses=[{**row, "org_id": str(row["org_id"])} for row in accesses],
    )


@dataclass(frozen=True)
class Receipt:
    id: str
    status: str
    requested_at: datetime


async def find_receipt(db: AsyncConnection, org_id: str, key: str) -> Receipt | None:
    row = (
        (
            await db.execute(
                text("""
            SELECT id, status, requested_at FROM emails
             WHERE organization_id = :org AND direction = 'outbound'
               AND metadata->>'hail_report' = :kind AND metadata->>'report_key' = :key
             ORDER BY created_at DESC LIMIT 1
            """),
                {"org": UUID(org_id), "kind": REPORT_KIND, "key": key},
            )
        )
        .mappings()
        .first()
    )
    return Receipt(str(row["id"]), row["status"], row["requested_at"]) if row else None


class Repository:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def snapshot(self, window, reporter_hash, org_id=None, key=None):
        async with self.engine.connect() as db, db.begin():
            await db.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            await db.execute(text("SET LOCAL statement_timeout = '30s'"))
            receipt = await find_receipt(db, org_id, key) if org_id and key else None
            data = None if receipt else await read_activity(db, window, reporter_hash)
            return receipt, data
