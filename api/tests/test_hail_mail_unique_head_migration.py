"""0070: one hail-mail address per org, one head per deployment."""

import hashlib
import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from hailhq.core.models import (
    EMAIL_DOMAINS_GUARD_FUNCTION,
    EMAIL_DOMAINS_GUARD_TRIGGER,
    EMAIL_DOMAINS_RETIRE_FUNCTION,
    EMAIL_DOMAINS_RETIRE_TRIGGER,
)
from hailhq.core.schemas import LOCAL_PREFIX
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

spec = importlib.util.spec_from_file_location(
    "hail_mail_unique_head_migration",
    Path(__file__).parents[1] / "migrations/versions/0070_hail_mail_unique_head.py",
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


def _hash(domain_id: uuid.UUID) -> str:
    return hashlib.md5(str(domain_id).encode()).hexdigest()[:8]


async def _row(db, head, tail, *, minutes=0, org=None) -> uuid.UUID:
    domain_id = uuid.uuid4()
    async with db.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO email_domains (id, organization_id, kind, domain, "
                "local_prefix_user, local_prefix_org, verification_status, "
                "created_at) VALUES (:id, :org, 'hail_mail', :domain, :head, "
                ":tail, 'verified', :at)"
            ),
            {
                "id": domain_id,
                "org": org or uuid.uuid4(),
                "domain": f"{head}+{tail}@mail.example.com",
                "head": head,
                "tail": tail,
                "at": T0 + timedelta(minutes=minutes),
            },
        )
    return domain_id


async def _email(db, domain_id) -> None:
    async with db.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO emails (organization_id, email_domain_id, "
                "email_domain_kind, direction, from_address, to_addresses, "
                "subject, body_text, status) SELECT organization_id, id, kind, "
                "'inbound', 'a@example.com', ARRAY['b@example.com'], 's', 'b', "
                "'received' "
                "FROM email_domains WHERE id = :id"
            ),
            {"id": domain_id},
        )


async def _address(db, domain_id) -> tuple[str, str]:
    async with db.begin() as connection:
        row = await connection.execute(
            text("SELECT local_prefix_user, domain FROM email_domains WHERE id = :id"),
            {"id": domain_id},
        )
        return tuple(row.one())


async def test_row_with_most_emails_keeps_the_head(db) -> None:
    # The test schema is the models' schema: already upgraded.
    await _run(db, migration.downgrade)
    oldest = await _row(db, "alice", "a")
    busiest = await _row(db, "alice", "b", minutes=5)
    await _email(db, busiest)

    await _run(db, migration.upgrade)

    assert await _address(db, busiest) == ("alice", "alice+b@mail.example.com")
    head, domain = await _address(db, oldest)
    assert head == f"alice-{_hash(oldest)}"
    assert domain == f"{head}+a@mail.example.com"


async def test_oldest_row_keeps_the_head_on_a_tie(db) -> None:
    await _run(db, migration.downgrade)
    newer = await _row(db, "alice", "b", minutes=5)
    oldest = await _row(db, "alice", "a")

    await _run(db, migration.upgrade)

    assert (await _address(db, oldest))[0] == "alice"
    assert (await _address(db, newer))[0] != "alice"


async def test_long_head_with_hyphen_at_the_cut_stays_valid(db) -> None:
    await _run(db, migration.downgrade)
    head = "abcdefghij-klmnopqrs"
    await _row(db, head, "a")
    other = await _row(db, head, "b", minutes=5)

    await _run(db, migration.upgrade)

    rewritten, _ = await _address(db, other)
    assert rewritten == f"abcdefghij-{_hash(other)}"
    assert LOCAL_PREFIX.fullmatch(rewritten)


async def test_unique_rule_and_downgrade(db) -> None:
    await _row(db, "alice", "a")
    with pytest.raises(IntegrityError):
        await _row(db, "alice", "b")

    await _run(db, migration.downgrade)
    await _row(db, "alice", "b")
    with pytest.raises(IntegrityError):
        await _row(db, "alice", "b")
    await _run(db, migration.upgrade)


async def _retired(db) -> list[tuple]:
    async with db.begin() as connection:
        rows = await connection.execute(
            text(
                "SELECT local_prefix_user, local_prefix_org, organization_id "
                "FROM retired_hail_mail_addresses"
            )
        )
        return [tuple(r) for r in rows]


