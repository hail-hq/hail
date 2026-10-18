"""Optional workspace call recording. Existing workspaces keep audio off."""

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE organization_call_settings ADD COLUMN recording_enabled BOOLEAN"
    )
    op.execute("ALTER TABLE agents ADD COLUMN recording_enabled BOOLEAN")
    op.execute(
        "ALTER TABLE organization_call_settings ADD COLUMN recording_retention_days INTEGER "
        "CONSTRAINT organization_recording_retention_days_check "
        "CHECK (recording_retention_days IN (7, 30, 365))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE organization_call_settings DROP COLUMN recording_retention_days"
    )
    op.execute("ALTER TABLE agents DROP COLUMN recording_enabled")
    op.execute("ALTER TABLE organization_call_settings DROP COLUMN recording_enabled")
