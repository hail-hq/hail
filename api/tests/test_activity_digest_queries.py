from datetime import datetime, timezone
from uuid import uuid4

import pytest
from hailhq.api.activity_digest import queries
from hailhq.api.activity_digest.queries import Repository, find_receipt, read_activity
from hailhq.api.activity_digest.report import daily_window
from hailhq.core.db import to_async_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

A, B, USER1, USER2 = (uuid4() for _ in range(4))
WINDOW = daily_window(datetime(2026, 10, 29, 9, tzinfo=timezone.utc))


@pytest.fixture
async def digest_engine(database_url):
    engine = create_async_engine(
        to_async_url(database_url), pool_size=1, max_overflow=0
    )
    schema = """
      CREATE TEMP TABLE organizations (id uuid PRIMARY KEY, name text, created_at timestamptz);
      CREATE TEMP TABLE users (id uuid, email text);
      CREATE TEMP TABLE members (organization_id uuid, user_id uuid, role text, created_at timestamptz);
      CREATE TEMP TABLE calls (id uuid DEFAULT gen_random_uuid(), organization_id uuid, direction text, status text, requested_at timestamptz);
      CREATE TEMP TABLE sms (LIKE calls INCLUDING DEFAULTS);
      CREATE TEMP TABLE emails (LIKE calls INCLUDING DEFAULTS, metadata jsonb DEFAULT '{}', created_at timestamptz DEFAULT now());
      CREATE TEMP TABLE audit_log (organization_id uuid, resource_id uuid, resource_type text, action text, actor_kind text, api_key_id uuid, actor_user_id uuid, occurred_at timestamptz);
      CREATE TEMP TABLE api_keys (reference_id text, key text, last_request timestamptz);
    """
    async with engine.begin() as db:
        for statement in schema.split(";"):
            if statement.strip():
                await db.execute(text(statement))
        await db.execute(
            text(
                "INSERT INTO organizations VALUES (:a,'Active org','2026-01-01'),(:b,'Other org','2026-01-01')"
            ),
            {"a": A, "b": B},
        )
        await db.execute(
            text(
                "INSERT INTO users VALUES (:u1,'owner1@example.com'),(:u2,'owner2@example.com')"
            ),
            {"u1": USER1, "u2": USER2},
        )
        await db.execute(
            text(
                "INSERT INTO members VALUES (:a,:u1,'owner','2026-01-01'),(:a,:u2,'owner','2026-01-02')"
            ),
            {"a": A, "u1": USER1, "u2": USER2},
        )
    try:
        yield engine
    finally:
        # Disposing the only physical connection drops all fixture tables.
        await engine.dispose()


async def test_counts_once_with_two_owners_and_exact_time_boundaries(digest_engine):
    async with digest_engine.begin() as db:
        await db.execute(
            text(
                """INSERT INTO calls (organization_id,direction,status,requested_at) VALUES
          (:a,'inbound','completed','2026-10-28 07:00Z'),
          (:a,'inbound','completed','2026-10-29 07:00Z'),
          (:a,'inbound','completed','2026-10-28 06:59Z')"""
            ),
            {"a": A},
        )
    async with digest_engine.connect() as db:
        data = await read_activity(db, WINDOW, None)
    assert data.organizations[0]["emails"] == [
        "owner1@example.com",
        "owner2@example.com",
    ]
    assert data.traffic == [
        {
            "org_id": str(A),
            "channel": "Calls",
            "direction": "inbound",
            "status": "completed",
            "source": "incoming",
            "count": 1,
        }
    ]


