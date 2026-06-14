import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from hailhq.core.email_ingest import IngestResult, _persist_one, ingest_inbound
from hailhq.core.models import Email, EmailAttachment, EmailDomain
from hailhq.core.providers.email.inbound.base import InboundMessage

FIX = Path(__file__).parent / "fixtures" / "inbound"


def _make_inbound_domain(org_id, user_prefix="alice", org_prefix="acme"):
    """An EmailDomain row for routing inbound to (user_prefix, org_prefix).

    ``inbound_enabled`` stays False here because the ingest service
    persists rows regardless of the flag — the flag only gates the
    later forwarding / webhook fan-out steps. Setting it True without
    forward_to/webhook_url would violate the email_domains_inbound_action
    check constraint.
    """
    return EmailDomain(
        organization_id=org_id,
        kind="hail_mail",
        domain=f"{user_prefix}+{org_prefix}@mail.hail.so",
        local_prefix_user=user_prefix,
        local_prefix_org=org_prefix,
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_ingest_persists_inbound_row_with_attachment(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="abc123",
        envelope_from="alice@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/abc123",
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )

    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "multipart_attachment.eml").read_bytes()

    result = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )

    assert isinstance(result, IngestResult)
    assert len(result.email_ids) == 1
    assert result.suppressed_reasons == []
    assert result.skipped_recipients == []

    email = (
        await async_session.execute(
            select(Email).where(Email.id == result.email_ids[0])
        )
    ).scalar_one()
    assert email.direction == "inbound"
    assert email.status == "received"
    assert email.from_address == "alice@example.com"
    assert email.message_id == "<m2@example.com>"
    assert email.email_domain_id == domain.id
    assert email.organization_id == org_id

    s3.put_attachment.assert_called_once()
    atts = (
        (
            await async_session.execute(
                select(EmailAttachment).where(EmailAttachment.email_id == email.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(atts) == 1
    assert atts[0].filename == "report.pdf"


@pytest.mark.asyncio
async def test_ingest_is_idempotent(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="bob", org_prefix="globex")
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="dup",
        envelope_from="x@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/dup",
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()

    r1 = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )
    r2 = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )

    assert r1.email_ids == r2.email_ids
    # Only one row in DB despite two calls.
    count = len(
        (
            await async_session.execute(
                select(Email).where(Email.organization_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    assert count == 1


@pytest.mark.asyncio
async def test_ingest_suppresses_spam(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="carol", org_prefix="initech")
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="spam1",
        envelope_from="x@spam.example",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/spam1",
        spam_verdict="FAIL",
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()

    result = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )

    assert result.suppressed_reasons == ["spam"]
    assert len(result.email_ids) == 1

    email = (
        await async_session.execute(
            select(Email).where(Email.id == result.email_ids[0])
        )
    ).scalar_one()
    assert email.metadata_ == {"suppressed": "spam"}
    # No attachment persisted on suppressed rows.
    s3.put_attachment.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_suppresses_virus(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="dave", org_prefix="hooli")
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="virus1",
        envelope_from="x@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/virus1",
        virus_verdict="FAIL",
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "multipart_attachment.eml").read_bytes()

    result = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )

    assert result.suppressed_reasons == ["virus"]
    s3.put_attachment.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_skips_unknown_recipient(async_session):
    msg = InboundMessage(
        provider_message_id="m4",
        envelope_from="x@example.com",
        envelope_recipients=["unknown@mail.hail.so"],
        raw_s3_bucket="b",
        raw_s3_key="raw/m4",
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()

    result = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )

    assert result.email_ids == []
    assert result.skipped_recipients == ["unknown@mail.hail.so"]


@pytest.mark.asyncio
async def test_forward_enqueues_per_target(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="eve", org_prefix="evilcorp")
    # Inbound_enabled requires at least one of forward_to/webhook_url, so
    # setting forward_to here is what unlocks forwarding.
    domain.inbound_enabled = True
    domain.forward_to = ["ops@example.com", "billing@example.com"]
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="fwd1",
        envelope_from="alice@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/fwd1",
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()

    captured: list[dict] = []

    async def fake_enqueue(_db, **kw):
        captured.append(kw)

    await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=fake_enqueue,
        org_rate_per_hour=10_000,
    )

    assert [c["to"] for c in captured] == ["ops@example.com", "billing@example.com"]
    # Forwarder address derived from local_prefix_org.
    assert all(c["from_address"] == "forwarder+evilcorp@mail.hail.so" for c in captured)


@pytest.mark.asyncio
async def test_forward_skipped_when_inbound_disabled(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="frank", org_prefix="dunder")
    # inbound_enabled stays False (default). forward_to alone is meaningless
    # without the flag — ingest persists but doesn't enqueue forwards.
    domain.forward_to = ["ops@example.com"]
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="fwd-off",
        envelope_from="alice@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/fwd-off",
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    enqueue_calls = []

    async def fake_enqueue(_db, **kw):
        enqueue_calls.append(kw)

    await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=fake_enqueue,
        org_rate_per_hour=10_000,
    )
    assert enqueue_calls == []


@pytest.mark.asyncio
async def test_forward_loop_header_suppresses(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="grace", org_prefix="loopco")
    domain.inbound_enabled = True
    domain.forward_to = ["ops@example.com"]
    async_session.add(domain)
    await async_session.commit()

    # MIME with X-Hail-Forward-Hops at the cap.
    raw = (
        b"From: x@example.com\r\n"
        b"To: " + domain.domain.encode() + b"\r\n"
        b"Subject: loop\r\n"
        b"Message-ID: <loop1@example.com>\r\n"
        b"X-Hail-Forward-Hops: 3\r\n"
        b"\r\n"
        b"body"
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = raw

    msg = InboundMessage(
        provider_message_id="loop1",
        envelope_from="x@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/loop1",
    )
    enqueue_calls = []

    async def fake_enqueue(_db, **kw):
        enqueue_calls.append(kw)

    result = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=fake_enqueue,
        forward_max_hops=3,
        org_rate_per_hour=10_000,
    )
    assert "forward_loop" in result.suppressed_reasons
    assert enqueue_calls == []


@pytest.mark.asyncio
async def test_forward_rejects_target_on_base_domain(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="hank", org_prefix="selfloop")
    domain.inbound_enabled = True
    domain.forward_to = ["other@mail.hail.so"]  # base domain — loop trap
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="self1",
        envelope_from="x@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/self1",
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    enqueue_calls = []

    async def fake_enqueue(_db, **kw):
        enqueue_calls.append(kw)

    result = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=fake_enqueue,
        org_rate_per_hour=10_000,
    )
    assert "forward_loop" in result.suppressed_reasons
    assert enqueue_calls == []


