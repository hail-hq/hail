"""Make custom sender domains globally unique across all orgs.

Previously ``email_domains`` only enforced uniqueness within an org
(``email_domains_org_domain_unique``).  Two different orgs could each
register ``acme.com``, letting the second org ride the real owner's SES
verification and — now that inbound exists — intercept their mail.

This migration adds a partial unique index on ``domain`` filtered to
``kind = 'custom'``, enforcing one custom row per domain globally.  The
existing org-scoped UniqueConstraint is kept (it's still needed for
hail_mail rows and is harmless/subsumed for custom rows).

NOTE: If the live DB already contains cross-org duplicate custom domain
rows, this index creation will fail with a unique-constraint violation.
An operator must manually deduplicate those rows before running this
migration.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "email_domains_custom_domain_global_uq",
        "email_domains",
        ["domain"],
        unique=True,
        postgresql_where=sa.text("kind = 'custom'"),
    )


def downgrade() -> None:
    op.drop_index("email_domains_custom_domain_global_uq", table_name="email_domains")