async def test_rewrite_records_the_old_address(db) -> None:
    await _run(db, migration.downgrade)
    await _row(db, "alice", "a")
    org = uuid.uuid4()
    await _row(db, "alice", "b", minutes=5, org=org)

    await _run(db, migration.upgrade)

    assert await _retired(db) == [("alice", "b", org)]


async def test_org_with_several_rows_keeps_the_busiest(db) -> None:
    await _run(db, migration.downgrade)
    org = uuid.uuid4()
    oldest = await _row(db, "alice", "a", org=org)
    busiest = await _row(db, "bob", "b", minutes=5, org=org)
    await _email(db, busiest)
    await _email(db, oldest)
    await _email(db, busiest)

    await _run(db, migration.upgrade)

    async with db.begin() as connection:
        rows = await connection.execute(text("SELECT id FROM email_domains"))
        assert [r[0] for r in rows] == [busiest]
        moved = await connection.execute(
            text("SELECT count(*) FROM emails WHERE email_domain_id = :id"),
            {"id": busiest},
        )
        assert moved.scalar_one() == 3
    assert await _retired(db) == [("alice", "a", org)]
    with pytest.raises(IntegrityError):
        await _row(db, "carol", "c", org=org)


async def test_merge_keeps_the_settings_of_every_row(db) -> None:
    await _run(db, migration.downgrade)
    org = uuid.uuid4()
    kept = await _row(db, "alice", "a", org=org)
    other = await _row(db, "bob", "b", minutes=5, org=org)
    async with db.begin() as connection:
        await connection.execute(
            text(
                "UPDATE email_domains SET forward_to = :to, "
                "forward_rate_per_hour = :rate, inbound_enabled = :on "
                "WHERE id = :id"
            ),
            [
                {"id": kept, "to": ["a@x.test", "b@x.test"], "rate": 7, "on": False},
                {"id": other, "to": ["b@x.test", "c@x.test"], "rate": 9, "on": True},
            ],
        )

    await _run(db, migration.upgrade)

    async with db.begin() as connection:
        row = await connection.execute(
            text(
                "SELECT forward_to, inbound_enabled, forward_rate_per_hour "
                "FROM email_domains WHERE id = :id"
            ),
            {"id": kept},
        )
        forward_to, inbound_enabled, rate = row.one()
    assert sorted(forward_to) == ["a@x.test", "b@x.test", "c@x.test"]
    assert inbound_enabled is True
    assert rate == 7


async def _locked_during_upgrade(db) -> set[str]:
    async with db.begin() as connection:

        def go(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            migration.upgrade()

        await connection.run_sync(go)
        rows = await connection.execute(
            text(
                "SELECT c.relname FROM pg_locks l "
                "JOIN pg_class c ON c.oid = l.relation "
                "WHERE l.pid = pg_backend_pid() "
                "AND l.mode = 'ShareRowExclusiveLock' "
                "AND c.relname IN ('emails', 'webhook_deliveries')"
            )
        )
        return {r[0] for r in rows}


async def test_merge_locks_the_tables_it_repoints(db) -> None:
    await _run(db, migration.downgrade)
    org = uuid.uuid4()
    await _row(db, "alice", "a", org=org)
    await _row(db, "bob", "b", org=org)

    assert await _locked_during_upgrade(db) == {"emails", "webhook_deliveries"}


async def test_no_merge_leaves_those_tables_unlocked(db) -> None:
    await _run(db, migration.downgrade)
    await _row(db, "alice", "a")

    assert await _locked_during_upgrade(db) == set()


def test_function_text_matches_the_code() -> None:
    assert _squash(migration.GUARD_FUNCTION) == _squash(EMAIL_DOMAINS_GUARD_FUNCTION)
    assert _squash(migration.RETIRE_FUNCTION) == _squash(EMAIL_DOMAINS_RETIRE_FUNCTION)
    assert _squash(migration.GUARD_TRIGGER) == _squash(EMAIL_DOMAINS_GUARD_TRIGGER)
    assert _squash(migration.RETIRE_TRIGGER) == _squash(EMAIL_DOMAINS_RETIRE_TRIGGER)


def _squash(sql: str) -> str:
    return "".join(sql.split())