@pytest.mark.asyncio
async def test_base_domain_target_skips_sibling_survives(async_session):
    """A base-domain forward target is skipped, but valid sibling targets
    still get enqueued (base_domain cause → continue, not return)."""
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="ivan", org_prefix="sibling")
    domain.inbound_enabled = True
    domain.forward_to = ["self@mail.hail.so", "valid@external.com"]
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="sibling1",
        envelope_from="sender@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/sibling1",
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()

    captured: list[dict] = []

    async def fake_enqueue(_db, **kw):
        captured.append(kw)

    result = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=fake_enqueue,
        org_rate_per_hour=10_000,
    )

    # The base-domain target is suppressed, but the external sibling IS enqueued.
    assert [c["to"] for c in captured] == ["valid@external.com"]
    assert "forward_loop" in result.suppressed_reasons
    # "forward_loop" must appear exactly once (no duplicate appending).
    assert result.suppressed_reasons.count("forward_loop") == 1


@pytest.mark.asyncio
async def test_ingest_skips_recipient_on_wrong_domain(async_session):
    msg = InboundMessage(
        provider_message_id="m5",
        envelope_from="x@example.com",
        envelope_recipients=["alice+acme@other.example"],
        raw_s3_bucket="b",
        raw_s3_key="raw/m5",
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()

    result = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )
    assert result.email_ids == []
    assert result.skipped_recipients == ["alice+acme@other.example"]


