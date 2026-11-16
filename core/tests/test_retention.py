"""Tests for the post-account-closure retention sweep.

Covers: an org past the 12-month cutoff gets its Call/Email content
scrubbed; an org within the window, or with no closure record at all, is
left untouched; audit_log is never touched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from hailhq.core.models import (
    AuditLog,
    Call,
    CarrierVerification,
    Contact,
    Email,
    OrgClosure,
    PhoneNumber,
    Sms,
)
from hailhq.core.retention import clear_closed_org_registrations, purge_expired_data
from sqlalchemy import select


async def _make_sms(session, organization_id: uuid.UUID) -> Sms:
    pn = PhoneNumber(
        organization_id=organization_id,
        e164=f"+1415666{uuid.uuid4().int % 10000:04d}",
        country_code="US",
        number_type="local",
        provider="twilio",
        provider_resource_id=f"PN-{uuid.uuid4()}",
        provisioning_state="active",
    )
    session.add(pn)
    await session.flush()

    sms = Sms(
        organization_id=organization_id,
        from_number_id=pn.id,
        from_e164=pn.e164,
        to_e164="+14155551234",
        direction="outbound",
        status="sent",
        body="your order shipped",
    )
    session.add(sms)
    await session.flush()
    return sms


async def _make_call(session, organization_id: uuid.UUID, *, transcript=None) -> Call:
    pn = PhoneNumber(
        organization_id=organization_id,
        e164=f"+1415555{uuid.uuid4().int % 10000:04d}",
        country_code="US",
        number_type="local",
        provider="twilio",
        provider_resource_id=f"PN-{uuid.uuid4()}",
        provisioning_state="active",
    )
    session.add(pn)
    await session.flush()

    call = Call(
        organization_id=organization_id,
        from_number_id=pn.id,
        from_e164=pn.e164,
        to_e164="+14155551234",
        voice_config={"stt": "deepgram", "tts": "cartesia"},
        status="completed",
        end_reason="normal_hangup",
        transcript=(
            transcript if transcript is not None else [{"role": "user", "text": "hi"}]
        ),
    )
    session.add(call)
    await session.flush()
    return call


async def _make_email(session, organization_id: uuid.UUID) -> Email:
    email = Email(
        organization_id=organization_id,
        direction="inbound",
        from_address="alice@example.com",
        to_addresses=["ops@example.com"],
        subject="hi",
        body_text="hello there",
        body_html="<p>hello there</p>",
        status="received",
        provider="ses",
        raw_s3_key="raw/abc123",
    )
    session.add(email)
    await session.flush()
    return email


async def test_purge_expired_data_scrubs_org_past_cutoff(async_session):
    org_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    session = async_session

    session.add(
        OrgClosure(
            organization_id=org_id,
            closed_at=now - timedelta(days=400),
            source="hail_website",
        )
    )
    await session.flush()

    call = await _make_call(session, org_id)
    sms = await _make_sms(session, org_id)
    email = await _make_email(session, org_id)
    await session.commit()

    summary = await purge_expired_data(session, now)

    assert summary.organizations_purged == [org_id]
    assert summary.calls_scrubbed == 1
    assert summary.sms_scrubbed == 1
    assert summary.emails_scrubbed == 1
    assert summary.calls_with_unexpected_recording == 0

    await session.refresh(call)
    await session.refresh(sms)
    await session.refresh(email)
    assert call.transcript is None
    assert sms.body == ""
    assert email.body_text == ""
    assert email.body_html is None
    assert email.raw_s3_key is None
    # Row shell stays intact.
    assert sms.to_e164 == "+14155551234"
    assert email.to_addresses == ["ops@example.com"]
    assert email.status == "received"


async def test_purge_expired_data_deletes_contacts_of_closed_org_only(async_session):
    """The contacts-v2 design specced organization_id as an FK with ON
    DELETE CASCADE; no real FK is possible across the two databases (see
    module docstring), so this sweep is the explicit replacement — contacts
    of a closed-past-cutoff org are hard-deleted, contacts of a live org
    are left untouched."""
    closed_org = uuid.uuid4()
    live_org = uuid.uuid4()
    now = datetime.now(timezone.utc)
    session = async_session

    session.add(
        OrgClosure(
            organization_id=closed_org,
            closed_at=now - timedelta(days=400),
            source="hail_website",
        )
    )
    closed_contact = Contact(
        organization_id=closed_org, name="Closed Org Contact", phone_e164="+14155551234"
    )
    live_contact = Contact(
        organization_id=live_org, name="Live Org Contact", phone_e164="+14155555678"
    )
    session.add(closed_contact)
    session.add(live_contact)
    await session.commit()

    summary = await purge_expired_data(session, now)

    assert summary.organizations_purged == [closed_org]
    assert summary.contacts_deleted == 1

    remaining = (await session.execute(select(Contact.organization_id))).scalars().all()
    assert remaining == [live_org]


async def test_purge_expired_data_leaves_org_within_window_untouched(async_session):
    org_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    session = async_session

    session.add(
        OrgClosure(
            organization_id=org_id,
            closed_at=now - timedelta(days=30),  # closed recently, still in window
            source="hail_website",
        )
    )
    await session.flush()

    call = await _make_call(session, org_id)
    email = await _make_email(session, org_id)
    await session.commit()

    summary = await purge_expired_data(session, now)

    assert summary.organizations_purged == []
    assert summary.calls_scrubbed == 0
    assert summary.emails_scrubbed == 0

    await session.refresh(call)
    await session.refresh(email)
    assert call.transcript is not None
    assert email.body_text == "hello there"


async def test_purge_expired_data_leaves_org_with_no_closure_untouched(async_session):
    org_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    session = async_session

    # No OrgClosure row at all for this org.
    call = await _make_call(session, org_id)
    email = await _make_email(session, org_id)
    await session.commit()

    summary = await purge_expired_data(session, now)

    assert summary.organizations_purged == []

    await session.refresh(call)
    await session.refresh(email)
    assert call.transcript is not None
    assert email.body_text == "hello there"


async def test_purge_expired_data_never_touches_audit_log(async_session):
    org_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    session = async_session

    session.add(
        OrgClosure(
            organization_id=org_id,
            closed_at=now - timedelta(days=400),
            source="hail_website",
        )
    )
    audit = AuditLog(
        organization_id=org_id,
        action="call.create",
        resource_type="call",
        payload={"to": "+14155551234"},
    )
    session.add(audit)
    await session.commit()

    await purge_expired_data(session, now)

    await session.refresh(audit)
    assert audit.payload == {"to": "+14155551234"}


async def test_purge_expired_data_deletes_optional_recording(
    async_session, monkeypatch
):
    """Closed-account cleanup deletes optional audio before clearing its key."""
    from unittest.mock import Mock

    storage = Mock()
    monkeypatch.setattr("hailhq.core.recordings.recording_storage", lambda: storage)
    org_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    session = async_session

    session.add(
        OrgClosure(
            organization_id=org_id,
            closed_at=now - timedelta(days=400),
            source="hail_website",
        )
    )
    await session.flush()
    call = await _make_call(session, org_id)
    call.recording_s3_key = "recordings/unexpected.wav"
    await session.commit()

    summary = await purge_expired_data(session, now)

    assert summary.calls_with_unexpected_recording == 0
    await session.refresh(call)
    assert call.recording_s3_key is None
    storage.delete_object.assert_called_once()
    assert call.transcript is None


async def test_purge_expired_data_deletes_closed_org_evidence_only(
    async_session, monkeypatch
):
    """Public consent screenshots stop working once the account's data goes."""
    closed, open_org = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    async_session.add(
        OrgClosure(
            organization_id=closed,
            closed_at=now - timedelta(days=400),
            source="hail_website",
        )
    )
    async_session.add(
        OrgClosure(
            organization_id=open_org,
            closed_at=now - timedelta(days=10),
            source="hail_website",
        )
    )
    await async_session.commit()
    deleted: list[uuid.UUID] = []

    async def fake_delete(org_id):
        deleted.append(org_id)
        return 2

    monkeypatch.setattr("hailhq.core.retention.delete_org_evidence", fake_delete)
    summary = await purge_expired_data(async_session, now)
    assert deleted == [closed]
    assert summary.evidence_files_deleted == 2


