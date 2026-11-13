"""Independent bookkeeping must work while requests hold the runtime pool."""

import asyncio
import uuid
from contextlib import AsyncExitStack

import pytest
from hailhq.api import usage
from hailhq.api.audit import write_audit_log
from hailhq.api.idempotency import _try_acquire_or_load
from hailhq.core import db as core_db
from hailhq.core.models import AuditLog, IdempotencyKey, UsageEvent
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.mark.parametrize("kind", ["audit", "usage", "idempotency"])
async def test_independent_writes_finish_while_requests_hold_the_runtime_pool(
    db, database_url, org_and_key, monkeypatch, kind
):
    org_id, key, _ = org_and_key
    marker = f"pool-regression-{uuid.uuid4()}"
    monkeypatch.setattr(core_db, "_engine", None)
    monkeypatch.setattr(core_db, "_sessionmaker", None)
    monkeypatch.setattr(core_db, "_independent_engine", None)
    monkeypatch.setattr(core_db, "_independent_sessionmaker", None)
    monkeypatch.setattr(core_db.settings, "database_url", database_url)
    monkeypatch.setattr(core_db.settings, "database_pool_size", 2)
    monkeypatch.setattr(core_db.settings, "database_max_overflow", 0)
    monkeypatch.setattr(core_db, "telemetry_enabled", lambda: False)
    monkeypatch.setattr(usage, "notify_usage_event_recorded", lambda _: None)
    factory = core_db._ensure_initialized()

    async def write():
        if kind == "audit":
            await write_audit_log(org_id, key.id, marker, "email", None, {})
        elif kind == "usage":
            await usage.write_usage_event(
                organization_id=org_id, channel="email", units=1, ref=marker
            )
        else:
            assert await _try_acquire_or_load(marker, org_id, "request-hash") is None

    try:
        async with AsyncExitStack() as requests:
            for _ in range(2):
                session = await requests.enter_async_context(factory())
                await session.execute(text("SELECT 1"))
            await asyncio.wait_for(write(), timeout=2)

        # Check durable effects in a separate transaction, not mock calls.
        async with async_sessionmaker(db)() as check:
            stmt = {
                "audit": select(AuditLog.id).where(AuditLog.action == marker),
                "usage": select(UsageEvent.id).where(UsageEvent.ref == marker),
                "idempotency": select(IdempotencyKey.key).where(
                    IdempotencyKey.key == marker
                ),
            }[kind]
            assert (await check.execute(stmt)).scalar_one() is not None
    finally:
        await core_db.dispose_engine()


async def test_usage_event_is_kept_when_the_pool_is_busy(db, org_and_key, monkeypatch):
    """A send must stay billed when its usage write first times out waiting
    for a connection."""
    from contextlib import asynccontextmanager

    from sqlalchemy.exc import TimeoutError as PoolTimeout

    org_id, _, _ = org_and_key
    marker = f"pool-busy-{uuid.uuid4()}"
    real = usage.independent_session_scope
    opened = 0

    @asynccontextmanager
    async def busy_once():
        nonlocal opened
        opened += 1
        if opened == 1:
            raise PoolTimeout("pool busy")
        async with real() as session:
            yield session

    monkeypatch.setattr(usage, "independent_session_scope", busy_once)
    monkeypatch.setattr(usage, "notify_usage_event_recorded", lambda _: None)
    await usage.write_usage_event(
        organization_id=org_id, channel="email", units=1, ref=marker
    )
    assert opened == 2
    async with async_sessionmaker(db)() as check:
        found = await check.execute(
            select(UsageEvent.id).where(UsageEvent.ref == marker)
        )
        assert found.scalar_one() is not None