@pytest.mark.asyncio
async def test_persist_one_survives_unique_violation(async_session):
    """Short-circuit: _persist_one returns the existing id without raising
    when (org, message_id) is already present in the DB (SELECT path)."""
    from hailhq.core.email_mime import ParsedMime

    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="zara", org_prefix="zerocorp")
    async_session.add(domain)
    await async_session.commit()

    # Pre-insert a colliding inbound row.
    pre = Email(
        organization_id=org_id,
        email_domain_id=domain.id,
        direction="inbound",
        from_address="a@b.com",
        to_addresses=["zara+zerocorp@mail.hail.so"],
        subject="x",
        body_text="hi",
        status="received",
        provider="ses",
        message_id="<dup-sc@hail>",
    )
    async_session.add(pre)
    await async_session.commit()

    parsed = ParsedMime(
        from_address="a@b.com",
        to_addresses=[],
        cc_addresses=[],
        subject="x",
        message_id="<dup-sc@hail>",
        in_reply_to=None,
        references_ids=None,
        body_text="hi",
        body_html=None,
        attachments=[],
    )
    msg = InboundMessage(
        provider_message_id="ses-sc-1",
        envelope_from="a@b.com",
        envelope_recipients=[],
        raw_s3_bucket="b",
        raw_s3_key="k",
    )

    class _StubS3:
        async def put_attachment(self, *a, **kw):
            pass

    result_id, created = await _persist_one(
        async_session,
        parsed=parsed,
        message=msg,
        domain=domain,
        suppress=None,
        s3=_StubS3(),
    )
    assert result_id == pre.id
    assert created is False


@pytest.mark.asyncio
async def test_persist_one_savepoint_catches_race(async_session):
    """Race path: when _existing_inbound_id misses a concurrent row,
    the SAVEPOINT-wrapped flush catches the IntegrityError from
    emails_inbound_message_id_uq and re-reads the existing id.

    The outer transaction must remain intact after recovery
    (verified by a successful commit at the end).
    """
    import hailhq.core.email_ingest as _ingest_mod
    from hailhq.core.email_mime import ParsedMime

    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="yvonne", org_prefix="yolocorp")
    async_session.add(domain)
    await async_session.commit()

    # Pre-insert the "winning" concurrent row. Leave provider_message_id NULL
    # so the collision is on emails_inbound_message_id_uq only (not the
    # outbound-only partial unique index).
    pre = Email(
        organization_id=org_id,
        email_domain_id=domain.id,
        direction="inbound",
        from_address="a@b.com",
        to_addresses=["yvonne+yolocorp@mail.hail.so"],
        subject="x",
        body_text="hi",
        status="received",
        provider="ses",
        message_id="<dup-race@hail>",
        provider_message_id=None,  # provider_message_id is irrelevant here: inbound rows are excluded from the
        # outbound partial unique index (emails_provider_message_id_outbound_uq).
    )
    async_session.add(pre)
    await async_session.commit()

    parsed = ParsedMime(
        from_address="a@b.com",
        to_addresses=[],
        cc_addresses=[],
        subject="x",
        message_id="<dup-race@hail>",
        in_reply_to=None,
        references_ids=None,
        body_text="hi",
        body_html=None,
        attachments=[],
    )
    # provider_message_id also NULL in _persist_one path (via message.provider_message_id)
    # InboundMessage requires str, so use a fresh unique id that won't collide on
    # emails_provider_message_id_outbound_uq (inbound rows are excluded from that index).
    msg = InboundMessage(
        provider_message_id="ses-race-1",
        envelope_from="a@b.com",
        envelope_recipients=[],
        raw_s3_bucket="b",
        raw_s3_key="k",
    )

    class _StubS3:
        async def put_attachment(self, *a, **kw):
            pass

    # Simulate the race: first _existing_inbound_id call returns None
    # (SELECT missed the concurrent row), second call returns the existing id.
    # The second value (pre.id) is what the post-rollback re-SELECT returns.
    # The first (None) only skips the early-return so _persist_one proceeds to
    # db.add(email) + db.flush(), which DOES hit the real emails_inbound_message_id_uq
    # constraint (pre is committed above). The IntegrityError is genuine.
    with patch.object(
        _ingest_mod,
        "_existing_inbound_id",
        side_effect=[None, pre.id],
    ):
        result_id, created = await _persist_one(
            async_session,
            parsed=parsed,
            message=msg,
            domain=domain,
            suppress=None,
            s3=_StubS3(),
        )

    assert result_id == pre.id
    assert created is False

    # Crucially: the outer transaction must still be usable after savepoint recovery.
    await async_session.commit()