async def test_purge_expired_data_keeps_deleting_evidence_after_one_org_fails(
    async_session, monkeypatch
):
    """One org's S3 failure never leaves the other orgs' links public."""

    first, second = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    for org in (first, second):
        async_session.add(
            OrgClosure(
                organization_id=org,
                closed_at=now - timedelta(days=400),
                source="hail_website",
            )
        )
    await async_session.commit()
    tried: list[uuid.UUID] = []

    async def fake_delete(org_id):
        tried.append(org_id)
        if len(tried) == 1:
            raise RuntimeError("AccessDenied")
        return 1

    monkeypatch.setattr("hailhq.core.retention.delete_org_evidence", fake_delete)
    with pytest.raises(RuntimeError, match="1 org"):
        await purge_expired_data(async_session, now)
    assert sorted(tried) == sorted([first, second])


async def test_purge_expired_data_deletes_closed_org_verification_uploads(
    async_session, monkeypatch
):
    """Verification documents of a closed org go, bucket folder and rows."""
    from hailhq.core.models import FileUpload
    from sqlalchemy import select

    closed = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async_session.add(
        OrgClosure(
            organization_id=closed,
            closed_at=now - timedelta(days=400),
            source="hail_website",
        )
    )
    for purpose in ("verification_document", "email_attachment"):
        async_session.add(
            FileUpload(
                organization_id=closed,
                purpose=purpose,
                filename="f",
                content_type="image/jpeg",
                size_bytes=1,
                s3_key="k",
                uploaded_at=now,
            )
        )
    await async_session.commit()
    deleted: list[uuid.UUID] = []

    async def fake_delete(org_id):
        deleted.append(org_id)
        return 3

    monkeypatch.setattr(
        "hailhq.core.retention.delete_org_verification_uploads", fake_delete
    )
    summary = await purge_expired_data(async_session, now)
    assert deleted == [closed]
    assert summary.verification_uploads_deleted == 3
    purposes = (await async_session.execute(select(FileUpload.purpose))).scalars().all()
    assert purposes == ["email_attachment"]


