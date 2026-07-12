"""users.phone_number: org-member phone, written via the Hail API only.

The users table is owned by better-auth (hail-website repo); this column is
named for a migration-free upgrade to better-auth's phone-number plugin.
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone_number", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "phone_number")
