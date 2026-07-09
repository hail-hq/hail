"""org_provider_config — per-org BYO provider keys for voice pipeline layers.

Cloud-console feature: encrypted (Fernet, HAIL_PROVIDER_SECRET_KEY) provider
API keys plus non-secret params (voice_id, model, base_url) per org and
layer (llm/tts/stt). organizations lives in the website DB — no FK.
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_provider_config",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("layer", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=True),
        sa.Column("key_last4", sa.Text(), nullable=True),
        sa.Column("key_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "params", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "fallback_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "layer IN ('llm','tts','stt')", name="org_provider_config_layer_check"
        ),
        sa.UniqueConstraint(
            "organization_id", "layer", name="org_provider_config_org_layer_key"
        ),
    )
    op.create_index(
        "org_provider_config_org_idx", "org_provider_config", ["organization_id"]
    )


def downgrade() -> None:
    op.drop_table("org_provider_config")
