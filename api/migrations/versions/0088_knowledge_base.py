"""Knowledge base: documents, their searchable pieces, per-workspace limits,
and the agent switch.

* ``pgvector`` for the piece embeddings. On Supabase the extension goes into
  the ``extensions`` schema (which is on the role's search path); elsewhere
  into the default schema.
* ``file_uploads`` takes a new purpose, ``knowledge_document``: the bytes of
  a document stay in the upload row and its object, like every other file.
* ``agents.knowledge``: the agent answers from the knowledge base.

Tables checked:

* ``file_uploads``: holds the bytes and their upload lifecycle, for every
  kind of file. A document adds ingest state (status, error, attempts,
  lease) and owns many pieces; putting that job state on ``file_uploads``
  would load every other kind of upload with columns it never uses. So a
  document is its own row, linked to its upload with ON DELETE CASCADE.
* ``email_attachments``: one file on one email. Not a document.
* ``organizations``: owned by hail-website's migrations; hail never writes
  it. ``organization_call_settings`` holds call behaviour the console edits;
  the knowledge limits are set by Hail staff only, so they are kept apart
  where no console write path can raise them. ``org_provider_config`` holds
  provider keys; ``platform_flags`` is platform-wide. None fits per-workspace
  limits set by staff, so ``organization_knowledge`` is its own table.
* Nothing holds text passages with embeddings, so ``knowledge_pieces`` is
  new, linked to its document with ON DELETE CASCADE.

No vector index: every search filters on one organization and scans its
pieces exactly (``knowledge_pieces_org_idx``), which is exact and costs no
index storage at the early-access limits.

Revision ID: 0088
Revises: 0087
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0088"
down_revision: str | None = "0087"
branch_labels = None
depends_on = None

_TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'extensions') THEN
            CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
          ELSE
            CREATE EXTENSION IF NOT EXISTS vector;
          END IF;
        END
        $$;
        """)
    op.drop_constraint("file_uploads_purpose_check", "file_uploads", type_="check")
    op.create_check_constraint(
        "file_uploads_purpose_check",
        "file_uploads",
        "purpose IN ('email_attachment', 'verification_document', "
        "'verification_kept', 'knowledge_document')",
    )
    op.add_column(
        "agents",
        sa.Column(
            "knowledge", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False
        ),
    )
    op.create_table(
        "organization_knowledge",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "max_documents", sa.Integer(), server_default=sa.text("10"), nullable=False
        ),
        sa.Column(
            "max_file_bytes",
            sa.Integer(),
            server_default=sa.text("10485760"),
            nullable=False,
        ),
        sa.Column(
            "max_pieces", sa.Integer(), server_default=sa.text("1000"), nullable=False
        ),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "max_documents BETWEEN 1 AND 1000",
            name="organization_knowledge_documents_check",
        ),
        sa.CheckConstraint(
            "max_file_bytes BETWEEN 1 AND 26214400",
            name="organization_knowledge_file_bytes_check",
        ),
        sa.CheckConstraint(
            "max_pieces BETWEEN 1 AND 100000",
            name="organization_knowledge_pieces_check",
        ),
    )
    op.create_table(
        "knowledge_documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "file_upload_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "file_uploads.id",
                name="knowledge_documents_file_upload_id_fkey",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "piece_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("claimed_at", _TS, nullable=True),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("file_upload_id", name="knowledge_documents_upload_uq"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed')",
            name="knowledge_documents_status_check",
        ),
    )
    op.create_index(
        "knowledge_documents_org_created_idx",
        "knowledge_documents",
        ["organization_id", "created_at", "id"],
    )
    op.create_index(
        "knowledge_documents_queue_idx",
        "knowledge_documents",
        ["created_at"],
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )
    # The vector type is found through the search path (``extensions`` is on
    # Supabase's), like the queries that use it.
    op.execute("""
        CREATE TABLE knowledge_pieces (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id uuid NOT NULL
                CONSTRAINT knowledge_pieces_document_id_fkey
                REFERENCES knowledge_documents (id) ON DELETE CASCADE,
            organization_id uuid NOT NULL,
            ordinal integer NOT NULL,
            text text NOT NULL,
            page integer,
            csv_row integer,
            embedding vector(384) NOT NULL,
            terms tsvector NOT NULL,
            CONSTRAINT knowledge_pieces_document_ordinal_uq
                UNIQUE (document_id, ordinal),
            CONSTRAINT knowledge_pieces_locator_check
                CHECK (page IS NULL OR csv_row IS NULL)
        )
        """)
    op.create_index("knowledge_pieces_org_idx", "knowledge_pieces", ["organization_id"])
    op.create_index(
        "knowledge_pieces_terms_idx",
        "knowledge_pieces",
        ["terms"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_table("knowledge_pieces")
    op.drop_table("knowledge_documents")
    op.drop_table("organization_knowledge")
    op.drop_column("agents", "knowledge")
    op.execute("DELETE FROM file_uploads WHERE purpose = 'knowledge_document'")
    op.drop_constraint("file_uploads_purpose_check", "file_uploads", type_="check")
    op.create_check_constraint(
        "file_uploads_purpose_check",
        "file_uploads",
        "purpose IN ('email_attachment', 'verification_document', "
        "'verification_kept')",
    )
    # The extension stays: dropping it needs every user of it gone, and it
    # costs nothing unused.