@pytest.mark.asyncio
async def test_raw_url_no_double_slash_when_base_has_trailing_slash(async_session):
    """join_url must produce a single-slash join even when api_base_url ends with /."""
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="ursula", org_prefix="urlco")
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="url-test-1",
        envelope_from="sender@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/url-test-1",
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()

    captured_data: list[dict] = []

    async def fake_fanout(
        _db, *, organization_id, email_domain_id, event_type, event_id, data
    ):
        captured_data.append(data)

    await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        fanout=fake_fanout,
        # Pass a base URL WITH a trailing slash — must not produce double slash.
        api_base_url="https://api.hail.so/",
        org_rate_per_hour=10_000,
    )

    assert len(captured_data) == 1
    data = captured_data[0]
    email_id = data["id"]

    # raw_url must be exactly one slash between base and path — no double slash.
    assert data["raw_url"] == f"https://api.hail.so/emails/{email_id}/raw"
    assert "//" not in data["raw_url"].replace("https://", "")


@pytest.mark.asyncio
async def test_org_inbound_cap_skips_fanout_but_persists(async_session):
    """When org_rate_per_hour=1, a second inbound message still gets a DB row
    but fan-out is skipped and 'inbound_rate_limit' is recorded in suppressed_reasons.
    """
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="zoe", org_prefix="capco")
    async_session.add(domain)
    await async_session.commit()

    calls: list[dict] = []

    async def fake_fanout(
        _db, *, organization_id, email_domain_id, event_type, event_id, data
    ):
        calls.append({"organization_id": organization_id, "event_type": event_type})

    def _make_msg(provider_msg_id: str, message_id: str) -> InboundMessage:
        return InboundMessage(
            provider_message_id=provider_msg_id,
            envelope_from="sender@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key=f"raw/{provider_msg_id}",
        )

    # Provide distinct MIME bytes per message so message_id differs.
    def _raw(message_id: str) -> bytes:
        return (
            b"From: sender@example.com\r\n"
            b"To: zoe+capco@mail.hail.so\r\n"
            b"Subject: Cap test\r\n"
            b"Message-ID: <" + message_id.encode() + b">\r\n"
            b"\r\n"
            b"body"
        )

    s3_first = AsyncMock()
    s3_first.fetch_raw.return_value = _raw("m1@capco")
    s3_second = AsyncMock()
    s3_second.fetch_raw.return_value = _raw("m2@capco")

    # First message — under cap (cap=1, used=0 → not over).
    await ingest_inbound(
        async_session,
        message=_make_msg("prov-cap-1", "<m1@capco>"),
        s3=s3_first,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=1,
        fanout=fake_fanout,
    )

    # Second message — over cap (cap=1, used=1 → over).
    res = await ingest_inbound(
        async_session,
        message=_make_msg("prov-cap-2", "<m2@capco>"),
        s3=s3_second,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=1,
        fanout=fake_fanout,
    )

    # Only the first message produced an email.received; the over-cap second
    # message produced an email.received.suppressed instead.
    received = [c for c in calls if c["event_type"] == "email.received"]
    suppressed = [c for c in calls if c["event_type"] == "email.received.suppressed"]
    assert len(received) == 1, f"expected 1 email.received, got {len(received)}"
    assert len(suppressed) == 1, f"expected 1 suppressed event, got {len(suppressed)}"
    # Second result: row persisted, rate-limit reason recorded.
    assert len(res.email_ids) == 1, "second message must still produce a DB row"
    assert "inbound_rate_limit" in res.suppressed_reasons
    # 'inbound_rate_limit' must appear exactly once (no duplication).
    assert res.suppressed_reasons.count("inbound_rate_limit") == 1


