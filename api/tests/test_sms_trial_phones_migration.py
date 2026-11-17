"""0082 adds sms_trial_phones; 0083 adds the sms trial index."""

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from hailhq.core.db import to_sync_url
from hailhq.core.models import SmsTrialPhone
from hailhq.core.testing.fixtures import database_url  # noqa: F401
from sqlalchemy.exc import IntegrityError

from .test_migrations import (  # noqa: F401
    API_DIR,
    _run_alembic,
    _to_libpq_url,
    empty_db,
)

pytestmark = pytest.mark.recreate_schema

PHONE = "+14155550142"


def _row(org: uuid.UUID, phone: str = PHONE, status: str = "pending"):
    return SmsTrialPhone(
        organization_id=org,
        phone_e164=phone,
        status=status,
        consent_version="v1",
        consent_at=datetime.now(timezone.utc),
    )


def _index_defs(conn: psycopg.Connection, table: str) -> dict[str, str]:
    return dict(
        conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s",
            (table,),
        ).fetchall()
    )


def test_sms_trial_phones_round_trip(empty_db: str) -> None:  # noqa: F811
    """Real alembic, as deploys run it: 0083 builds the sms index
    CONCURRENTLY in an autocommit block, which cannot run inside a
    transaction."""
    url = _to_libpq_url(to_sync_url(empty_db))
    _run_alembic(empty_db, ["upgrade", "head"])
    with psycopg.connect(url, autocommit=True) as conn:
        phone_defs = _index_defs(conn, "sms_trial_phones")
        verified = phone_defs["sms_trial_phones_verified_phone_uq"]
        assert "WHERE (status = 'verified'" in verified
        log = phone_defs["sms_trial_phones_code_send_log_idx"]
        assert "USING gin (code_send_log jsonb_path_ops)" in log
        trial = _index_defs(conn, "sms")["sms_trial_org_idx"]
        assert "metadata ? 'sms_trial'" in trial
        checks = {
            r[0]
            for r in conn.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'sms_trial_phones'::regclass AND contype = 'c'"
            ).fetchall()
        }
        assert {
            "sms_trial_phones_phone_e164_check",
            "sms_trial_phones_pending_phone_e164_check",
        } <= checks

    _run_alembic(empty_db, ["downgrade", "0082"])
    with psycopg.connect(url, autocommit=True) as conn:
        assert "sms_trial_org_idx" not in _index_defs(conn, "sms")
        assert "sms_trial_phones_code_send_log_idx" in _index_defs(
            conn, "sms_trial_phones"
        )

    _run_alembic(empty_db, ["downgrade", "0081"])
    with psycopg.connect(url, autocommit=True) as conn:
        assert _index_defs(conn, "sms_trial_phones") == {}

    _run_alembic(empty_db, ["upgrade", "head"])
    with psycopg.connect(url, autocommit=True) as conn:
        assert "sms_trial_org_idx" in _index_defs(conn, "sms")


def _offline_sql(revisions: str, *, down: bool = False) -> str:
    """The SQL alembic would run, without a database."""
    env = {**os.environ, "DATABASE_URL": "postgresql+psycopg://offline@localhost/x"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade" if down else "upgrade"]
        + [revisions, "--sql"],
        cwd=str(API_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_sms_index_is_built_and_dropped_concurrently() -> None:
    """``sms`` is a hot table: building the index must not block writes.
    It has its own revision, so a failed build leaves 0082 applied and the
    re-run builds only the index."""
    up = _offline_sql("0082:0083")
    assert "CREATE INDEX CONCURRENTLY sms_trial_org_idx" in up
    assert "sms_trial_phones" not in up
    assert "DROP INDEX CONCURRENTLY sms_trial_org_idx" in _offline_sql(
        "0083:0082", down=True
    )
    table = _offline_sql("0081:0082")
    assert "CREATE TABLE sms_trial_phones" in table
    assert "sms_trial_org_idx" not in table


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


@pytest.mark.parametrize("bad", [PHONE + "\n", "+123", "14155550142", "+0415555014"])
async def test_phone_must_be_canonical_e164(async_session, bad):
    async_session.add(_row(uuid.uuid4(), phone=bad))
    with pytest.raises(IntegrityError, match="sms_trial_phones_phone_e164_check"):
        await async_session.flush()


@pytest.mark.parametrize("bad", [PHONE + "\n", "+123", "14155550142"])
async def test_pending_phone_must_be_canonical_e164(async_session, bad):
    row = _row(uuid.uuid4(), status="verified")
    row.pending_phone_e164 = bad
    async_session.add(row)
    with pytest.raises(
        IntegrityError, match="sms_trial_phones_pending_phone_e164_check"
    ):
        await async_session.flush()
