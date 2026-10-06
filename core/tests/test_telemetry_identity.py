import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from hailhq.core.telemetry_identity import (
    IdentityMiddleware,
    get_identity,
    identity_scope,
    resolve_identity,
    set_identity,
)


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_share_identity():
    seen = []

    async def app(scope, receive, send):
        assert get_identity() == {}
        set_identity({"user_email": scope["email"], "organization_name": scope["org"]})
        await asyncio.sleep(0)
        seen.append(get_identity())

    middleware = IdentityMiddleware(app)
    await asyncio.gather(
        middleware({"email": "one@example.org", "org": "One"}, None, None),
        middleware({"email": "two@example.org", "org": "Two"}, None, None),
    )
    assert {row["user_email"] for row in seen} == {"one@example.org", "two@example.org"}
    assert all(
        row["organization_name"]
        == ("One" if row["user_email"].startswith("one") else "Two")
        for row in seen
    )
    assert get_identity() == {}


@pytest.mark.asyncio
async def test_identity_lookup_failure_keeps_ids_and_business_transaction():
    entered = []

    @asynccontextmanager
    async def savepoint():
        entered.append(True)
        yield

    org, user = uuid4(), uuid4()
    db = SimpleNamespace(
        begin_nested=savepoint,
        scalar=AsyncMock(side_effect=RuntimeError("lookup unavailable")),
    )
    identity = await resolve_identity(db, org, user, actor_kind="jwt")
    assert identity == {
        "organization_id": str(org),
        "user_id": str(user),
        "actor_kind": "jwt",
    }
    assert entered == [True]


def test_job_scope_restores_identity_even_after_an_error():
    with identity_scope({"organization_name": "Outer"}):
        with pytest.raises(RuntimeError), identity_scope(
            {"organization_name": "Inner"}
        ):
            assert get_identity()["organization_name"] == "Inner"
            raise RuntimeError("job failed")
        assert get_identity()["organization_name"] == "Outer"
    assert get_identity() == {}
