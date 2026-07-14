"""org_provider_config: allow multiple providers per layer, one active."""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "org_provider_config_org_layer_key", "org_provider_config", type_="unique"
    )
    op.create_unique_constraint(
        "org_provider_config_org_layer_provider_key",
        "org_provider_config",
        ["organization_id", "layer", "provider"],
    )
    op.add_column(
        "org_provider_config",
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    # Every existing layer has exactly one row today → it is the active one.
    op.execute("UPDATE org_provider_config SET is_active = true")
    op.create_index(
        "org_provider_config_one_active_idx",
        "org_provider_config",
        ["organization_id", "layer"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index(
        "org_provider_config_one_active_idx", table_name="org_provider_config"
    )
    op.drop_column("org_provider_config", "is_active")
    op.drop_constraint(
        "org_provider_config_org_layer_provider_key",
        "org_provider_config",
        type_="unique",
    )
    op.create_unique_constraint(
        "org_provider_config_org_layer_key",
        "org_provider_config",
        ["organization_id", "layer"],
    )