@pytest.mark.asyncio
async def test_attachment_url_no_double_slash_when_base_has_trailing_slash(
    async_session,
):
    """Attachment URL must be well-formed with no double slash when base has trailing slash."""
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id, user_prefix="viktor", org_prefix="vaultco")
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="url-att-test-1",
        envelope_from="sender@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/url-att-test-1",
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "multipart_attachment.eml").read_bytes()

    captured_data: list[dict] = []

    async def fake_fanout(
        _db, *, organization_id, email_domain_id, event_type, event_id, data
    ):
        captured_data.append(data)

    await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        fanout=fake_fanout,
        # Pass a base URL WITH a trailing slash.
        api_base_url="https://api.hail.so/",
        org_rate_per_hour=10_000,
    )

    assert len(captured_data) == 1
    data = captured_data[0]
    email_id = data["id"]
    atts = data["attachments"]

    # At least one attachment from the multipart fixture.
    assert len(atts) >= 1
    att_url = atts[0]["url"]
    assert att_url is not None
    # Must start correctly and contain no double slash after the scheme.
    assert att_url.startswith(f"https://api.hail.so/emails/{email_id}/attachments/")
    assert "//" not in att_url.replace("https://", "")


@pytest.mark.asyncio
async def test_replay_does_not_refire_forwards_or_fanout(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    domain.inbound_enabled = True
    domain.forward_to = ["ops@example.com"]
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="replay-1",
        envelope_from="alice@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/replay-1",
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    forward_enqueue = AsyncMock()
    fanout = AsyncMock(return_value=1)

    for _ in range(2):  # initial delivery + SES redelivery
        await ingest_inbound(
            async_session,
            message=msg,
            s3=s3,
            hail_mail_base_domain="mail.hail.so",
            forward_enqueue=forward_enqueue,
            fanout=fanout,
            org_rate_per_hour=10_000,
        )

    assert forward_enqueue.await_count == 1
    assert fanout.await_count == 1


@pytest.mark.asyncio
async def test_attachment_only_mail_is_persisted_not_destroyed(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="attonly-1",
        envelope_from="alice@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/attonly-1",
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "attachment_only.eml").read_bytes()

    result = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )
    assert len(result.email_ids) == 1
    email = (
        await async_session.execute(
            select(Email).where(Email.id == result.email_ids[0])
        )
    ).scalar_one()
    assert email.body_text == ""  # coalesced, not lost
    assert email.body_html is None


