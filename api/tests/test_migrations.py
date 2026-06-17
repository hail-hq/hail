"""Real-alembic migration tests for the duplicate-0003 recovery.

Two reachable starting states have to converge on the same head schema:

* Fresh DB — no alembic_version, no tables. Runs 0001..0005 in order.
* "State B" — a dev DB upgraded to head between commits 9b0bcea (May 15)
  and deb3692 (May 16) when two migrations shared revision id 0003.
  Those DBs ran the original ``0003_audit_log_api_key_id_uuid``: their
  ``audit_log.api_key_id`` is already UUID, ``alembic_version='0003'``,
  and they have **no** ``call_end_reason`` ENUM. Migration 0004 has to
  detect the missing ENUM and apply it via the idempotent guard.

Tests subprocess ``alembic`` so the production env.py is exercised end
to end. They wipe ``public`` before and after so other tests (which use
``Base.metadata.create_all`` via ``async_session``) aren't polluted by
the alembic-driven artifacts (functions, triggers, ENUMs).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import psycopg
import pytest

from hailhq.core.db import to_sync_url
from hailhq.core.testing.fixtures import database_url  # noqa: F401

API_DIR = Path(__file__).resolve().parents[1]


def _to_libpq_url(url: str) -> str:
    """Strip the SQLAlchemy driver suffix so libpq/psycopg accepts the URL."""
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


def _wipe_public_schema(url: str) -> None:
    """Reset the database to a truly empty state.

    ``DROP SCHEMA public CASCADE`` removes tables, sequences, types
    (including ENUMs), functions, and triggers — everything alembic
    might have created. pgcrypto stays installed at the database level.
    """
    with psycopg.connect(_to_libpq_url(to_sync_url(url)), autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")


def _run_alembic(url: str, args: list[str]) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(API_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic {' '.join(args)} failed:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


@pytest.fixture()
def empty_db(database_url: str) -> Iterator[str]:  # noqa: F811
    """A wiped DB yielded to the test; wiped again on teardown.

    Other tests in this package use ``async_session`` which does its own
    ``drop_all`` + ``create_all`` cycle, so they're insulated from this
    fixture's effects either way.
    """
    _wipe_public_schema(database_url)
    yield database_url
    _wipe_public_schema(database_url)


def _column_data_type(conn: psycopg.Connection, table: str, column: str) -> str | None:
    cur = conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = %s AND column_name = %s",
        (table, column),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _enum_exists(conn: psycopg.Connection, name: str) -> bool:
    cur = conn.execute("SELECT 1 FROM pg_type WHERE typname = %s", (name,))
    return cur.fetchone() is not None


def _table_exists(conn: psycopg.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = %s",
        (name,),
    )
    return cur.fetchone() is not None


def _constraint_exists(conn: psycopg.Connection, name: str) -> bool:
    cur = conn.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (name,))
    return cur.fetchone() is not None


def _constraint_src(conn: psycopg.Connection, name: str) -> str | None:
    """Return the Postgres-normalised CHECK expression for a named constraint."""
    cur = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
        (name,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _assert_head_schema(url: str) -> None:
    """Every reachable upgrade path lands here."""
    with psycopg.connect(_to_libpq_url(to_sync_url(url))) as conn:
        assert _enum_exists(conn, "call_end_reason"), "call_end_reason ENUM missing"
        end_reason_type = conn.execute(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = 'calls' AND column_name = 'end_reason'"
        ).fetchone()
        assert end_reason_type is not None and end_reason_type[0] == "call_end_reason"
        assert _column_data_type(conn, "audit_log", "api_key_id") == "uuid"
        assert _constraint_exists(conn, "calls_end_reason_when_terminal")
        assert _table_exists(conn, "emails")
        assert _table_exists(conn, "email_domains")
        # The prefix-consistency CHECK landed in 0005 on sender_domains
        # and was renamed alongside the table in 0006.
        assert _constraint_exists(conn, "email_domains_prefix_kind_consistency")
        # 0007 added the inbound-email schema additions.
        assert _table_exists(conn, "email_attachments")
        assert _constraint_exists(conn, "emails_direction_check")
        assert _constraint_exists(conn, "emails_outbound_has_domain")
        assert _constraint_exists(conn, "email_domains_inbound_action")


def test_fresh_db_upgrade_head(empty_db: str) -> None:
    """A brand-new DB runs every migration in order and ends at the
    same shape every other path converges on."""
    _run_alembic(empty_db, ["upgrade", "head"])
    _assert_head_schema(empty_db)


def test_audit_log_flavored_0003_db_converges(empty_db: str) -> None:
    """Dev DBs that ran the old audit_log-flavored 0003 (between 9b0bcea
    and deb3692) sit at alembic_version='0003' with audit_log.api_key_id
    already UUID and **no** call_end_reason ENUM. Migration 0004's
    convergence guards must apply the missing ENUM + CHECK on the next
    ``upgrade head``."""
    # Bring the DB up to 0002 cleanly via real alembic.
    _run_alembic(empty_db, ["upgrade", "0002"])

    # Replay the OLD 0003_audit_log_api_key_id_uuid body (from 9b0bcea)
    # to land the State-B shape, then stamp alembic_version='0003' so
    # alembic believes 0003 has already been applied.
    with psycopg.connect(_to_libpq_url(to_sync_url(empty_db)), autocommit=True) as conn:
        conn.execute(
            "UPDATE audit_log SET api_key_id = NULL WHERE api_key_id = 'shared'"
        )
        conn.execute(
            "ALTER TABLE audit_log "
            "ALTER COLUMN api_key_id TYPE uuid USING api_key_id::uuid"
        )
        conn.execute("UPDATE alembic_version SET version_num = '0003'")

    # Confirm State B before the recovery: audit_log is uuid, ENUM absent.
    with psycopg.connect(_to_libpq_url(to_sync_url(empty_db))) as conn:
        assert _column_data_type(conn, "audit_log", "api_key_id") == "uuid"
        assert not _enum_exists(
            conn, "call_end_reason"
        ), "test setup didn't simulate State B correctly — ENUM already present"
        assert not _constraint_exists(conn, "calls_end_reason_when_terminal")

    # The recovery itself.
    _run_alembic(empty_db, ["upgrade", "head"])
    _assert_head_schema(empty_db)


def _index_def(conn: psycopg.Connection, indexname: str) -> str | None:
    cur = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
        (indexname,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def test_provider_message_id_unique_is_outbound_only(empty_db: str) -> None:
    """After 0009 the global unique is gone; only a partial (outbound) one remains."""
    _run_alembic(empty_db, ["upgrade", "0009"])
    with psycopg.connect(_to_libpq_url(to_sync_url(empty_db))) as conn:
        # The old global unique constraint must be gone.
        assert not _constraint_exists(
            conn, "emails_provider_message_id_key"
        ), "global unique on provider_message_id still present after 0009"
        # A partial unique index scoped to outbound must exist.
        indexdef = _index_def(conn, "emails_provider_message_id_outbound_uq")
        assert (
            indexdef is not None
        ), "partial unique index emails_provider_message_id_outbound_uq missing"
        assert "direction" in indexdef and "outbound" in indexdef


def test_hail_mail_prefix_pair_is_globally_unique(empty_db: str) -> None:
    """After 0010 a partial unique index makes hail_mail prefix pairs
    global — inbound routing matches (local_prefix_user, local_prefix_org)
    with no org scoping, so two orgs holding the same pair would let one
    intercept the other's mail (even across base-domain changes)."""
    _run_alembic(empty_db, ["upgrade", "head"])
    with psycopg.connect(_to_libpq_url(to_sync_url(empty_db))) as conn:
        indexdef = _index_def(conn, "email_domains_hail_mail_prefix_uq")
        assert (
            indexdef is not None
        ), "partial unique index email_domains_hail_mail_prefix_uq missing"
        assert "UNIQUE" in indexdef
        assert "hail_mail" in indexdef
        assert "local_prefix_user" in indexdef
        assert "local_prefix_org" in indexdef


