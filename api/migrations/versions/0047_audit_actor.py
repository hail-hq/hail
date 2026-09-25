"""Audit log: who acted (actor_user_id) and as what (actor_kind).

actor_kind: api_key | user | superadmin | system. Existing rows stay NULL.

Revision ID: 0047
Revises: 0046
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_log", sa.Column("actor_user_id", UUID(as_uuid=True), nullable=True)
    )
    op.add_column("audit_log", sa.Column("actor_kind", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "actor_kind")
    op.drop_column("audit_log", "actor_user_id")
