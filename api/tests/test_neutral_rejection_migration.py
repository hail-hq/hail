"""0064: carrier-written rejection reasons move to the audit log and the row
gets Hail's wording. Superadmin reasons and Hail's own text stay."""

import importlib.util
import uuid
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from hailhq.api.number_orders import REGISTRATION_REJECTED
from hailhq.core.models import AuditLog, CarrierVerification
from hailhq.core.providers.verification import base
from sqlalchemy import select, text

spec = importlib.util.spec_from_file_location(
    "neutral_rejection_migration",
    Path(__file__).parents[1] / "migrations/versions/0064_neutral_rejection_reasons.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_wording_matches_the_code() -> None:
    assert migration.DETAILS_REJECTED == base.DETAILS_REJECTED
    assert migration.BRAND_REJECTED == base.BRAND_REJECTED
    assert migration.CAMPAIGN_REJECTED == base.CAMPAIGN_REJECTED
    assert migration.NOT_ACCEPTED == base.NOT_ACCEPTED
    assert migration.VALUE_NOT_ACCEPTED == base.VALUE_NOT_ACCEPTED
    assert migration.REGISTRATION_REJECTED == REGISTRATION_REJECTED


def test_is_hail_text() -> None:
    assert migration.is_hail_text(
        "The carrier declined the details. Start again. Requirements: a, b."
    )
    assert migration.is_hail_text(
        f"document: {base.VALUE_NOT_ACCEPTED}; {base.NOT_ACCEPTED}"
    )
    assert not migration.is_hail_text("Name does not match the ID")
    assert not migration.is_hail_text(
        f"{base.NOT_ACCEPTED}; Address in Portugal required"
    )


def _row(org, reason, **over) -> CarrierVerification:
    return CarrierVerification(
        id=uuid.uuid4(),
        organization_id=org,
        provider=over.pop("provider", "twilio"),
        purpose=over.pop("purpose", "number"),
        country_code="GB",
        number_type=over.pop("number_type", "mobile"),
        subject_type="person",
        state=over.pop("state", "rejected"),
        provider_refs=over.pop("provider_refs", {}),
        requirements_version="1",
        rejection_reason=reason,
        **over,
    )


async def _run(db, step) -> None:
    async with db.begin() as connection:

        def go(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            step()

        await connection.run_sync(go)


async def test_upgrade_moves_carrier_text_and_downgrade_restores_it(
    db, async_session, org_and_key
) -> None:
    from datetime import datetime, timezone

    org, _, _ = org_and_key
    # One row per way a reason was written before this release.
    polled = _row(org, "Bundle failed: passport expired")
    sweeper = _row(org, "Address in Portugal required", number_type="national")
    registration = _row(
        org,
        "Proof of address is blurry",
        provider="didww",
        number_type="local",
        approved_at=datetime.now(timezone.utc),
    )
    brand = _row(
        org,
        "TAX_ID mismatch",
        purpose="sms",
        number_type="local",
        provider_refs={"brand_sid": "BN1"},
    )
    campaign = _row(
        org,
        "Website lacks business information",
        purpose="sms",
        number_type="toll_free",
        provider_refs={"brand_id": "B1", "campaign_id": "C1"},
        state="cancelled",
    )
    admin = _row(org, "ID unreadable, please upload a clearer scan")
    hail = _row(
        org,
        "The carrier declined the details. Start again. Requirements: r1.",
        provider="telnyx",
    )
    async_session.add_all([polled, sweeper, registration, brand, campaign, admin, hail])
    await async_session.flush()
    async_session.add_all(
        [
            # A system rejection wrote a reason; a superadmin one did not.
            AuditLog(
                organization_id=org,
                action="verification.reject",
                resource_type="carrier_verification",
                resource_id=sweeper.id,
                actor_kind="system",
                payload={"rejected_by": None, "reason": sweeper.rejection_reason},
            ),
            AuditLog(
                organization_id=org,
                action="verification.reject",
                resource_type="carrier_verification",
                resource_id=admin.id,
                actor_kind="superadmin",
                payload={"rejected_by": str(uuid.uuid4())},
            ),
        ]
    )
    await async_session.commit()
    raw = {
        r.id: r.rejection_reason
        for r in (polled, sweeper, registration, brand, campaign, admin, hail)
    }

    await _run(db, migration.upgrade)
    await _run(db, migration.upgrade)  # idempotent

    async def reasons():
        rows = (
            await async_session.execute(
                select(
                    CarrierVerification.id, CarrierVerification.rejection_reason
                ).execution_options(populate_existing=True)
            )
        ).all()
        return dict(rows)

    async def moved():
        return (
            (
                await async_session.execute(
                    select(AuditLog).where(
                        AuditLog.payload["migration"].astext == "0064"
                    )
                )
            )
            .scalars()
            .all()
        )

    got = await reasons()
    assert got[polled.id] == base.DETAILS_REJECTED
    assert got[sweeper.id] == base.DETAILS_REJECTED
    assert got[registration.id] == REGISTRATION_REJECTED
    assert got[brand.id] == base.BRAND_REJECTED
    assert got[campaign.id] == base.CAMPAIGN_REJECTED
    assert got[admin.id] == raw[admin.id]
    assert got[hail.id] == raw[hail.id]
    audits = await moved()
    assert sorted(a.payload["carrier_reason"] for a in audits) == sorted(
        raw[r.id] for r in (polled, sweeper, registration, brand, campaign)
    )
    for a in audits:
        assert a.actor_kind == "system" and a.organization_id == org
        assert a.payload["reason"] == got[a.resource_id]

    await _run(db, migration.downgrade)
    assert await reasons() == raw
    assert await moved() == []
    # The audit rows written before the migration are untouched.
    assert (
        await async_session.execute(text("SELECT count(*) FROM audit_log"))
    ).scalar_one() == 2
