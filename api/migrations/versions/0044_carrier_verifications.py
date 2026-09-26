"""Carrier verifications: state and opaque carrier IDs, no personal data.

Revision ID: 0044
Revises: 0043
"""

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE carrier_verifications (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        organization_id UUID NOT NULL,
        provider TEXT NOT NULL,
        country_code TEXT NOT NULL,
        number_type TEXT NOT NULL,
        subject_type TEXT NOT NULL,
        state TEXT NOT NULL,
        provider_refs JSONB NOT NULL DEFAULT '{}'::jsonb,
        requirements_version TEXT NOT NULL,
        rejection_reason TEXT,
        approved_by UUID,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        submitted_at TIMESTAMPTZ,
        approved_at TIMESTAMPTZ,
        CONSTRAINT carrier_verifications_state_check CHECK (
            state IN ('draft','awaiting_review','submitted','approved','rejected','cancelled')
        ),
        CONSTRAINT carrier_verifications_subject_type_check CHECK (
            subject_type IN ('person','business')
        )
    )""")
    op.execute(
        "CREATE UNIQUE INDEX carrier_verifications_live_uniq ON carrier_verifications "
        "(organization_id, provider, country_code, number_type) "
        "WHERE state NOT IN ('cancelled','rejected')"
    )
    op.execute(
        "CREATE INDEX carrier_verifications_org_idx ON carrier_verifications (organization_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE carrier_verifications")
