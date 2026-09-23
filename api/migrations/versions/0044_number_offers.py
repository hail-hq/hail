"""Persist org-bound carrier quotes and purchase replay references.

Revision ID: 0044
Revises: 0043
"""

import sqlalchemy as sa
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
    # A carrier order exists before the carrier returns a resource id.
    op.execute(
        "ALTER TABLE phone_numbers ALTER COLUMN provider_resource_id DROP NOT NULL"
    )
    # A failed order never owned its number: free the e164 for a new order.
    op.execute("DROP INDEX phone_numbers_e164_live_uniq")
    op.execute(
        "CREATE UNIQUE INDEX phone_numbers_e164_live_uniq ON phone_numbers (e164)"
        " WHERE provisioning_state NOT IN ('released', 'failed')"
    )


def downgrade():
    bind = op.get_bind()
    blocked = bind.execute(
        sa.text(
            "SELECT 1 FROM phone_numbers WHERE provider_resource_id IS NULL"
            " OR (provisioning_state = 'failed' AND EXISTS ("
            "  SELECT 1 FROM phone_numbers o WHERE o.e164 = phone_numbers.e164"
            "  AND o.id <> phone_numbers.id AND o.provisioning_state <> 'released'))"
            " LIMIT 1"
        )
    ).first()
    if blocked is not None:
        raise RuntimeError(
            "cannot downgrade 0044: phone_numbers has rows with a NULL "
            "provider_resource_id, or a failed order sharing an e164 with a "
            "live row. Delete those rows first, then re-run the downgrade."
        )
    op.execute("DROP INDEX phone_numbers_e164_live_uniq")
    op.execute(
        "CREATE UNIQUE INDEX phone_numbers_e164_live_uniq ON phone_numbers (e164)"
        " WHERE provisioning_state <> 'released'"
    )
    op.execute(
        "ALTER TABLE phone_numbers ALTER COLUMN provider_resource_id SET NOT NULL"
    )
    op.execute("DROP TABLE number_offers")