def test_forward_queue_poll_index_exists(empty_db: str) -> None:
    """After 0010 a partial index covers the forward worker's 1s poll
    (status='queued' AND direction='outbound') so it never seq-scans
    emails."""
    _run_alembic(empty_db, ["upgrade", "head"])
    with psycopg.connect(_to_libpq_url(to_sync_url(empty_db))) as conn:
        indexdef = _index_def(conn, "emails_forward_queue_idx")
        assert indexdef is not None, "partial index emails_forward_queue_idx missing"
        assert "WHERE" in indexdef
        assert "queued" in indexdef
        assert "outbound" in indexdef
        assert "created_at" in indexdef


def test_inbound_provider_message_id_dedupe_index_exists(empty_db: str) -> None:
    """After 0010 a partial unique index dedupes inbound rows on
    (organization_id, provider_message_id) — the SES receipt id, which is
    what repeats on redelivery for mail without a Message-ID header. It is
    org-scoped: the same receipt id may land once per organization."""
    _run_alembic(empty_db, ["upgrade", "head"])
    with psycopg.connect(_to_libpq_url(to_sync_url(empty_db))) as conn:
        indexdef = _index_def(conn, "emails_inbound_provider_message_id_uq")
        assert (
            indexdef is not None
        ), "partial unique index emails_inbound_provider_message_id_uq missing"
        assert "UNIQUE" in indexdef
        assert "organization_id" in indexdef
        assert "provider_message_id" in indexdef
        assert "inbound" in indexdef


