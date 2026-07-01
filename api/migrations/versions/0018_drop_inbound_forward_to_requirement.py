"""Drop the inbound forward_to requirement so receiving can run webhook-only.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("email_domains_inbound_action", "email_domains", type_="check")


def downgrade() -> None:
    op.create_check_constraint(
        "email_domains_inbound_action",
        "email_domains",
        "NOT inbound_enabled OR forward_to IS NOT NULL",
    )
