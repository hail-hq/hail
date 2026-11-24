"""Upload links for US texting consent evidence (purpose ``sms_evidence``).

An agent that has only the MCP cannot send a multipart request to
``POST /verifications/sms/evidence``. An upload link (``POST /uploads``)
lets its shell send the bytes with a plain PUT, and the PUT returns the
same public evidence link.

Tables checked: ``file_uploads`` already holds every upload link (token
hash, expiry, the link sweep), so a link for evidence is one more
``purpose`` value on it, not a new table. The row lives only while the link
is unused: the PUT that uses it deletes it, and the link sweep deletes an
unused one. Evidence files themselves stay untracked, as before.

Revision ID: 0091
Revises: 0090
"""

from __future__ import annotations

from alembic import op

revision: str = "0091"
down_revision: str | None = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("file_uploads_purpose_check", "file_uploads", type_="check")
    op.create_check_constraint(
        "file_uploads_purpose_check",
        "file_uploads",
        "purpose IN ('email_attachment', 'verification_document', "
        "'verification_kept', 'sms_evidence')",
    )


def downgrade() -> None:
    # Only unused links have rows; their PUT would fail after a downgrade.
    op.execute("DELETE FROM file_uploads WHERE purpose = 'sms_evidence'")
    op.drop_constraint("file_uploads_purpose_check", "file_uploads", type_="check")
    op.create_check_constraint(
        "file_uploads_purpose_check",
        "file_uploads",
        "purpose IN ('email_attachment', 'verification_document', "
        "'verification_kept')",
    )
