"""contacts: manual org contacts (phone and/or email).

organizations lives in the website DB — no FK (see migration 0001, 0023).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contacts",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("phone_e164", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        # created_by intentionally has no FK — users is website-owned (see 0001/0029);
        # keys and rows may outlive their creator.
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "phone_e164 IS NOT NULL OR email IS NOT NULL",
            name="contacts_phone_or_email",
        ),
    )
    op.create_index(
        "contacts_org_phone_key",
        "contacts",
        ["organization_id", "phone_e164"],
        unique=True,
        postgresql_where=sa.text("phone_e164 IS NOT NULL"),
    )
    # Case-sensitive index; correctness relies on write-time full lowercasing
    # in schemas.py's _normalize_contact_email (ContactCreate/ContactPatch).
    op.create_index(
        "contacts_org_email_key",
        "contacts",
        ["organization_id", "email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    op.create_index("contacts_org_idx", "contacts", ["organization_id"])


def downgrade() -> None:
    op.drop_table("contacts")
