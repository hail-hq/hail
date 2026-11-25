"""Let ``sip_connections.default_region`` be empty.

When set, it names the phone system's country and Hail reads callers with it
first. Empty (most connections): the country of the number that was called,
then of the connection's other numbers.

Tables checked first: this is a column of ``sip_connections`` (the
connection's own setting); no other table fits.

Downgrade fills an empty country with the country of one of the
connection's numbers, ``US`` when it has none, before it restores NOT NULL.

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
    op.execute("""
        UPDATE sip_connections c
           SET default_region = COALESCE(
               (SELECT n.country_code FROM phone_numbers n
                 WHERE n.sip_connection_id = c.id
                   AND n.country_code ~ '^[A-Z]{2}$'
                 ORDER BY n.created_at LIMIT 1),
               'US')
         WHERE c.default_region IS NULL
        """)
    op.alter_column("sip_connections", "default_region", nullable=False)