async def test_an_evidence_failure_does_not_skip_the_verification_uploads(
    async_session, monkeypatch
):
    """Each bucket folder is deleted on its own; one failure skips neither."""
    org = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async_session.add(
        OrgClosure(
            organization_id=org,
            closed_at=now - timedelta(days=400),
            source="hail_website",
        )
    )
    await async_session.commit()
    uploads_tried: list[uuid.UUID] = []

    async def evidence_fails(org_id):
        raise RuntimeError("AccessDenied")

    async def uploads_delete(org_id):
        uploads_tried.append(org_id)
        return 1

    monkeypatch.setattr("hailhq.core.retention.delete_org_evidence", evidence_fails)
    monkeypatch.setattr(
        "hailhq.core.retention.delete_org_verification_uploads", uploads_delete
    )
    with pytest.raises(RuntimeError, match="1 org"):
        await purge_expired_data(async_session, now)
    assert uploads_tried == [org]


# What a filed US texting registration keeps in provider_refs: carrier ids
# and billing keys (kept), and copies of the customer's answers (deleted).
_KEPT_REFS = {
    "brand_sid": "BN1",
    "campaign_sid": "CM1",
    "campaign_filed_at": "2026-11-01T00:00:00+00:00",
    "brand_reused": True,
    "subject_type": "person",
}
_ANSWER_REFS = {
    "application": {"fields": {"first_name": "Ada", "tax_id": "12-3456789"}},
    "campaign": {"description": "Order updates", "contact_email": "ada@example.com"},
    "subject_name": "Ada Lovelace",
}


