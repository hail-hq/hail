"""Carrier verifications: add the 'submitting' state.

Revision ID: 0045
Revises: 0044
"""

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE carrier_verifications "
        "DROP CONSTRAINT carrier_verifications_state_check"
    )
    op.execute(
        "ALTER TABLE carrier_verifications "
        "ADD CONSTRAINT carrier_verifications_state_check CHECK ("
        "state IN ('draft','awaiting_review','submitting','submitted',"
        "'approved','rejected','cancelled'))"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE carrier_verifications SET state = 'awaiting_review' WHERE state = 'submitting'"
    )
    op.execute(
        "ALTER TABLE carrier_verifications "
        "DROP CONSTRAINT carrier_verifications_state_check"
    )
    op.execute(
        "ALTER TABLE carrier_verifications "
        "ADD CONSTRAINT carrier_verifications_state_check CHECK ("
        "state IN ('draft','awaiting_review','submitted','approved','rejected','cancelled'))"
    )