@pytest.mark.asyncio
async def test_unrelated_integrity_error_is_not_swallowed(async_session):
    """A non-dedupe constraint violation must propagate, not 200-skip.

    Calls _persist_one directly with a detached EmailDomain whose
    id/organization_id don't exist in the DB → FK violation on flush.
    Under the old blanket `except IntegrityError` this was silently
    absorbed as a dedupe race; it must raise instead.
    """
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    from hailhq.core.email_mime import ParsedMime

    # Never added to the session/DB — its ids point nowhere.
    ghost_domain = _make_inbound_domain(uuid.uuid4())
    ghost_domain.id = uuid.uuid4()

    parsed = ParsedMime(
        from_address="alice@example.com",
        to_addresses=["alice+acme@mail.hail.so"],
        cc_addresses=[],
        subject="x",
        message_id="<fk-violation@example.com>",
        in_reply_to=None,
        references_ids=None,
        body_text="hi",
        body_html=None,
        attachments=[],
    )
    msg = InboundMessage(
        provider_message_id="fk-violation-1",
        envelope_from="alice@example.com",
        envelope_recipients=["alice+acme@mail.hail.so"],
        raw_s3_bucket="b",
        raw_s3_key="raw/fk-violation-1",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )

    with pytest.raises(SAIntegrityError):
        await _persist_one(
            async_session,
            parsed=parsed,
            message=msg,
            domain=ghost_domain,
            suppress=None,
            s3=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_org_cap_suppresses_forwarding_too(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    domain.inbound_enabled = True
    domain.forward_to = ["ops@example.com"]
    async_session.add(domain)
    await async_session.commit()

    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    forward_enqueue = AsyncMock()

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="cap-1",
            envelope_from="alice@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key="raw/cap-1",
            spam_verdict="PASS",
            virus_verdict="PASS",
            spf_verdict="PASS",
            dkim_verdict="PASS",
            dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=forward_enqueue,
        org_rate_per_hour=0,  # cap of 0 → always over
    )
    assert len(result.email_ids) == 1  # persisted
    forward_enqueue.assert_not_awaited()  # but not forwarded


@pytest.mark.asyncio
async def test_suppressed_event_emitted_on_rate_limit(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    async_session.add(domain)
    await async_session.commit()

    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    fanout = AsyncMock(return_value=1)

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="sup-1",
            envelope_from="alice@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key="raw/sup-1",
            spam_verdict="PASS",
            virus_verdict="PASS",
            spf_verdict="PASS",
            dkim_verdict="PASS",
            dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        fanout=fanout,
        org_rate_per_hour=0,
    )
    assert result.suppressed_reasons == ["inbound_rate_limit"]
    fanout.assert_awaited_once()
    kwargs = fanout.await_args.kwargs
    assert kwargs["event_type"] == "email.received.suppressed"
    assert kwargs["data"]["reason"] == "inbound_rate_limit"


@pytest.mark.asyncio
async def test_suppressed_event_emitted_on_forward_loop(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    domain.inbound_enabled = True
    domain.forward_to = ["ops@mail.hail.so"]  # base-domain target → loop
    async_session.add(domain)
    await async_session.commit()

    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    fanout = AsyncMock(return_value=1)
    forward_enqueue = AsyncMock()

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="sup-2",
            envelope_from="alice@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key="raw/sup-2",
            spam_verdict="PASS",
            virus_verdict="PASS",
            spf_verdict="PASS",
            dkim_verdict="PASS",
            dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=forward_enqueue,
        fanout=fanout,
        org_rate_per_hour=10_000,
    )
    assert "forward_loop" in result.suppressed_reasons
    event_types = [c.kwargs["event_type"] for c in fanout.await_args_list]
    assert "email.received" in event_types
    assert "email.received.suppressed" in event_types
    sup = next(
        c.kwargs
        for c in fanout.await_args_list
        if c.kwargs["event_type"] == "email.received.suppressed"
    )
    assert sup["data"]["reason"] == "forward_loop"


@pytest.mark.asyncio
async def test_suppressed_event_emitted_on_forward_rate_limit(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    domain.inbound_enabled = True
    domain.forward_to = ["ops@example.com"]
    domain.forward_rate_per_hour = 0  # cap of 0 → always over
    async_session.add(domain)
    await async_session.commit()

    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    fanout = AsyncMock(return_value=1)
    forward_enqueue = AsyncMock()

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="sup-3",
            envelope_from="alice@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key="raw/sup-3",
            spam_verdict="PASS",
            virus_verdict="PASS",
            spf_verdict="PASS",
            dkim_verdict="PASS",
            dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=forward_enqueue,
        fanout=fanout,
        org_rate_per_hour=10_000,
    )
    assert "forward_rate_limit" in result.suppressed_reasons
    forward_enqueue.assert_not_awaited()
    sup = next(
        c.kwargs
        for c in fanout.await_args_list
        if c.kwargs["event_type"] == "email.received.suppressed"
    )
    assert sup["data"]["reason"] == "forward_rate_limit"


@pytest.mark.asyncio
async def test_missing_from_header_falls_back_to_envelope_from(async_session):
    raw = b"To: alice+acme@mail.hail.so\r\nSubject: no from\r\nMessage-ID: <nf@x>\r\n\r\nhi"
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    async_session.add(domain)
    await async_session.commit()

    s3 = AsyncMock()
    s3.fetch_raw.return_value = raw

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="nf-1",
            envelope_from="bounce@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key="raw/nf-1",
            spam_verdict="PASS",
            virus_verdict="PASS",
            spf_verdict="PASS",
            dkim_verdict="PASS",
            dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )

    email = (
        await async_session.execute(
            select(Email).where(Email.id == result.email_ids[0])
        )
    ).scalar_one()
    assert email.from_address == "bounce@example.com"


