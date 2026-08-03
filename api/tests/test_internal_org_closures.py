"""Tests for POST /internal/org-closures.

hail-website calls this to notify hail that an account was closed/deleted;
this test suite exercises the receiving endpoint only (see
api/hailhq/api/routes/internal/org_closures.py for the integration note).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.api.routes.internal.org_closures import (
    OrgClosureIn,
    record_org_closure,
)
from hailhq.api.routes.internal.org_closures import (
    router as org_closures_router,
)
from hailhq.core.config import settings
from hailhq.core.models import OrgClosure
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_internal_dsar import _signed, internal_secret_set  # noqa: F401


@pytest.mark.asyncio
async def test_rejects_when_secret_unconfigured(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "hail_internal_secret", "")
    body = json.dumps(
        {
            "organization_id": str(uuid.uuid4()),
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
    ).encode()
    resp = await client.post(
        "/internal/org-closures", content=body, headers=_signed(body)
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_rejects_missing_signature(
    client: httpx.AsyncClient, internal_secret_set  # noqa: F811
):
    resp = await client.post(
        "/internal/org-closures",
        json={
            "organization_id": str(uuid.uuid4()),
            "closed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_rejects_bad_signature(
    client: httpx.AsyncClient, internal_secret_set  # noqa: F811
):
    body = json.dumps(
        {
            "organization_id": str(uuid.uuid4()),
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
    ).encode()
    resp = await client.post(
        "/internal/org-closures",
        content=body,
        headers={"X-Hail-Signature": "sha256=deadbeef"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_happy_path_inserts_row(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    internal_secret_set,  # noqa: F811
):
    org_id = uuid.uuid4()
    closed_at = datetime.now(timezone.utc).replace(microsecond=0)
    body = json.dumps(
        {
            "organization_id": str(org_id),
            "closed_at": closed_at.isoformat(),
            "source": "hail_website",
        }
    ).encode()

    resp = await client.post(
        "/internal/org-closures", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200
    assert resp.json()["organization_id"] == str(org_id)

    row = (
        await async_session.execute(
            select(OrgClosure).where(OrgClosure.organization_id == org_id)
        )
    ).scalar_one()
    assert row.closed_at == closed_at
    assert row.source == "hail_website"


@pytest.mark.asyncio
async def test_repeat_notification_upserts_existing_row(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    internal_secret_set,  # noqa: F811
):
    org_id = uuid.uuid4()
    first_closed_at = datetime.now(timezone.utc).replace(microsecond=0)
    body1 = json.dumps(
        {"organization_id": str(org_id), "closed_at": first_closed_at.isoformat()}
    ).encode()
    resp1 = await client.post(
        "/internal/org-closures", content=body1, headers=_signed(body1)
    )
    assert resp1.status_code == 200

    corrected_closed_at = first_closed_at + timedelta(days=1)
    body2 = json.dumps(
        {
            "organization_id": str(org_id),
            "closed_at": corrected_closed_at.isoformat(),
            "source": "manual_correction",
        }
    ).encode()
    resp2 = await client.post(
        "/internal/org-closures", content=body2, headers=_signed(body2)
    )
    assert resp2.status_code == 200

    rows = (
        (
            await async_session.execute(
                select(OrgClosure).where(OrgClosure.organization_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].closed_at == corrected_closed_at
    assert rows[0].source == "manual_correction"


async def test_concurrent_notifications_for_same_org_do_not_race(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Two concurrent notifications for the same organization_id must not
    raise an IntegrityError — the whole point of the documented
    idempotency guarantee. Uses two independent sessions (like two real
    concurrent requests would get), not the shared-session `client`
    fixture, so this actually exercises the race."""
    org_id = uuid.uuid4()
    closed_at = datetime.now(timezone.utc).replace(microsecond=0)

    async def _notify() -> dict:
        async with session_factory() as session:
            return await record_org_closure(
                OrgClosureIn(
                    organization_id=org_id, closed_at=closed_at, source="hail_website"
                ),
                session,
            )

    # Warm the pool with two concurrent connections first. On a cold pool,
    # connection *establishment* for the two sessions is serialized enough
    # that the first notification's get-then-write completes before the
    # second one's SELECT is even sent, masking the race. Two connections
    # already open and idle is what a live server actually looks like under
    # concurrent requests, and is what makes this test reliably exercise the
    # interleaving instead of passing by accident.
    async def _warm() -> None:
        async with session_factory() as session:
            await session.execute(select(1))

    await asyncio.gather(_warm(), _warm())

    results = await asyncio.gather(_notify(), _notify())
    assert all(r["organization_id"] == str(org_id) for r in results)


def test_router_level_dependency_protects_the_whole_router():
    """Mirrors dsar.py's router-level wiring: the auth dependency must be
    attached to the APIRouter itself, not to individual route decorators,
    so a future route added to this file is protected by construction."""
    dependant_callables = {
        dep.dependency
        for route in org_closures_router.routes
        for dep in route.dependencies
    }
    assert verify_internal_request in dependant_callables
    router_level_dependant_callables = {
        dep.dependency for dep in org_closures_router.dependencies
    }
    assert verify_internal_request in router_level_dependant_callables