async def test_agent_sends_and_handover_are_automation_not_extra_setup(digest_engine):
    s1, s2, e1 = (uuid4() for _ in range(3))
    async with digest_engine.begin() as db:
        await db.execute(
            text(
                "INSERT INTO sms VALUES (:s1,:a,'outbound','sent','2026-10-28 09:00Z'),(:s2,:a,'outbound','sent','2026-10-28 09:00Z')"
            ),
            {"s1": s1, "s2": s2, "a": A},
        )
        await db.execute(
            text(
                """INSERT INTO emails (id,organization_id,direction,status,requested_at,metadata) VALUES
          (:e1,:a,'outbound','sent','2026-10-28 09:00Z','{}'),
          (gen_random_uuid(),:a,'outbound','sent','2026-10-28 09:00Z','{"system_kind":"account_notice"}')"""
            ),
            {"e1": e1, "a": A},
        )
        await db.execute(
            text(
                """INSERT INTO audit_log (organization_id,resource_id,resource_type,action,actor_kind,occurred_at) VALUES
          (:a,:s1,'sms','agent.sms.send','system','2026-10-28 09:00Z'),
          (:a,:s2,'sms','agent.handover.sms','system','2026-10-28 09:00Z'),
          (:a,:e1,'email','agent.email.send','system','2026-10-28 09:00Z')"""
            ),
            {"a": A, "s1": s1, "s2": s2, "e1": e1},
        )
    async with digest_engine.connect() as db:
        data = await read_activity(db, WINDOW, None)
    assert [(r["channel"], r["source"], r["count"]) for r in data.traffic] == [
        ("Emails", "system", 2),
        ("SMS", "system", 2),
    ]
    assert data.actions == []


async def test_report_marker_only_excludes_the_authenticated_reporting_workspace(
    digest_engine,
):
    async with digest_engine.begin() as db:
        await db.execute(
            text("INSERT INTO api_keys VALUES (:user,'reporter-hash',NULL)"),
            {"user": str(USER1)},
        )
        await db.execute(
            text(
                """INSERT INTO emails (organization_id,direction,status,requested_at,metadata) VALUES
          (:a,'outbound','sent','2026-10-28 09:00Z','{"hail_report":"daily_activity"}'),
          (:b,'outbound','sent','2026-10-28 09:00Z','{"hail_report":"daily_activity"}')"""
            ),
            {"a": A, "b": B},
        )
    async with digest_engine.connect() as db:
        data = await read_activity(db, WINDOW, "reporter-hash")
    assert [(r["org_id"], r["count"]) for r in data.traffic] == [(str(B), 1)]


async def test_access_uses_oldest_membership_and_excludes_the_reporting_key(
    digest_engine,
):
    async with digest_engine.begin() as db:
        await db.execute(
            text("INSERT INTO members VALUES (:b,:user,'owner','2026-02-01')"),
            {"b": B, "user": USER1},
        )
        await db.execute(
            text(
                "INSERT INTO api_keys VALUES (:user,'other-key','2026-10-28 10:00Z'),(:user,'reporter-hash','2026-10-28 11:00Z')"
            ),
            {"user": str(USER1)},
        )
    async with digest_engine.connect() as db:
        data = await read_activity(db, WINDOW, "reporter-hash")
    assert data.accesses == [
        {"org_id": str(A), "last_at": datetime(2026, 10, 28, 10, tzinfo=timezone.utc)}
    ]


async def test_receipts_are_scoped_to_workspace_and_key(digest_engine):
    async with digest_engine.begin() as db:
        await db.execute(
            text(
                """INSERT INTO emails (organization_id,direction,status,requested_at,metadata) VALUES
          (:a,'outbound','delivered','2026-10-29 07:00Z','{"hail_report":"daily_activity","report_key":"key"}')"""
            ),
            {"a": A},
        )
    async with digest_engine.connect() as db:
        assert (await find_receipt(db, str(A), "key")).status == "delivered"
        assert await find_receipt(db, str(B), "key") is None
        assert await find_receipt(db, str(A), "other") is None


async def test_snapshot_enforces_a_read_only_repeatable_transaction(
    digest_engine, monkeypatch
):
    original = queries.read_activity

    async def checked(db, window, reporter_hash):
        assert (await db.execute(text("SHOW transaction_read_only"))).scalar() == "on"
        assert (
            await db.execute(text("SHOW transaction_isolation"))
        ).scalar() == "repeatable read"
        return await original(db, window, reporter_hash)

    monkeypatch.setattr(queries, "read_activity", checked)
    receipt, data = await Repository(digest_engine).snapshot(WINDOW, None)
    assert receipt is None
    assert len(data.organizations) == 2
