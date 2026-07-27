"""email_attachment_uploads table — pre-send, reusable outbound attachments.

Uploaded via POST /email-attachments, referenced by EmailCreate.attachment_ids.
Rows never used by a send are garbage-collected 24h after upload (see
hailhq.core.email_attachment_gc); used rows are kept indefinitely. Distinct
from email_attachments (0007), which is always 1:1 with an inbound email.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_attachment_uploads",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("first_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "email_attachment_uploads_gc_idx",
        "email_attachment_uploads",
        ["created_at"],
        postgresql_where=sa.text("first_used_at IS NULL"),
    )
    op.create_index(
        "email_attachment_uploads_org_idx",
        "email_attachment_uploads",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "email_attachment_uploads_org_idx", table_name="email_attachment_uploads"
    )
    op.drop_index(
        "email_attachment_uploads_gc_idx", table_name="email_attachment_uploads"
    )
    op.drop_table("email_attachment_uploads")
