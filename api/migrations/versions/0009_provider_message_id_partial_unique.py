"""Make emails.provider_message_id uniqueness outbound-only.

The global UNIQUE on provider_message_id (from 0005) is correct for
outbound sends — SES returns a unique MessageId per send. But inbound
fan-out writes the *same* SES receipt messageId on one row per recipient
org, so a multi-org delivery collides on the global constraint and 500s,
rolling back every org's row. Inbound idempotency is already enforced by
`emails_inbound_message_id_uq` on (organization_id, message_id), so the
provider_message_id constraint only needs to guard outbound dedup.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("emails_provider_message_id_key", "emails", type_="unique")
    op.create_index(
        "emails_provider_message_id_outbound_uq",
        "emails",
        ["provider_message_id"],
        unique=True,
        postgresql_where=sa.text(
            "direction = 'outbound' AND provider_message_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("emails_provider_message_id_outbound_uq", table_name="emails")
    op.create_unique_constraint(
        "emails_provider_message_id_key", "emails", ["provider_message_id"]
    )
