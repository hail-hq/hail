"""Upload links and verification documents: ``email_attachment_uploads``
becomes ``file_uploads``.

An agent's shell has no Hail credentials, so a file reaches Hail through a
one-time upload link (``POST /uploads``, then ``PUT /uploads/{id}?token=``).
The same flow carries outbound email attachments and carrier verification
documents.

Tables checked (schema rules: extend the closest table):

* ``email_attachment_uploads`` already holds org-owned uploaded files with
  their S3 key, type and size. A verification document is the same kind of
  record, so this table is extended with a ``purpose`` column and renamed.
  Only hail reads it (the email attachment routes, ``POST /emails`` and the
  attachment GC); hail-website does not.
* ``email_attachments`` holds attachments of received emails, 1:1 with an
  email row. Not an upload; does not fit.
* ``carrier_verifications`` holds the verification itself. Files are never
  stored on it, and a document is uploaded before any verification exists.

Changes:

* Rename the table and its indexes. Add ``purpose`` (CHECK
  ``email_attachment`` or ``verification_document``, default
  ``email_attachment``), ``token_hash`` (sha256 hex of the link token; null
  once used or for a direct upload), ``link_expires_at``, ``uploaded_at``
  and ``sha256``.
* ``size_bytes`` becomes nullable: a link row has no bytes yet. CHECK
  ``uploaded_at IS NULL OR size_bytes IS NOT NULL``.
* Backfill ``uploaded_at = created_at``: every existing row was a direct
  upload.
* The GC index moves to ``(purpose, uploaded_at)`` for uploaded, unused
  rows; a partial index on ``link_expires_at`` serves the sweep of links
  never used.

Downgrade deletes the verification documents and the link rows never used
(their bytes are short-lived or absent), then reverses every change. Their
S3 objects, if any, are left for the bucket lifecycle; they hold no row.

Revision ID: 0073
Revises: 0072
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None

TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.rename_table("email_attachment_uploads", "file_uploads")
    op.execute(
        "ALTER INDEX email_attachment_uploads_org_idx RENAME TO file_uploads_org_idx"
    )
    op.execute("ALTER INDEX email_attachment_uploads_pkey RENAME TO file_uploads_pkey")
    op.drop_index("email_attachment_uploads_gc_idx", table_name="file_uploads")

    op.add_column(
        "file_uploads",
        sa.Column(
            "purpose",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'email_attachment'"),
        ),
    )
    op.add_column("file_uploads", sa.Column("token_hash", sa.Text(), nullable=True))
    op.add_column("file_uploads", sa.Column("link_expires_at", TS, nullable=True))
    op.add_column("file_uploads", sa.Column("uploaded_at", TS, nullable=True))
    op.add_column("file_uploads", sa.Column("sha256", sa.Text(), nullable=True))
    op.execute("UPDATE file_uploads SET uploaded_at = created_at")
    op.alter_column("file_uploads", "size_bytes", nullable=True)

    op.create_check_constraint(
        "file_uploads_purpose_check",
        "file_uploads",
        "purpose IN ('email_attachment', 'verification_document')",
    )
    op.create_check_constraint(
        "file_uploads_uploaded_has_size",
        "file_uploads",
        "uploaded_at IS NULL OR size_bytes IS NOT NULL",
    )
    op.create_index(
        "file_uploads_gc_idx",
        "file_uploads",
        ["purpose", "uploaded_at"],
        postgresql_where=sa.text("uploaded_at IS NOT NULL AND first_used_at IS NULL"),
    )
    op.create_index(
        "file_uploads_link_expiry_idx",
        "file_uploads",
        ["link_expires_at"],
        postgresql_where=sa.text("uploaded_at IS NULL"),
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM file_uploads "
        "WHERE purpose = 'verification_document' OR uploaded_at IS NULL"
    )
    op.drop_index("file_uploads_link_expiry_idx", table_name="file_uploads")
    op.drop_index("file_uploads_gc_idx", table_name="file_uploads")
    op.drop_constraint("file_uploads_uploaded_has_size", "file_uploads")
    op.drop_constraint("file_uploads_purpose_check", "file_uploads")
    op.alter_column("file_uploads", "size_bytes", nullable=False)
    op.drop_column("file_uploads", "sha256")
    op.drop_column("file_uploads", "uploaded_at")
    op.drop_column("file_uploads", "link_expires_at")
    op.drop_column("file_uploads", "token_hash")
    op.drop_column("file_uploads", "purpose")
    op.create_index(
        "email_attachment_uploads_gc_idx",
        "file_uploads",
        ["created_at"],
        postgresql_where=sa.text("first_used_at IS NULL"),
    )
    op.execute("ALTER INDEX file_uploads_pkey RENAME TO email_attachment_uploads_pkey")
    op.execute(
        "ALTER INDEX file_uploads_org_idx RENAME TO email_attachment_uploads_org_idx"
    )
    op.rename_table("file_uploads", "email_attachment_uploads")
