"""Organization-configurable call duration.

Revision ID: 0043
Revises: 0042
"""

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE organization_call_settings (
        organization_id UUID PRIMARY KEY,
        max_duration_seconds INTEGER NOT NULL CHECK (max_duration_seconds BETWEEN 60 AND 3600)
    )""")


def downgrade() -> None:
    op.execute("DROP TABLE organization_call_settings")
