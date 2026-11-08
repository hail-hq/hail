"""0068: one suppression row per (organization, recipient, channel)."""

import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

spec = importlib.util.spec_from_file_location(
    "suppressions_uniq_migration",
    Path(__file__).parents[1] / "migrations/versions/0068_suppressions_uniq.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


async def _run(db, step) -> None:
    async with db.begin() as connection:

        def go(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            step()

        await connection.run_sync(go)


async def _insert(db, org, reason, minutes=0, recipient="bob@example.com") -> None:
    async with db.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO suppressions (organization_id, recipient, channel, "
                "reason, source, created_at) VALUES (:org, :recipient, 'email', "
                ":reason, 'bounce', :at)"
            ),
            {
                "org": org,
                "recipient": recipient,
                "reason": reason,
                "at": T0 + timedelta(minutes=minutes),
            },
        )


async def _reasons(db) -> list[tuple]:
    async with db.begin() as connection:
        rows = await connection.execute(
            text(
                "SELECT organization_id, recipient, reason FROM suppressions "
                "ORDER BY organization_id NULLS FIRST, recipient, reason"
            )
        )
        return [tuple(r) for r in rows]


async def test_down_then_up_keeps_oldest_row(db) -> None:
    # The test schema is the models' schema: already upgraded.
    await _run(db, migration.downgrade)
    org = uuid.uuid4()
    await _insert(db, org, "newer", minutes=5)
    await _insert(db, org, "oldest")
    await _insert(db, org, "other", recipient="carol@example.com")
    await _insert(db, None, "platform-newer", minutes=5)
    await _insert(db, None, "platform-oldest")

    await _run(db, migration.upgrade)
    assert await _reasons(db) == [
        (None, "bob@example.com", "platform-oldest"),
        (org, "bob@example.com", "oldest"),
        (org, "carol@example.com", "other"),
    ]
    with pytest.raises(IntegrityError):
        await _insert(db, org, "again")
    with pytest.raises(IntegrityError):
        await _insert(db, None, "again")
