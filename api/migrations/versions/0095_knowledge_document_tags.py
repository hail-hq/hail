"""Store reusable typed tag values and document assignments per workspace.

``knowledge_documents`` holds a document's identity and ingest state.
``file_uploads`` holds its original file, and ``knowledge_pieces`` holds indexed
text. The API and Better Auth schemas have no reusable types and values that
these documents can share. The tag catalog and assignments are separate rows
so every document can share a value and retain a manual clear for one type.

Revision ID: 0095
Revises: 0094
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0095"
down_revision: str | None = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "knowledge_documents_org_id_uq",
        "knowledge_documents",
        ["organization_id", "id"],
    )
    op.create_table(
        "knowledge_tag_values",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("type", sa.Text(), primary_key=True),
        sa.Column("value_key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "type ~ '^[a-z][a-z0-9_]{0,31}$'", name="knowledge_tag_values_type_check"
        ),
        sa.CheckConstraint(
            "char_length(value) BETWEEN 1 AND 255 AND char_length(value_key) > 0",
            name="knowledge_tag_values_length_check",
        ),
    )
    op.create_table(
        "knowledge_document_tags",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("type", sa.Text(), primary_key=True),
        sa.Column("value_key", sa.Text(), nullable=True),
        sa.Column("is_manual", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["knowledge_documents.organization_id", "knowledge_documents.id"],
            name="knowledge_document_tags_document_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "type", "value_key"],
            [
                "knowledge_tag_values.organization_id",
                "knowledge_tag_values.type",
                "knowledge_tag_values.value_key",
            ],
            name="knowledge_document_tags_value_fkey",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "type ~ '^[a-z][a-z0-9_]{0,31}$'", name="knowledge_document_tags_type_check"
        ),
        sa.CheckConstraint(
            "is_manual OR value_key IS NOT NULL",
            name="knowledge_document_tags_manual_clear_check",
        ),
    )
    op.create_index(
        "knowledge_document_tags_value_idx",
        "knowledge_document_tags",
        ["organization_id", "type", "value_key"],
        postgresql_where=sa.text("value_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_document_tags")
    op.drop_table("knowledge_tag_values")
    op.drop_constraint(
        "knowledge_documents_org_id_uq", "knowledge_documents", type_="unique"
    )
