"""Let ``sip_connections.default_region`` be empty.

Hail reads each PBX caller with the country of the number that was called,
then of the connection's other numbers, and tries this column last. Most
connections leave it empty.

Tables checked first: this is a column of ``sip_connections`` (the
connection's own setting); no other table fits.

Downgrade fills an empty country with ``US`` before it restores NOT NULL;
the older code then reads those connections' national callers as US, so a
rollback misreads callers of connections in other countries.

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
