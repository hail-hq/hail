"""Account close: numbers released and registrations cancelled
(hailhq.api.org_close)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from hailhq.api.org_close import close_org_carrier_items
from hailhq.api.routes.verifications import sweep_verifications
from hailhq.core.config import SELF_HOSTED_ORG_ID
from hailhq.core.models import (
    AccountCredit,
    CarrierVerification,
    OrgClosure,
    PhoneNumber,
)
from hailhq.core.providers.verification.telnyx_sms import (
    TelnyxSmsRegistrationProvider,
)
from hailhq.core.providers.voice.base import CarrierRequestError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


class FakeVerifier:
    """Records what it discarded; ``fail`` maps a bundle id to an error."""

    def __init__(self) -> None:
        self.discarded: list[dict] = []
        self.submitted: list[dict] = []
        self.fail: dict[str, Exception] = {}

    async def discard(self, refs):
        error = self.fail.get(refs.get("bundle_sid"))
        if error is not None:
            raise error
        self.discarded.append(dict(refs))

    async def check(self, refs):
        return []

    async def submit(self, refs):
        self.submitted.append(refs)


@pytest.fixture()
def verifier() -> FakeVerifier:
    return FakeVerifier()


def _registry(verifier: FakeVerifier):
    return lambda name, purpose="number": verifier


async def _close(session: AsyncSession, org: uuid.UUID) -> None:
    session.add(
        OrgClosure(
            organization_id=org,
            closed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            source="hail_website",
        )
    )
    await session.commit()


async def _number(session: AsyncSession, org: uuid.UUID, **kw) -> PhoneNumber:
    pn = PhoneNumber(
        organization_id=org,
        e164=f"+1415{uuid.uuid4().int % 10**7:07d}",
        country_code=kw.pop("country_code", "US"),
        number_type=kw.pop("number_type", "local"),
        provider="twilio",
        provider_resource_id=f"PN{uuid.uuid4().hex}",
        provisioning_state=kw.pop("state", "active"),
        **kw,
    )
    session.add(pn)
    await session.commit()
    return pn


async def _verification(
    session: AsyncSession, org: uuid.UUID, state: str, bundle: str, **kw
) -> CarrierVerification:
    row = CarrierVerification(
        organization_id=org,
        provider="twilio",
        country_code=kw.pop("country_code", "US"),
        number_type=kw.pop("number_type", "local"),
        subject_type="business",
        subject_name=kw.pop("subject_name", f"Acme {bundle}"),
        purpose=kw.pop("purpose", "number"),
        state=state,
        provider_refs={"bundle_sid": bundle},
        requirements_version="1",
        **kw,
    )
    session.add(row)
    await session.commit()
    return row


async def _run(session, verifier, lk=None, org=None):
    return await close_org_carrier_items(
        session, datetime.now(timezone.utc), _registry(verifier), lk, org
    )


async def test_closed_org_numbers_released_and_registrations_cancelled(
    async_session, voice_provider_mock, verifier
) -> None:
    org, other = uuid.uuid4(), uuid.uuid4()
    await _close(async_session, org)
    numbers = [await _number(async_session, org) for _ in range(2)]
    kept = await _number(async_session, other)
    approved = await _verification(async_session, org, "approved", "B1")
    submitted = await _verification(
        async_session, org, "submitted", "B2", country_code="GB"
    )
    open_row = await _verification(async_session, other, "approved", "B3")

    counts = await _run(async_session, verifier)

    assert counts.released == 2 and counts.cancelled == 2
    assert counts.failed == counts.refused == counts.waiting == 0
    released = {c.args[0] for c in voice_provider_mock.release_number.await_args_list}
    assert released == {n.provider_resource_id for n in numbers}
    for number in numbers:
        await async_session.refresh(number)
        assert number.provisioning_state == "released"
    for row in (approved, submitted):
        await async_session.refresh(row)
        assert row.state == "cancelled" and row.cancelled_at is not None
    assert sorted(d["bundle_sid"] for d in verifier.discarded) == ["B1", "B2"]
    # Open orgs are untouched.
    await async_session.refresh(kept)
    await async_session.refresh(open_row)
    assert kept.provisioning_state == "active" and open_row.state == "approved"
    # No money moved for the closed org.
    rows = await async_session.scalar(
        select(func.count())
        .select_from(AccountCredit)
        .where(AccountCredit.organization_id == org)
    )
    assert rows == 0

    # A second run does nothing.
    voice_provider_mock.release_number.reset_mock()
    verifier.discarded.clear()
    again = await _run(async_session, verifier)
    assert again.as_dict() == dict.fromkeys(again.as_dict(), 0)
    voice_provider_mock.release_number.assert_not_awaited()
    assert verifier.discarded == []


async def test_one_failed_release_does_not_stop_the_rest_and_retries(
    async_session, voice_provider_mock, verifier
) -> None:
    org = uuid.uuid4()
    await _close(async_session, org)
    bad = await _number(async_session, org)
    good = await _number(async_session, org)
    covering = await _verification(async_session, org, "approved", "B1")

    async def release(resource_id):
        if resource_id == bad.provider_resource_id:
            raise CarrierRequestError(503)

    voice_provider_mock.release_number.side_effect = release
    counts = await _run(async_session, verifier)

    assert counts.released == 1 and counts.failed == 1
    # The verification covers the unreleased number: it waits.
    assert counts.waiting == 1 and verifier.discarded == []
    await async_session.refresh(good)
    await async_session.refresh(bad)
    await async_session.refresh(covering)
    assert good.provisioning_state == "released"
    assert bad.provisioning_state == "active"
    assert covering.state == "approved"

    voice_provider_mock.release_number.side_effect = None
    counts = await _run(async_session, verifier)
    assert counts.released == 1 and counts.cancelled == 1
    await async_session.refresh(bad)
    await async_session.refresh(covering)
    assert bad.provisioning_state == "released" and covering.state == "cancelled"


async def test_a_final_carrier_refusal_is_stored_and_not_retried(
    async_session, voice_provider_mock, verifier
) -> None:
    org = uuid.uuid4()
    await _close(async_session, org)
    number = await _number(async_session, org)
    row = await _verification(async_session, org, "approved", "B9", country_code="GB")
    voice_provider_mock.release_number.side_effect = CarrierRequestError(422)
    verifier.fail["B9"] = CarrierRequestError(400)

    counts = await _run(async_session, verifier)

    assert counts.refused == 2 and counts.failed == 0
    await async_session.refresh(number)
    await async_session.refresh(row)
    assert number.provisioning_state == "active"
    assert number.provisioning_metadata["release_refused"]["status"] == 422
    assert row.state == "approved"
    assert row.provider_refs["close_refused"]["status"] == 400

    voice_provider_mock.release_number.reset_mock()
    again = await _run(async_session, verifier)
    assert again.as_dict() == dict.fromkeys(again.as_dict(), 0)
    voice_provider_mock.release_number.assert_not_awaited()


async def test_a_retryable_cancel_failure_is_tried_again(
    async_session, voice_provider_mock, verifier
) -> None:
    org = uuid.uuid4()
    await _close(async_session, org)
    row = await _verification(async_session, org, "approved", "B5")
    verifier.fail["B5"] = CarrierRequestError(503)

    counts = await _run(async_session, verifier)
    assert counts.failed == 1 and counts.refused == 0
    await async_session.refresh(row)
    assert row.state == "approved" and "close_refused" not in row.provider_refs

    verifier.fail.clear()
    counts = await _run(async_session, verifier)
    assert counts.cancelled == 1
    await async_session.refresh(row)
    assert row.state == "cancelled"


async def test_a_failed_campaign_delete_keeps_the_ids_and_is_retried(
    async_session, voice_provider_mock
) -> None:
    """The real Telnyx plug-in: a 5xx on the campaign delete keeps the row
    and its carrier ids, and the next pass deletes it again. A 404 there
    (Telnyx no longer holds it) counts as done."""
    org = uuid.uuid4()
    await _close(async_session, org)
    campaign_status = {"code": 500}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if "/10dlc/campaign/" in request.url.path:
            return httpx.Response(campaign_status["code"], json={})
        return httpx.Response(200, json={})

    telnyx = TelnyxSmsRegistrationProvider(
        api_key="k",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.telnyx.com/v2",
        ),
    )
    row = CarrierVerification(
        organization_id=org,
        provider="telnyx",
        country_code="US",
        number_type="local",
        subject_type="business",
        subject_name="Acme",
        purpose="sms",
        state="approved",
        provider_refs={"brand_id": "B1", "campaign_id": "C1"},
        requirements_version="1",
    )
    async_session.add(row)
    await async_session.commit()

    async def run():
        return await close_org_carrier_items(
            async_session, datetime.now(timezone.utc), lambda *_: telnyx, None
        )

    counts = await run()
    assert counts.failed == 1 and counts.cancelled == counts.refused == 0
    assert calls == ["DELETE /v2/10dlc/campaign/C1", "DELETE /v2/10dlc/brand/B1"]
    await async_session.refresh(row)
    assert row.state == "approved" and row.cancelled_at is None
    assert row.provider_refs["campaign_id"] == "C1"
    assert row.provider_refs["brand_id"] == "B1"
    assert "close_refused" not in row.provider_refs

    campaign_status["code"] = 404
    calls.clear()
    counts = await run()
    assert counts.cancelled == 1 and counts.failed == 0
    assert calls == ["DELETE /v2/10dlc/campaign/C1", "DELETE /v2/10dlc/brand/B1"]
    await async_session.refresh(row)
    assert row.state == "cancelled" and "campaign_id" not in row.provider_refs


async def test_one_org_only_when_named(
    async_session, voice_provider_mock, verifier
) -> None:
    named, other = uuid.uuid4(), uuid.uuid4()
    for org in (named, other):
        await _close(async_session, org)
    a = await _number(async_session, named)
    b = await _number(async_session, other)

    counts = await _run(async_session, verifier, org=named)

    assert counts.released == 1
    await async_session.refresh(a)
    await async_session.refresh(b)
    assert a.provisioning_state == "released" and b.provisioning_state == "active"


async def test_no_closure_row_means_not_closed(
    async_session, voice_provider_mock, verifier
) -> None:
    """The self-hosted org has no organizations row: only org_closures
    closes an org."""
    from hailhq.core.config import SELF_HOSTED_ORG_ID

    number = await _number(async_session, SELF_HOSTED_ORG_ID)
    counts = await _run(async_session, verifier)
    assert counts.released == 0
    await async_session.refresh(number)
    assert number.provisioning_state == "active"


async def test_sweep_files_nothing_for_a_closed_org(async_session, verifier) -> None:
    org = uuid.uuid4()
    await _close(async_session, org)
    row = await _verification(async_session, org, "awaiting_review", "B7")
    row.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    await async_session.commit()

    counts = await sweep_verifications(async_session, _registry(verifier))

    assert counts["submitted"] == 0 and verifier.submitted == []
    await async_session.refresh(row)
    assert row.state == "awaiting_review"


async def test_a_brand_kept_for_a_next_filing_is_discarded(
    async_session, voice_provider_mock, verifier
) -> None:
    org = uuid.uuid4()
    await _close(async_session, org)
    cancelled_at = datetime.now(timezone.utc) - timedelta(days=3)
    row = await _verification(
        async_session,
        org,
        "cancelled",
        "B4",
        purpose="sms",
        cancelled_at=cancelled_at,
    )
    row.provider_refs = {"kept_brand_id": "BR1"}
    await async_session.commit()

    counts = await _run(async_session, verifier)

    assert counts.cancelled == 1 and verifier.discarded == [{"brand_id": "BR1"}]
    await async_session.refresh(row)
    assert row.provider_refs == {} and row.cancelled_at == cancelled_at
    assert (await _run(async_session, verifier)).cancelled == 0


async def test_a_number_on_a_texting_registration_is_released_first(
    async_session, voice_provider_mock, verifier
) -> None:
    org = uuid.uuid4()
    await _close(async_session, org)
    row = await _verification(async_session, org, "approved", "B6", purpose="sms")
    number = await _number(async_session, org, sms_registration_id=row.id)

    counts = await _run(async_session, verifier)

    assert counts.released == 1 and counts.cancelled == 1
    await async_session.refresh(number)
    await async_session.refresh(row)
    assert number.provisioning_state == "released"
    assert number.sms_registration_id is None
    assert row.state == "cancelled"


async def test_a_failed_order_number_not_given_back_is_released_or_refused(
    async_session, voice_provider_mock, verifier
) -> None:
    org = uuid.uuid4()
    await _close(async_session, org)
    given = await _number(
        async_session,
        org,
        state="failed",
        released_at=datetime.now(timezone.utc),
        provisioning_metadata={"unreleased_resource_id": "PNGIVE"},
    )
    refused = await _number(
        async_session,
        org,
        state="failed",
        country_code="GB",
        provisioning_metadata={"unreleased_resource_id": "PNREFUSE"},
    )
    covering = await _verification(
        async_session, org, "approved", "B1", country_code="GB"
    )

    async def release(resource_id):
        if resource_id == "PNREFUSE":
            raise CarrierRequestError(422)

    voice_provider_mock.release_number.side_effect = release
    counts = await _run(async_session, verifier)

    assert counts.released == 1 and counts.refused == 1
    await async_session.refresh(given)
    await async_session.refresh(refused)
    await async_session.refresh(covering)
    assert "unreleased_resource_id" not in given.provisioning_metadata
    assert refused.provisioning_metadata["release_refused"]["status"] == 422
    # The refusal is final: the registration it blocked is cancelled.
    assert covering.state == "cancelled" and counts.waiting == 0


async def test_the_self_hosted_org_is_never_closed(
    async_session, voice_provider_mock, verifier
) -> None:
    await _close(async_session, SELF_HOSTED_ORG_ID)
    number = await _number(async_session, SELF_HOSTED_ORG_ID)
    row = await _verification(async_session, SELF_HOSTED_ORG_ID, "approved", "B1")

    counts = await _run(async_session, verifier)

    assert counts.as_dict() == dict.fromkeys(counts.as_dict(), 0)
    voice_provider_mock.release_number.assert_not_awaited()
    await async_session.refresh(number)
    await async_session.refresh(row)
    assert number.provisioning_state == "active" and row.state == "approved"
