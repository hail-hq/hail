"""Let ``sip_connections.default_region`` be empty until the first number.

A connection is created without a country; the first number to pass its
ownership check fills it with that number's country, and a country already
set never changes by itself (PATCH changes it). Hail reads national caller
numbers with it.

Tables checked first: this is a column of ``sip_connections`` (the
connection's own setting); no other table fits.

Downgrade fills an empty country with ``US`` before it restores NOT NULL.
After a downgrade and a new upgrade those connections keep ``US``: PATCH
``default_region`` corrects one.

Revision ID: 0093
Revises: 0092
"""

from __future__ import annotations

from alembic import op

revision: str = "0093"
down_revision: str | None = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("sip_connections", "default_region", nullable=True)


def downgrade() -> None:
    op.execute(
        "UPDATE sip_connections SET default_region = 'US' WHERE default_region IS NULL"
    )
    op.alter_column("sip_connections", "default_region", nullable=False)