def test_audit_log_idempotent_on_already_converted_column(empty_db: str) -> None:
    """The original case migration 0004 was designed for: fresh DB after
    deb3692 where call_end_reason landed at 0003 and the audit_log
    conversion was silently dropped. 0004's audit_log guard must still
    convert the column even when call_end_reason already exists.

    This is a regression test for the original 0004 design — left in
    place so a future cleanup doesn't accidentally tighten the guard
    to "audit_log AND no call_end_reason".
    """
    _run_alembic(empty_db, ["upgrade", "0003"])

    # 0003 only touches call_end_reason; audit_log.api_key_id is still
    # TEXT at this point (no migration has converted it yet).
    with psycopg.connect(_to_libpq_url(to_sync_url(empty_db))) as conn:
        assert _column_data_type(conn, "audit_log", "api_key_id") == "text"
        assert _enum_exists(conn, "call_end_reason")

    _run_alembic(empty_db, ["upgrade", "head"])
    _assert_head_schema(empty_db)


def test_head_schema_forbids_email_inbound(empty_db: str) -> None:
    """email_inbound was collapsed into the single ``email`` channel. At head,
    neither channel CHECK may permit it — the value must be gone from the
    schema, not merely unused."""
    _run_alembic(empty_db, ["upgrade", "head"])
    with psycopg.connect(_to_libpq_url(to_sync_url(empty_db))) as conn:
        for name in ("usage_events_channel_check", "account_credits_channel_check"):
            src = _constraint_src(conn, name)
            assert src is not None, f"{name} missing"
            assert (
                "email_inbound" not in src
            ), f"{name} still permits email_inbound: {src}"


def test_email_inbound_rows_converted_on_upgrade(empty_db: str) -> None:
    """A DB that ran 0011 while the channel was live may hold email_inbound
    rows. 0012 must convert them to email *before* narrowing the CHECK — else
    Postgres validates the narrower constraint against the leftover row and
    aborts, re-breaking the deploy."""
    _run_alembic(empty_db, ["upgrade", "0011"])
    org = "11111111-1111-1111-1111-111111111111"
    with psycopg.connect(_to_libpq_url(to_sync_url(empty_db)), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO usage_events (organization_id, channel, units) "
            "VALUES (%s, 'email_inbound', 1)",
            (org,),
        )
        conn.execute(
            "INSERT INTO account_credits "
            "(organization_id, kind, channel, amount_cents, source) "
            "VALUES (%s, 'debit', 'email_inbound', -1, 'test')",
            (org,),
        )

    _run_alembic(empty_db, ["upgrade", "head"])

    with psycopg.connect(_to_libpq_url(to_sync_url(empty_db))) as conn:
        for table in ("usage_events", "account_credits"):
            remaining = conn.execute(
                f"SELECT count(*) FROM {table} WHERE channel = 'email_inbound'"
            ).fetchone()[0]
            assert remaining == 0, f"{table} still has email_inbound rows"
        converted = conn.execute(
            "SELECT count(*) FROM usage_events WHERE channel = 'email'"
        ).fetchone()[0]
        assert (
            converted == 1
        ), "the email_inbound usage_event was not converted to email"
