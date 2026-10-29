"""0068: US texting rows that hold a campaign get ``campaign_filed_at``, and
Hail's old brand and campaign rejection wording gets the new text."""

import importlib.util
import uuid
from datetime import datetime, timezone
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from hailhq.core.models import CarrierVerification
from hailhq.core.providers.verification import base
from sqlalchemy import select

spec = importlib.util.spec_from_file_location(
    "sms_campaign_filed_backfill",
    Path(__file__).parents[1]
    / "migrations/versions/0068_sms_campaign_filed_backfill.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_new_wording_matches_the_code() -> None:
    assert migration.NEW_BRAND_REJECTED == base.BRAND_REJECTED
    assert migration.NEW_CAMPAIGN_REJECTED == base.CAMPAIGN_REJECTED


async def _run(db, step) -> None:
    async with db.begin() as connection:

        def go(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            step()

        await connection.run_sync(go)


def _row(org, **over) -> CarrierVerification:
    return CarrierVerification(
        id=uuid.uuid4(),
        organization_id=org,
        provider=over.pop("provider", "telnyx"),
        purpose=over.pop("purpose", "sms"),
        country_code="US",
        number_type="local",
        subject_type="person",
        state=over.pop("state", "cancelled"),
        provider_refs=over.pop("provider_refs", {}),
        requirements_version="1",
        **over,
    )


async def test_upgrade_marks_filed_campaigns_and_rewords(
    db, async_session, org_and_key
) -> None:
    org, _, _ = org_and_key
    at = datetime(2026, 10, 28, 13, 15, tzinfo=timezone.utc)
    telnyx = _row(
        org, state="submitted", provider_refs={"campaign_id": "C1"}, submitted_at=at
    )
    twilio = _row(org, provider="twilio", provider_refs={"campaign_sid": "QE1"})
    stamped = _row(
        org, provider_refs={"campaign_id": "C2", "campaign_filed_at": "kept"}
    )
    brand_only = _row(org, provider_refs={"brand_id": "B1"})
    empty_id = _row(org, provider_refs={"campaign_id": ""})
    number = _row(
        org, purpose="number", provider="twilio", provider_refs={"campaign_id": "X"}
    )
    brand_no = _row(
        org, state="rejected", rejection_reason=migration.OLD_BRAND_REJECTED
    )
    campaign_no = _row(
        org, state="rejected", rejection_reason=migration.OLD_CAMPAIGN_REJECTED
    )
    other_no = _row(org, state="rejected", rejection_reason="Superadmin text")
    rows = [telnyx, twilio, stamped, brand_only, empty_id, number]
    rows += [brand_no, campaign_no, other_no]
    async_session.add_all(rows)
    await async_session.commit()

    await _run(db, migration.upgrade)
    await _run(db, migration.upgrade)  # idempotent

    async def read():
        got = (
            await async_session.execute(
                select(
                    CarrierVerification.id,
                    CarrierVerification.provider_refs,
                    CarrierVerification.rejection_reason,
                ).execution_options(populate_existing=True)
            )
        ).all()
        return {r.id: (r.provider_refs, r.rejection_reason) for r in got}

    got = await read()
    filed = datetime.fromisoformat(got[telnyx.id][0]["campaign_filed_at"])
    assert filed == at
    assert datetime.fromisoformat(got[twilio.id][0]["campaign_filed_at"])
    assert got[stamped.id][0]["campaign_filed_at"] == "kept"
    for row in (brand_only, empty_id, number):
        assert "campaign_filed_at" not in got[row.id][0]
    assert got[brand_no.id][1] == base.BRAND_REJECTED
    assert got[campaign_no.id][1] == base.CAMPAIGN_REJECTED
    assert got[other_no.id][1] == "Superadmin text"

    await _run(db, migration.downgrade)
    got = await read()
    assert got[brand_no.id][1] == migration.OLD_BRAND_REJECTED
    assert got[campaign_no.id][1] == migration.OLD_CAMPAIGN_REJECTED
