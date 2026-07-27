"""Tests for the post-account-closure retention sweep.

Covers: an org past the 12-month cutoff gets its Call/Email content
scrubbed; an org within the window, or with no closure record at all, is
left untouched; audit_log is never touched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from hailhq.core.models import (
    AuditLog,
    Call,
    Contact,
    Email,
    OrgClosure,
    PhoneNumber,
    Sms,
)
from hailhq.core.retention import purge_expired_data
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


async def test_purge_expired_data_flags_unexpected_recording(async_session):
    """recording_s3_key should already be NULL (transcript-only guarantee);
    if it isn't, purge logs a warning and reports it, without clearing it."""
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

    assert summary.calls_with_unexpected_recording == 1
    await session.refresh(call)
    # Not cleared -- purge doesn't mutate a field it wasn't asked to touch.
    assert call.recording_s3_key == "recordings/unexpected.wav"
    assert call.transcript is None
