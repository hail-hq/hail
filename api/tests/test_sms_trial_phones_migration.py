"""0079 adds sms_trial_phones and the sms trial index."""

import importlib.util
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from hailhq.core.models import SmsTrialPhone
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.recreate_schema
spec = importlib.util.spec_from_file_location(
    "sms_trial_phones_migration",
    Path(__file__).parents[1] / "migrations/versions/0079_sms_trial_phones.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

PHONE = "+14155550142"


def _row(org: uuid.UUID, phone: str = PHONE, status: str = "pending"):
    return SmsTrialPhone(
        organization_id=org,
        phone_e164=phone,
        status=status,
        consent_version="v1",
        consent_at=datetime.now(timezone.utc),
    )


async def test_sms_trial_phones_round_trip(db):
    async with db.begin() as conn:

        def apply(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            migration.downgrade()
            migration.upgrade()

        await conn.run_sync(apply)

        async def indexdefs(table):
            return (
                (
                    await conn.execute(
                        text("SELECT indexdef FROM pg_indexes WHERE tablename=:t"),
                        {"t": table},
                    )
                )
                .scalars()
                .all()
            )

        phone_defs = await indexdefs("sms_trial_phones")
        verified = [d for d in phone_defs if "sms_trial_phones_verified_phone_uq" in d]
        assert verified and "WHERE (status = 'verified'" in verified[0]
        sms_defs = await indexdefs("sms")
        trial = [d for d in sms_defs if "sms_trial_org_idx" in d]
        assert trial and "metadata ? 'sms_trial'" in trial[0]


async def test_one_workspace_per_verified_phone(async_session):
    async_session.add_all(
        [
            _row(uuid.uuid4(), status="verified"),
            _row(uuid.uuid4(), status="verified"),
        ]
    )
    with pytest.raises(IntegrityError):
        await async_session.flush()


async def test_two_pending_rows_for_one_phone_are_allowed(async_session):
    async_session.add_all([_row(uuid.uuid4()), _row(uuid.uuid4())])
    await async_session.flush()


async def test_one_row_per_workspace(async_session):
    org = uuid.uuid4()
    async_session.add_all([_row(org), _row(org, phone="+14155550143")])
    with pytest.raises(IntegrityError):
        await async_session.flush()
