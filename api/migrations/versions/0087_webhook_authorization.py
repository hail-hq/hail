"""Add encrypted receiver authorization to webhook subscriptions.

Reuse webhook_subscriptions, which already owns delivery targets and signing
secrets. agent_http_tools holds agent tool credentials, not event subscriptions;
org_provider_config holds provider credentials, not customer receiver keys.
The website auth tables hold identities and sessions, not delivery targets.

Revision ID: 0087
Revises: 0086
"""

import sqlalchemy as sa
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webhook_subscriptions",
        sa.Column("authorization_encrypted", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("webhook_subscriptions", "authorization_encrypted")