def _verification(org, **over) -> CarrierVerification:
    return CarrierVerification(
        organization_id=org,
        provider=over.pop("provider", "twilio"),
        country_code=over.pop("country_code", "US"),
        number_type="local",
        subject_type="person",
        purpose=over.pop("purpose", "sms"),
        subject_name=over.pop("subject_name", "Ada Lovelace"),
        state=over.pop("state", "approved"),
        provider_refs=over.pop("provider_refs", {**_KEPT_REFS, **_ANSWER_REFS}),
        rejection_reason=over.pop("rejection_reason", None),
        requirements_version="1",
        **over,
    )


async def test_closed_org_registrations_lose_their_answers_at_close(async_session):
    now = datetime.now(timezone.utc)
    closed, open_org, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # Closed just now: no 12-month wait.
    async_session.add(
        OrgClosure(organization_id=closed, closed_at=now, source="hail_website")
    )
    rows = {
        "approved": _verification(closed),
        "rejected": _verification(
            closed,
            provider="telnyx",
            state="rejected",
            rejection_reason="The carrier could not match Ada Lovelace.",
        ),
        # Two live number verifications: their names stay (a nameless-live
        # unique index), the reason goes.
        "number_a": _verification(
            closed,
            purpose="number",
            country_code="GB",
            subject_name="Ada Lovelace",
            provider_refs={"bundle_sid": "BU1"},
            rejection_reason="Earlier: blurry passport",
        ),
        "number_b": _verification(
            closed,
            purpose="number",
            country_code="GB",
            subject_name="Grace Hopper",
            provider_refs={"bundle_sid": "BU2"},
        ),
        "number_ended": _verification(
            closed,
            purpose="number",
            country_code="DE",
            state="cancelled",
            provider_refs={"bundle_sid": "BU3"},
        ),
        "open": _verification(open_org, rejection_reason="Fix the website"),
        "other": _verification(other, provider="telnyx"),
    }
    async_session.add_all(rows.values())
    await async_session.commit()

    assert await clear_closed_org_registrations(async_session, now) == 4
    # Safe to run twice: nothing left to clear.
    assert await clear_closed_org_registrations(async_session, now) == 0

    for row in rows.values():
        await async_session.refresh(row)
    for name in ("approved", "rejected"):
        row = rows[name]
        assert row.provider_refs == _KEPT_REFS, name
        assert row.subject_name is None and row.rejection_reason is None, name
    # State and dates stay for billing.
    assert rows["approved"].state == "approved"
    assert rows["rejected"].state == "rejected"
    assert rows["number_a"].subject_name == "Ada Lovelace"
    assert rows["number_a"].rejection_reason is None
    assert rows["number_a"].provider_refs == {"bundle_sid": "BU1"}
    assert rows["number_b"].subject_name == "Grace Hopper"
    assert rows["number_ended"].subject_name is None
    assert rows["number_ended"].provider_refs == {"bundle_sid": "BU3"}
    # An open org and any other org keep everything.
    for name in ("open", "other"):
        row = rows[name]
        assert row.provider_refs == {**_KEPT_REFS, **_ANSWER_REFS}, name
        assert row.subject_name == "Ada Lovelace", name
    assert rows["open"].rejection_reason == "Fix the website"


async def test_closed_org_clear_waits_for_closed_at_and_takes_one_org(async_session):
    now = datetime.now(timezone.utc)
    later, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async_session.add_all(
        [
            OrgClosure(
                organization_id=later,
                closed_at=now + timedelta(days=1),
                source="hail_website",
            ),
            OrgClosure(organization_id=first, closed_at=now, source="hail_website"),
            OrgClosure(organization_id=second, closed_at=now, source="hail_website"),
        ]
    )
    rows = [_verification(org) for org in (later, first, second)]
    async_session.add_all(rows)
    await async_session.commit()

    assert await clear_closed_org_registrations(async_session, now, first) == 1
    for row in rows:
        await async_session.refresh(row)
    assert [("application" in r.provider_refs) for r in rows] == [True, False, True]
