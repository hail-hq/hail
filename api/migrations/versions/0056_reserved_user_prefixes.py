"""Reserve the ``noreply`` and ``forwarder`` hail-mail user prefixes at the DB.

``noreply+<org>@<base>`` is the sender of every forward and system mail.
The API refuses to mint it (schemas.RESERVED_USER_PREFIXES); this CHECK
makes the rule hold for any writer. ``NOT VALID`` so a self-host that
already holds such a row keeps running — the constraint applies to new
and updated rows only. Hail Cloud has no such row.

Revision ID: 0056
Revises: 0055
"""

from __future__ import annotations

from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels = None
depends_on = None

_NAME = "email_domains_user_prefix_not_reserved"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE email_domains ADD CONSTRAINT {_NAME} CHECK "
        "(local_prefix_user IS NULL OR local_prefix_user NOT IN ('noreply','forwarder')) "
        "NOT VALID"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE email_domains DROP CONSTRAINT {_NAME}")
