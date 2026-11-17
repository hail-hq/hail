"""Keep a number registration's files for a resubmit (``verification_kept``).

A rejected number registration can be fixed and filed again
(``POST /verifications/{id}/resubmit``) from what the customer sent. Hail
keeps two things for 30 days after submit, or until the registration is
approved or cancelled or the account closes:

* the files: a ``file_uploads`` row each, new purpose ``verification_kept``,
  with ``verification_id`` (FK to ``carrier_verifications``, ON DELETE
  CASCADE) and ``slot``. ``file_uploads`` already holds every uploaded
  file and its object key; a kept copy is one more kind of that record, so
  it gets a ``purpose`` value and two link columns, not a new table. A
  CHECK ties the two columns to that purpose, and a partial unique index
  allows one file per verification and slot.
* the typed answers (fields, address, document options): no column or
  table here. They go in ``carrier_verifications.provider_refs.application``,
  the same JSON key US texting registrations already use for their saved
  answers, and are cleared by the same close-time step
  (``REGISTRATION_ANSWER_REFS``). Tables checked for them:
  ``file_uploads`` (files only), ``number_offers`` (Hail's own prices),
  ``audit_log`` (kept for years; never personal answers). None holds a
  customer's form answers.

Revision ID: 0081
Revises: 0080
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "file_uploads", sa.Column("verification_id", UUID(as_uuid=True), nullable=True)
    )
    op.add_column("file_uploads", sa.Column("slot", sa.Text(), nullable=True))
    op.create_foreign_key(
        "file_uploads_verification_id_fkey",
        "file_uploads",
        "carrier_verifications",
        ["verification_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("file_uploads_purpose_check", "file_uploads", type_="check")
    op.create_check_constraint(
        "file_uploads_purpose_check",
        "file_uploads",
        "purpose IN ('email_attachment', 'verification_document', "
        "'verification_kept')",
    )
    op.create_check_constraint(
        "file_uploads_kept_link_check",
        "file_uploads",
        "(purpose = 'verification_kept') = "
        "(verification_id IS NOT NULL AND slot IS NOT NULL)",
    )
    op.create_index(
        "file_uploads_verification_slot_uniq",
        "file_uploads",
        ["verification_id", "slot"],
        unique=True,
        postgresql_where=sa.text("verification_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Kept copies cannot exist without these columns; their objects expire
    # by the bucket's lifecycle rule.
    op.execute("DELETE FROM file_uploads WHERE purpose = 'verification_kept'")
    op.drop_index("file_uploads_verification_slot_uniq", table_name="file_uploads")
    op.drop_constraint("file_uploads_kept_link_check", "file_uploads", type_="check")
    op.drop_constraint("file_uploads_purpose_check", "file_uploads", type_="check")
    op.create_check_constraint(
        "file_uploads_purpose_check",
        "file_uploads",
        "purpose IN ('email_attachment', 'verification_document')",
    )
    op.drop_constraint(
        "file_uploads_verification_id_fkey", "file_uploads", type_="foreignkey"
    )
    op.drop_column("file_uploads", "slot")
    op.drop_column("file_uploads", "verification_id")
