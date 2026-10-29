"""US texting registrations: one "campaign filed" key, and the new wording.

1. ``carrier_verifications.provider_refs.campaign_filed_at`` says a US
   texting campaign is filed, whatever key the carrier keeps it under. The
   API writes it from now on. This sets it on ``purpose = 'sms'`` rows that
   already hold a campaign (``campaign_id`` or ``campaign_sid``, a non-empty
   string) and lack it, to ``submitted_at`` (else ``updated_at``).
2. ``rejection_reason`` of ``purpose = 'sms'`` rows that hold Hail's old
   brand or campaign wording ("The carrier rejected ...") gets the new
   wording, the same text new rejections get.

Tables checked (no new table, no new column): ``carrier_verifications``
already holds both values.

Idempotent: a row that has the key, or the new wording, is skipped.
Downgrade puts the old wording back. It keeps ``campaign_filed_at``: the
key is harmless to older code, which never reads it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None

# The wording before and after, copied here so this migration never changes
# when the code does. A test checks NEW_* match the code.
OLD_BRAND_REJECTED = (
    "The carrier rejected the brand registration. Check the business or "
    "person details and start again."
)
OLD_CAMPAIGN_REJECTED = (
    "The carrier rejected the campaign. Check the use case, description, "
    "sample messages and links, and start again."
)
NEW_BRAND_REJECTED = (
    "The 10DLC registry rejected the registration. Check the business or "
    "person details and start again."
)
NEW_CAMPAIGN_REJECTED = (
    "The registration was rejected in review. Check the use case, "
    "description, sample messages and links, and start again."
)
_CAMPAIGN_KEYS = ("campaign_id", "campaign_sid")


def _filed(key: str) -> str:
    return (
        f"(jsonb_typeof(provider_refs->'{key}') = 'string' "
        f"AND provider_refs->>'{key}' <> '')"
    )


def _reword(old: str, new: str) -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE carrier_verifications SET rejection_reason = :new "
            "WHERE purpose = 'sms' AND rejection_reason = :old"
        ),
        {"old": old, "new": new},
    )


def upgrade() -> None:
    filed = " OR ".join(_filed(k) for k in _CAMPAIGN_KEYS)
    # A timestamptz in jsonb is an ISO 8601 string, as the API writes it.
    op.execute(
        "UPDATE carrier_verifications SET provider_refs = provider_refs || "
        "jsonb_build_object('campaign_filed_at', "
        "COALESCE(submitted_at, updated_at)) "
        f"WHERE purpose = 'sms' AND ({filed}) "
        "AND provider_refs->'campaign_filed_at' IS NULL"
    )
    _reword(OLD_BRAND_REJECTED, NEW_BRAND_REJECTED)
    _reword(OLD_CAMPAIGN_REJECTED, NEW_CAMPAIGN_REJECTED)


def downgrade() -> None:
    _reword(NEW_BRAND_REJECTED, OLD_BRAND_REJECTED)
    _reword(NEW_CAMPAIGN_REJECTED, OLD_CAMPAIGN_REJECTED)
