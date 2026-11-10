"""Migration 0073: ``email_attachment_uploads`` becomes ``file_uploads``."""

from __future__ import annotations

import uuid

import psycopg
from hailhq.core.db import to_sync_url
from hailhq.core.testing.fixtures import database_url  # noqa: F401

from .test_migrations import (  # noqa: F401
    _constraint_exists,
    _index_def,
    _run_alembic,
    _table_exists,
    _to_libpq_url,
    empty_db,
)


def _connect(url: str) -> psycopg.Connection:
    return psycopg.connect(_to_libpq_url(to_sync_url(url)), autocommit=True)


def test_upgrade_keeps_rows_as_uploaded_email_attachments(
    empty_db: str,  # noqa: F811
) -> None:
    _run_alembic(empty_db, ["upgrade", "0072"])
    row_id, org = uuid.uuid4(), uuid.uuid4()
    with _connect(empty_db) as conn:
        conn.execute(
            "INSERT INTO email_attachment_uploads "
            "(id, organization_id, filename, content_type, size_bytes, s3_key) "
            "VALUES (%s, %s, 'a.pdf', 'application/pdf', 3, 'k')",
            (row_id, org),
        )

    _run_alembic(empty_db, ["upgrade", "0073"])
    with _connect(empty_db) as conn:
        assert not _table_exists(conn, "email_attachment_uploads")
        purpose, uploaded_at, created_at = conn.execute(
            "SELECT purpose, uploaded_at, created_at FROM file_uploads WHERE id = %s",
            (row_id,),
        ).fetchone()
        assert purpose == "email_attachment"
        assert uploaded_at == created_at
        assert _constraint_exists(conn, "file_uploads_purpose_check")
        assert _constraint_exists(conn, "file_uploads_uploaded_has_size")
        assert "uploaded_at IS NULL" in _index_def(conn, "file_uploads_link_expiry_idx")
        assert _index_def(conn, "file_uploads_org_idx") is not None
        # A link row: no bytes yet.
        conn.execute(
            "INSERT INTO file_uploads (organization_id, purpose, filename, "
            "content_type, s3_key, token_hash, link_expires_at) VALUES "
            "(%s, 'verification_document', 'p.jpg', 'image/jpeg', 'k2', 'h', now())",
            (org,),
        )
        try:
            conn.execute(
                "INSERT INTO file_uploads (organization_id, purpose, filename, "
                "content_type, s3_key) VALUES (%s, 'other', 'x', 'a/b', 'k3')",
                (org,),
            )
        except psycopg.errors.CheckViolation:
            pass
        else:  # pragma: no cover
            raise AssertionError("unknown purpose was accepted")
        try:
            conn.execute(
                "INSERT INTO file_uploads (organization_id, filename, "
                "content_type, s3_key, uploaded_at) VALUES "
                "(%s, 'x', 'a/b', 'k4', now())",
                (org,),
            )
        except psycopg.errors.CheckViolation:
            pass
        else:  # pragma: no cover
            raise AssertionError("an uploaded row without a size was accepted")

    _run_alembic(empty_db, ["downgrade", "0072"])
    with _connect(empty_db) as conn:
        assert not _table_exists(conn, "file_uploads")
        rows = conn.execute("SELECT id FROM email_attachment_uploads").fetchall()
        assert rows == [(row_id,)]
        assert _index_def(conn, "email_attachment_uploads_gc_idx") is not None
        assert _index_def(conn, "email_attachment_uploads_org_idx") is not None

    _run_alembic(empty_db, ["upgrade", "head"])