@pytest.mark.asyncio
async def test_created_email_ids_excludes_replays(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    async_session.add(domain)
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    msg = InboundMessage(
        provider_message_id="created-1",
        envelope_from="alice@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/created-1",
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    first = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )
    second = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        org_rate_per_hour=10_000,
    )
    assert len(first.created_email_ids) == 1
    assert first.created_email_ids[0][1] == org_id  # (email_id, org_id) tuple
    assert second.created_email_ids == []  # replay creates nothing
    assert len(second.email_ids) == 1  # but still resolves the row


@pytest.mark.asyncio
async def test_mail_without_message_id_is_still_dedupe_safe(async_session):
    raw = b"From: a@x\r\nTo: alice+acme@mail.hail.so\r\nSubject: no mid\r\n\r\nhi"
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    async_session.add(domain)
    await async_session.commit()

    s3 = AsyncMock()
    s3.fetch_raw.return_value = raw

    msg = InboundMessage(
        provider_message_id="ses-receipt-1",
        envelope_from="a@x",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/ses-receipt-1",
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    for _ in range(2):  # redelivery of the same SES receipt
        await ingest_inbound(
            async_session,
            message=msg,
            s3=s3,
            hail_mail_base_domain="mail.hail.so",
            org_rate_per_hour=10_000,
        )

    count = (
        await async_session.execute(
            select(func.count())
            .select_from(Email)
            .where(Email.organization_id == org_id, Email.direction == "inbound")
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_unfunded_org_suppresses_forwarding_with_reason(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    domain.inbound_enabled = True
    domain.forward_to = ["ops@example.com"]
    async_session.add(domain)
    await async_session.commit()

    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    forward_enqueue = AsyncMock()
    fanout = AsyncMock(return_value=1)

    async def broke(_db, _org_id):
        return False

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="nofunds-1",
            envelope_from="alice@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key="raw/nofunds-1",
            spam_verdict="PASS",
            virus_verdict="PASS",
            spf_verdict="PASS",
            dkim_verdict="PASS",
            dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=forward_enqueue,
        fanout=fanout,
        funds_check=broke,
        org_rate_per_hour=10_000,
    )
    assert len(result.created_email_ids) == 1
    forward_enqueue.assert_not_awaited()
    assert "insufficient_funds" in result.suppressed_reasons
    types = [c.kwargs["event_type"] for c in fanout.await_args_list]
    assert "email.received" in types
    sup = next(
        c.kwargs
        for c in fanout.await_args_list
        if c.kwargs["event_type"] == "email.received.suppressed"
    )
    assert sup["data"]["reason"] == "insufficient_funds"


@pytest.mark.asyncio
async def test_funded_org_forwards_normally(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    domain.inbound_enabled = True
    domain.forward_to = ["ops@example.com"]
    async_session.add(domain)
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    forward_enqueue = AsyncMock()

    async def funded(_db, _org_id):
        return True

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="funds-1",
            envelope_from="alice@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key="raw/funds-1",
            spam_verdict="PASS",
            virus_verdict="PASS",
            spf_verdict="PASS",
            dkim_verdict="PASS",
            dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=forward_enqueue,
        funds_check=funded,
        org_rate_per_hour=10_000,
    )
    forward_enqueue.assert_awaited_once()
    assert "insufficient_funds" not in result.suppressed_reasons


@pytest.mark.asyncio
async def test_suppression_reason_persisted_on_row(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    domain.inbound_enabled = True
    domain.forward_to = ["ops@example.com"]
    async_session.add(domain)
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()

    async def broke(_db, _org):
        return False

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="supmeta-1",
            envelope_from="a@x",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b",
            raw_s3_key="raw/supmeta-1",
            spam_verdict="PASS",
            virus_verdict="PASS",
            spf_verdict="PASS",
            dkim_verdict="PASS",
            dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
        forward_enqueue=AsyncMock(),
        funds_check=broke,
        org_rate_per_hour=10_000,
    )
    email = (
        await async_session.execute(
            select(Email).where(Email.id == result.created_email_ids[0][0])
        )
    ).scalar_one()
    assert email.metadata_.get("suppressed_reasons") == ["insufficient_funds"]
