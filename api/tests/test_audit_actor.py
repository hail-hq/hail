import uuid
from types import SimpleNamespace

from hailhq.api.audit import actor_of, write_audit_log
from hailhq.core.models import AuditLog
from sqlalchemy import select


def test_actor_of_classifies_principals():
    u = uuid.uuid4()
    assert actor_of(
        SimpleNamespace(user_id=u, api_key_id=uuid.uuid4(), superadmin=False)
    ) == (u, "api_key")
    assert actor_of(SimpleNamespace(user_id=u, api_key_id=None, superadmin=False)) == (
        u,
        "user",
    )
    assert actor_of(SimpleNamespace(user_id=u, api_key_id=None, superadmin=True)) == (
        u,
        "superadmin",
    )
    assert actor_of(
        SimpleNamespace(user_id=None, api_key_id=None, superadmin=False)
    ) == (None, "system")


async def test_write_audit_log_stores_actor(async_session):
    org, user = uuid.uuid4(), uuid.uuid4()
    await write_audit_log(
        org,
        None,
        "verification.approve",
        "carrier_verification",
        None,
        {},
        actor_user_id=user,
        actor_kind="superadmin",
    )
    row = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.organization_id == org)
        )
    ).scalar_one()
    assert row.actor_user_id == user and row.actor_kind == "superadmin"
