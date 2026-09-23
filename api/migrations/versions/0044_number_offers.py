"""Persist org-bound carrier quotes and purchase replay references.

Revision ID: 0044
Revises: 0043
"""

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE number_offers (
        id UUID PRIMARY KEY,
        organization_id UUID NOT NULL,
        offer JSONB NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        number_id UUID
    )""")
    op.execute(
        "CREATE INDEX ix_number_offers_organization_id ON number_offers (organization_id)"
    )


def downgrade():
    op.execute("DROP TABLE number_offers")
