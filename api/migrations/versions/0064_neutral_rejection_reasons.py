"""Neutral rejection reasons on carrier verifications.

``carrier_verifications.rejection_reason`` is shown to the customer. Rows
rejected before this release may hold the carrier's own text. For each such
row this migration:

1. writes an ``audit_log`` row (action ``verification.reject``, actor
   ``system``) whose ``payload.carrier_reason`` keeps the carrier's text, and
2. replaces ``rejection_reason`` with Hail's own wording, the same wording
   new rejections get.

Reasons a superadmin typed are left alone. A superadmin rejection always
wrote an ``audit_log`` row ``verification.reject`` for the verification whose
payload has no ``reason`` key; every system rejection that wrote one has a
``reason`` key, and a rejection read from the carrier wrote none. Reasons that
are already Hail's own wording are left alone too.

Idempotent: a rewritten row has this migration's audit row and holds Hail's
wording, so a second run skips it.
Downgrade puts the carrier's text back from the audit rows this migration
wrote (``payload.migration = '0064'``) and deletes them.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

MIGRATION = "0064"

# Hail's wording for new rejections, copied here so this migration never
# changes when the code does. A test checks they match the code.
DETAILS_REJECTED = "The carrier rejected the details. Start again."
BRAND_REJECTED = (
    "The carrier rejected the brand registration. Check the business or "
    "person details and start again."
)
CAMPAIGN_REJECTED = (
    "The carrier rejected the campaign. Check the use case, description, "
    "sample messages and links, and start again."
)
REGISTRATION_REJECTED = (
    "the carrier rejected the end-user registration; "
    "start a new verification with corrected details"
)
NOT_ACCEPTED = (
    "The carrier did not accept the details. Check your entries and try again."
)
VALUE_NOT_ACCEPTED = "The carrier did not accept this value."

# Text Hail itself has written into rejection_reason, now or before.
_HAIL_TEXT = {
    DETAILS_REJECTED,
    BRAND_REJECTED,
    CAMPAIGN_REJECTED,
    REGISTRATION_REJECTED,
    NOT_ACCEPTED,
    VALUE_NOT_ACCEPTED,
    "the carrier rejected the campaign",
    "the carrier refused the brand",
    "the carrier rejected the end-user registration",
    "The carrier did not accept the details.",
    "Not accepted by the carrier.",
    "This requirement is missing.",
    "The carrier declined this value.",
}
# Hail's own text that ends with a list of the form's field names.
_HAIL_PREFIXES = (
    "The carrier declined the details. Start again.",
    "The carrier's approval expired. Start again.",
)


def is_hail_text(reason: str) -> bool:
    """True when every ``; ``-separated part is Hail's own wording, alone or
    after a ``field: `` label."""
    if reason in _HAIL_TEXT or reason.startswith(_HAIL_PREFIXES):
        return True
    for part in reason.split("; "):
        part = part.strip()
        if part in _HAIL_TEXT:
            continue
        if ": " in part and part.split(": ", 1)[1] in _HAIL_TEXT:
            continue
        return False
    return True


def neutral_reason(purpose: str, approved_at, provider_refs: dict | None) -> str:
    """Hail's wording for a carrier rejection of this kind of verification."""
    refs = provider_refs or {}
    if purpose == "sms":
        if refs.get("campaign_sid") or refs.get("campaign_id"):
            return CAMPAIGN_REJECTED
        return BRAND_REJECTED
    if approved_at is not None:
        # Approved, then the carrier refused the registration of a bought
        # number.
        return REGISTRATION_REJECTED
    return DETAILS_REJECTED


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("""
            SELECT v.id, v.organization_id, v.purpose, v.approved_at,
                   v.provider_refs, v.rejection_reason
            FROM carrier_verifications v
            WHERE v.rejection_reason IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM audit_log a
                WHERE a.action = 'verification.reject'
                  AND a.resource_id = v.id
                  AND (NOT (a.payload ? 'reason')
                       OR a.payload->>'migration' = '0064')
              )
            """)).all()
    for row in rows:
        if is_hail_text(row.rejection_reason):
            continue
        reason = neutral_reason(row.purpose, row.approved_at, row.provider_refs)
        bind.execute(
            sa.text("""
                INSERT INTO audit_log (organization_id, actor_kind, action,
                                       resource_type, resource_id, payload)
                VALUES (:org, 'system', 'verification.reject',
                        'carrier_verification', :id, CAST(:payload AS JSONB))
                """),
            {
                "org": row.organization_id,
                "id": row.id,
                "payload": json.dumps(
                    {
                        "rejected_by": None,
                        "reason": reason,
                        "carrier_reason": row.rejection_reason,
                        "migration": MIGRATION,
                    }
                ),
            },
        )
        bind.execute(
            sa.text(
                "UPDATE carrier_verifications SET rejection_reason = :reason "
                "WHERE id = :id"
            ),
            {"reason": reason, "id": row.id},
        )


def downgrade() -> None:
    op.execute("""
        UPDATE carrier_verifications v
        SET rejection_reason = a.payload->>'carrier_reason'
        FROM audit_log a
        WHERE a.action = 'verification.reject'
          AND a.payload->>'migration' = '0064'
          AND a.resource_id = v.id
        """)
    op.execute(
        "DELETE FROM audit_log WHERE action = 'verification.reject' "
        "AND payload->>'migration' = '0064'"
    )
