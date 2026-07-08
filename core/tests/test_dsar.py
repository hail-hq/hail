"""Tests for DSAR (Data Subject Access Request) tooling.

Covers: lookup finds matching calls/emails/suppressions across channels;
delete clears content but preserves suppression + audit rows; export
returns a JSON-able structure.
"""

from __future__ import annotations

import json
import uuid

from hailhq.core.dsar import (
    delete_recipient_data,
    export_recipient_data,
    lookup_recipient,
)
from hailhq.core.models import AuditLog, Call, Email, PhoneNumber, Sms, Suppression

PHONE = "+14155551234"
EMAIL_ADDR = "alice@example.com"


async def _make_call(session, organization_id, *, to_e164=PHONE) -> Call:
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
        to_e164=to_e164,
        voice_config={"stt": "deepgram", "tts": "cartesia"},
        status="completed",
        end_reason="normal_hangup",
        transcript=[{"role": "user", "text": "hi"}],
    )
    session.add(call)
    await session.flush()
    return call


async def _make_sms(session, organization_id, *, to_e164=PHONE) -> Sms:
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
        to_e164=to_e164,
        direction="outbound",
        status="sent",
        body="your order shipped",
    )
    session.add(sms)
    await session.flush()
    return sms


async def _make_email(session, organization_id, *, to=None, cc=None, bcc=None) -> Email:
    email = Email(
        organization_id=organization_id,
        direction="inbound",
        from_address="bob@example.com",
        to_addresses=to or [EMAIL_ADDR],
        cc_addresses=cc,
        bcc_addresses=bcc,
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


async def test_lookup_recipient_finds_calls_and_emails_two_channels(async_session):
    org_id = uuid.uuid4()
    call = await _make_call(async_session, org_id, to_e164=PHONE)
    email = await _make_email(async_session, org_id, to=[EMAIL_ADDR])
    await async_session.commit()

    call_record = await lookup_recipient(async_session, PHONE)
    assert [c.id for c in call_record.calls] == [call.id]
    assert call_record.emails == []

    email_record = await lookup_recipient(async_session, EMAIL_ADDR)
    assert [e.id for e in email_record.emails] == [email.id]
    assert email_record.calls == []


async def test_lookup_recipient_finds_sms(async_session):
    org_id = uuid.uuid4()
    await _make_sms(async_session, org_id)
    await _make_sms(async_session, org_id, to_e164="+14155559999")
    await async_session.commit()

    record = await lookup_recipient(async_session, PHONE)
    assert len(record.sms) == 1
    assert record.sms[0].to_e164 == PHONE


async def test_delete_recipient_data_scrubs_sms_body(async_session):
    org_id = uuid.uuid4()
    sms = await _make_sms(async_session, org_id)
    await async_session.commit()

    summary = await delete_recipient_data(async_session, PHONE)
    assert summary.sms_scrubbed == 1

    await async_session.refresh(sms)
    assert sms.body == ""
    # Row shell (recipient linkage, status) survives — content-only scrub.
    assert sms.to_e164 == PHONE


async def test_export_recipient_data_includes_sms(async_session):
    org_id = uuid.uuid4()
    await _make_sms(async_session, org_id)
    await async_session.commit()

    export = await export_recipient_data(async_session, PHONE)
    assert len(export["sms"]) == 1
    assert export["sms"][0]["to_e164"] == PHONE
    json.dumps(export)  # JSON-able end to end


async def test_lookup_recipient_finds_email_in_cc_and_bcc(async_session):
    org_id = uuid.uuid4()
    cc_email = await _make_email(
        async_session, org_id, to=["someone-else@example.com"], cc=[EMAIL_ADDR]
    )
    await async_session.commit()

    record = await lookup_recipient(async_session, EMAIL_ADDR)
    assert [e.id for e in record.emails] == [cc_email.id]


async def test_lookup_recipient_finds_suppressions_and_audit_log(async_session):
    org_id = uuid.uuid4()
    sup = Suppression(
        organization_id=org_id,
        recipient=PHONE,
        channel="voice",
        reason="recipient_request",
        source="manual",
    )
    async_session.add(sup)
    audit = AuditLog(
        organization_id=org_id,
        action="call.create",
        resource_type="call",
        payload={"to": PHONE},
    )
    async_session.add(audit)
    await async_session.commit()

    record = await lookup_recipient(async_session, PHONE)
    assert [s.id for s in record.suppressions] == [sup.id]
    assert [a.id for a in record.audit_logs] == [audit.id]


async def test_lookup_recipient_matches_email_audit_log_array_payload(async_session):
    org_id = uuid.uuid4()
    audit = AuditLog(
        organization_id=org_id,
        action="email.create",
        resource_type="email",
        payload={"to": [EMAIL_ADDR, "other@example.com"]},
    )
    async_session.add(audit)
    await async_session.commit()

    record = await lookup_recipient(async_session, EMAIL_ADDR)
    assert [a.id for a in record.audit_logs] == [audit.id]


async def test_lookup_recipient_finds_audit_log_via_bcc(async_session):
    org_id = uuid.uuid4()
    audit = AuditLog(
        organization_id=org_id,
        action="email.create",
        resource_type="email",
        payload={
            "to": ["someone-else@example.com"],
            "cc": None,
            "bcc": [EMAIL_ADDR],
        },
    )
    async_session.add(audit)
    await async_session.commit()

    record = await lookup_recipient(async_session, EMAIL_ADDR)
    assert [a.id for a in record.audit_logs] == [audit.id]


async def test_lookup_recipient_finds_audit_log_with_mixed_case_bcc(async_session):
    """Task 5 made the Email-table match case-insensitive; the AuditLog
    match must have the same guarantee, or export_recipient_data silently
    omits audit rows for a mixed-case-addressed recipient."""
    org_id = uuid.uuid4()
    audit = AuditLog(
        organization_id=org_id,
        action="email.create",
        resource_type="email",
        payload={
            "to": ["someone-else@example.com"],
            "cc": None,
            "bcc": ["Alice@example.com"],
        },
    )
    async_session.add(audit)
    await async_session.commit()

    record = await lookup_recipient(async_session, "alice@example.com")
    assert [a.id for a in record.audit_logs] == [audit.id]


async def test_lookup_recipient_normalizes_email_case(async_session):
    org_id = uuid.uuid4()
    email = await _make_email(async_session, org_id, to=["alice@example.com"])
    await async_session.commit()

    record = await lookup_recipient(async_session, "ALICE@EXAMPLE.COM")
    assert [e.id for e in record.emails] == [email.id]


async def test_lookup_recipient_matches_mixed_case_stored_local_part(async_session):
    """The reverse direction of test_lookup_recipient_normalizes_email_case:
    the STORED address has a mixed-case local part (as a tenant might type
    it), and the DSAR request comes in fully lowercase (as a data subject
    would naturally type their own address)."""
    org_id = uuid.uuid4()
    email = await _make_email(async_session, org_id, to=["Alice@example.com"])
    await async_session.commit()

    record = await lookup_recipient(async_session, "alice@example.com")
    assert [e.id for e in record.emails] == [email.id]


async def test_delete_recipient_data_scrubs_mixed_case_stored_local_part(async_session):
    org_id = uuid.uuid4()
    email = await _make_email(async_session, org_id, to=["Alice@example.com"])
    await async_session.commit()

    summary = await delete_recipient_data(async_session, "alice@example.com")

    assert summary.emails_scrubbed == 1
    await async_session.refresh(email)
    assert email.body_text == ""


async def test_export_recipient_data_returns_json_serializable_dict(async_session):
    org_id = uuid.uuid4()
    await _make_call(async_session, org_id, to_e164=PHONE)
    await async_session.commit()

    data = await export_recipient_data(async_session, PHONE)
    # Must round-trip through json.dumps with no custom encoder.
    encoded = json.dumps(data)
    decoded = json.loads(encoded)
    assert decoded["identifier"] == PHONE
    assert len(decoded["calls"]) == 1
    assert decoded["calls"][0]["to_e164"] == PHONE
    assert isinstance(decoded["calls"][0]["id"], str)
    assert isinstance(decoded["calls"][0]["requested_at"], str)


async def test_delete_recipient_data_clears_content_preserves_suppression_and_audit(
    async_session,
):
    org_id = uuid.uuid4()
    call = await _make_call(async_session, org_id, to_e164=PHONE)
    sup = Suppression(
        organization_id=org_id,
        recipient=PHONE,
        channel="voice",
        reason="recipient_request",
        source="manual",
    )
    async_session.add(sup)
    audit = AuditLog(
        organization_id=org_id,
        action="call.create",
        resource_type="call",
        payload={"to": PHONE},
    )
    async_session.add(audit)
    await async_session.commit()

    summary = await delete_recipient_data(async_session, PHONE)

    assert summary.calls_scrubbed == 1
    assert summary.suppressions_preserved == 1

    await async_session.refresh(call)
    await async_session.refresh(sup)
    await async_session.refresh(audit)
    assert call.transcript is None
    # Suppression untouched -- it's kept, not removed.
    assert sup.recipient == PHONE
    # audit_log untouched.
    assert audit.payload == {"to": PHONE}


async def test_delete_recipient_data_clears_email_content(async_session):
    org_id = uuid.uuid4()
    email = await _make_email(async_session, org_id, to=[EMAIL_ADDR])
    await async_session.commit()

    summary = await delete_recipient_data(async_session, EMAIL_ADDR)

    assert summary.emails_scrubbed == 1
    await async_session.refresh(email)
    assert email.body_text == ""
    assert email.body_html is None
    assert email.raw_s3_key is None
    assert email.to_addresses == [EMAIL_ADDR]  # row shell intact
