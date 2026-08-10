"""Unit tests for ``SesEmailProvider``.

We mock at the **botocore boundary** (``Stubber``) rather than patching SDK
objects. That way the tests pin the actual request shape sent to SES — if
SES renames a parameter or removes a field from the response, these tests
break the same way real usage would.
"""

from __future__ import annotations

import boto3
import pytest
from botocore.stub import ANY, Stubber
from hailhq.core.providers.email import SesEmailProvider
from hailhq.core.providers.email.base import (
    DkimRecord,
    ProviderAttachment,
    ProviderIdentity,
    ProviderSendResult,
)


@pytest.fixture()
def ses_client():
    return boto3.client(
        "sesv2",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture()
def stub(ses_client):
    with Stubber(ses_client) as stubber:
        yield stubber


class FakeClient:
    """Captures ``send_email`` kwargs and returns a canned MessageId.

    Used where the Stubber's exact-params matching can't pin the request
    (raw MIME carries a nondeterministic multipart boundary) or where a
    test asserts on the kwargs shape directly.
    """

    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        self.kwargs: dict | None = None

    def send_email(self, **kwargs):
        self.kwargs = kwargs
        return {"MessageId": self.message_id}


# ---------------------------------------------------------------- send_email


async def test_send_email_text_only(ses_client, stub: Stubber) -> None:
    stub.add_response(
        "send_email",
        {"MessageId": "0102018f1234abcd-ses-test"},
        {
            "FromEmailAddress": "alerts@acme.com",
            "Destination": {"ToAddresses": ["alice@example.com"]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": "hi", "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": "body", "Charset": "UTF-8"}},
                }
            },
        },
    )

    provider = SesEmailProvider(client=ses_client)
    result = await provider.send_email(
        from_address="alerts@acme.com",
        to_addresses=["alice@example.com"],
        subject="hi",
        body_text="body",
        body_html=None,
    )

    assert isinstance(result, ProviderSendResult)
    assert result.provider_message_id == "0102018f1234abcd-ses-test"
    stub.assert_no_pending_responses()


async def test_send_email_html_and_text_with_cc_bcc_reply_to(
    ses_client, stub: Stubber
) -> None:
    stub.add_response(
        "send_email",
        {"MessageId": "msg-id"},
        {
            "FromEmailAddress": "alerts@acme.com",
            "Destination": {
                "ToAddresses": ["alice@example.com"],
                "CcAddresses": ["bob@example.com"],
                "BccAddresses": ["audit@acme.com"],
            },
            "Content": {
                "Simple": {
                    "Subject": ANY,
                    "Body": {
                        "Text": {"Data": "plain", "Charset": "UTF-8"},
                        "Html": {"Data": "<p>html</p>", "Charset": "UTF-8"},
                    },
                }
            },
            "ReplyToAddresses": ["replyto@acme.com"],
        },
    )

    provider = SesEmailProvider(client=ses_client)
    result = await provider.send_email(
        from_address="alerts@acme.com",
        to_addresses=["alice@example.com"],
        subject="hi",
        body_text="plain",
        body_html="<p>html</p>",
        cc=["bob@example.com"],
        bcc=["audit@acme.com"],
        reply_to="replyto@acme.com",
    )
    assert result.provider_message_id == "msg-id"
    stub.assert_no_pending_responses()


async def test_send_email_with_headers_uses_simple_headers(
    ses_client, stub: Stubber
) -> None:
    stub.add_response(
        "send_email",
        {"MessageId": "m-1"},
        {
            "FromEmailAddress": "forwarder+acme@mail.hail.so",
            "Destination": {"ToAddresses": ["ops@example.com"]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": "Fwd: hi", "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": "x", "Charset": "UTF-8"}},
                    "Headers": [
                        {"Name": "X-Hail-Forward-Hops", "Value": "1"},
                        {"Name": "Auto-Submitted", "Value": "auto-forwarded"},
                    ],
                }
            },
        },
    )

    provider = SesEmailProvider(client=ses_client)
    result = await provider.send_email(
        from_address="forwarder+acme@mail.hail.so",
        to_addresses=["ops@example.com"],
        subject="Fwd: hi",
        body_text="x",
        body_html=None,
        headers={"X-Hail-Forward-Hops": "1", "Auto-Submitted": "auto-forwarded"},
    )
    assert result.provider_message_id == "m-1"
    stub.assert_no_pending_responses()


async def test_send_email_with_attachments_uses_raw_mime() -> None:
    fake = FakeClient("m-2")
    provider = SesEmailProvider(client=fake)
    result = await provider.send_email(
        from_address="forwarder+acme@mail.hail.so",
        to_addresses=["ops@example.com"],
        subject="Fwd: invoice",
        body_text="see attached",
        body_html=None,
        headers={"X-Hail-Forward-Hops": "1"},
        attachments=[
            ProviderAttachment(
                filename="invoice.pdf",
                content_type="application/pdf",
                payload=b"%PDF-1.4",
            )
        ],
    )

    assert result.provider_message_id == "m-2"
    assert fake.kwargs is not None
    assert "Raw" in fake.kwargs["Content"]
    raw = fake.kwargs["Content"]["Raw"]["Data"]
    assert b"invoice.pdf" in raw
    assert b"X-Hail-Forward-Hops" in raw
    assert b"base64" in raw  # attachment payload is base64-encoded


async def test_send_email_simple_path_with_from_name(ses_client, stub: Stubber) -> None:
    stub.add_response(
        "send_email",
        {"MessageId": "m-name"},
        {
            "FromEmailAddress": "Acme Billing <alerts@acme.com>",
            "Destination": {"ToAddresses": ["alice@example.com"]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": "hi", "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": "body", "Charset": "UTF-8"}},
                }
            },
        },
    )

    provider = SesEmailProvider(client=ses_client)
    result = await provider.send_email(
        from_address="alerts@acme.com",
        from_name="Acme Billing",
        to_addresses=["alice@example.com"],
        subject="hi",
        body_text="body",
        body_html=None,
    )
    assert result.provider_message_id == "m-name"
    stub.assert_no_pending_responses()


async def test_send_email_simple_path_splits_long_non_ascii_from_name() -> None:
    """RFC 2047 caps encoded words at 75 chars — a long non-ASCII name must
    ride as multiple encoded words, not one oversized token, and the
    parameter must stay a single line."""
    fake = FakeClient("m-name-long")
    provider = SesEmailProvider(client=fake)
    await provider.send_email(
        from_address="a@b.co",
        from_name=(
            "Café Réservations München Support Team — "
            "Département Facturation Européenne"
        ),
        to_addresses=["ops@example.com"],
        subject="hi",
        body_text="body",
        body_html=None,
    )

    assert fake.kwargs is not None
    friendly = fake.kwargs["FromEmailAddress"]
    assert "\r" not in friendly and "\n" not in friendly
    assert friendly.endswith("<a@b.co>")
    encoded_words = [tok for tok in friendly.split() if tok.startswith("=?")]
    assert encoded_words, friendly
    assert all(len(tok) <= 75 for tok in encoded_words), friendly


async def test_send_email_raw_path_with_from_name() -> None:
    fake = FakeClient("m-name-raw")
    provider = SesEmailProvider(client=fake)
    await provider.send_email(
        from_address="alerts@acme.com",
        from_name="Acme Billing",
        to_addresses=["ops@example.com"],
        subject="invoice",
        body_text="see attached",
        body_html=None,
        attachments=[
            ProviderAttachment(
                filename="invoice.pdf",
                content_type="application/pdf",
                payload=b"%PDF-1.4",
            )
        ],
    )

    assert fake.kwargs is not None
    raw = fake.kwargs["Content"]["Raw"]["Data"]
    assert b"From: Acme Billing <alerts@acme.com>" in raw
    # The envelope identity stays the bare address — SES matches the
    # sending identity against it.
    assert fake.kwargs["FromEmailAddress"] == "alerts@acme.com"


async def test_send_email_raw_path_encodes_non_ascii_from_name() -> None:
    fake = FakeClient("m-name-utf8")
    provider = SesEmailProvider(client=fake)
    await provider.send_email(
        from_address="alerts@acme.com",
        from_name="Café Réservations",
        to_addresses=["ops@example.com"],
        subject="invoice",
        body_text="see attached",
        body_html=None,
        attachments=[
            ProviderAttachment(
                filename="invoice.pdf",
                content_type="application/pdf",
                payload=b"%PDF-1.4",
            )
        ],
    )

    assert fake.kwargs is not None
    raw = fake.kwargs["Content"]["Raw"]["Data"]
    # Non-ASCII display names must ride as RFC 2047 encoded words, never
    # raw UTF-8 bytes in the header.
    assert b"=?utf-8?" in raw
    assert "Café".encode() not in raw


async def test_send_email_requires_recipients(ses_client) -> None:
    provider = SesEmailProvider(client=ses_client)
    with pytest.raises(ValueError, match="at least one recipient"):
        await provider.send_email(
            from_address="alerts@acme.com",
            to_addresses=[],
            subject="hi",
            body_text="body",
            body_html=None,
        )


async def test_send_email_requires_a_body(ses_client) -> None:
    provider = SesEmailProvider(client=ses_client)
    with pytest.raises(ValueError, match="body_text or body_html"):
        await provider.send_email(
            from_address="alerts@acme.com",
            to_addresses=["alice@example.com"],
            subject="hi",
            body_text=None,
            body_html=None,
        )


async def test_send_email_simple_path_with_configuration_set(
    ses_client, stub: Stubber, monkeypatch
) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "hail_ses_configuration_set", "hail-events")
    stub.add_response(
        "send_email",
        {"MessageId": "mid-123"},
        {
            "FromEmailAddress": "a@b.com",
            "Destination": {"ToAddresses": ["c@d.com"]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": "s", "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": "t", "Charset": "UTF-8"}},
                }
            },
            "ConfigurationSetName": "hail-events",
        },
    )

    provider = SesEmailProvider(client=ses_client)
    result = await provider.send_email(
        from_address="a@b.com",
        to_addresses=["c@d.com"],
        subject="s",
        body_text="t",
        body_html=None,
    )
    assert result.provider_message_id == "mid-123"
    stub.assert_no_pending_responses()


async def test_send_email_raw_path_with_configuration_set(monkeypatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "hail_ses_configuration_set", "hail-events")

    fake = FakeClient("m-attach")
    provider = SesEmailProvider(client=fake)
    result = await provider.send_email(
        from_address="forwarder+acme@mail.hail.so",
        to_addresses=["ops@example.com"],
        subject="Fwd: invoice",
        body_text="see attached",
        body_html=None,
        attachments=[
            ProviderAttachment(
                filename="invoice.pdf",
                content_type="application/pdf",
                payload=b"%PDF-1.4",
            )
        ],
    )

    assert result.provider_message_id == "m-attach"
    assert fake.kwargs is not None
    assert "Raw" in fake.kwargs["Content"]
    assert fake.kwargs.get("ConfigurationSetName") == "hail-events"


async def test_send_email_without_configuration_set_simple_path(monkeypatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "hail_ses_configuration_set", "")

    fake = FakeClient("mid-no-config")
    provider = SesEmailProvider(client=fake)
    result = await provider.send_email(
        from_address="a@b.com",
        to_addresses=["c@d.com"],
        subject="s",
        body_text="t",
        body_html=None,
    )
    assert result.provider_message_id == "mid-no-config"
    assert fake.kwargs is not None
    assert "ConfigurationSetName" not in fake.kwargs


async def test_send_email_without_configuration_set_raw_path(monkeypatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "hail_ses_configuration_set", "")

    fake = FakeClient("m-no-config")
    provider = SesEmailProvider(client=fake)
    result = await provider.send_email(
        from_address="forwarder+acme@mail.hail.so",
        to_addresses=["ops@example.com"],
        subject="Fwd: invoice",
        body_text="see attached",
        body_html=None,
        attachments=[
            ProviderAttachment(
                filename="invoice.pdf",
                content_type="application/pdf",
                payload=b"%PDF-1.4",
            )
        ],
    )

    assert result.provider_message_id == "m-no-config"
    assert fake.kwargs is not None
    assert "ConfigurationSetName" not in fake.kwargs


# ---------------------------------------------------------- create_identity


async def test_create_identity_sets_mail_from_and_returns_all_records(
    ses_client, stub: Stubber
) -> None:
    from hailhq.core.providers.email.base import DnsRecord

    stub.add_response(
        "create_email_identity",
        {
            "IdentityType": "DOMAIN",
            "DkimAttributes": {
                "Tokens": ["aaaaa", "bbbbb", "ccccc"],
                "Status": "PENDING",
            },
        },
        {"EmailIdentity": "acme.com"},
    )
    stub.add_response(
        "put_email_identity_mail_from_attributes",
        {},
        {
            "EmailIdentity": "acme.com",
            "MailFromDomain": "send.acme.com",
            "BehaviorOnMxFailure": "USE_DEFAULT_VALUE",
        },
    )

    provider = SesEmailProvider(client=ses_client)
    identity = await provider.create_identity("acme.com")

    assert identity.mail_from_domain == "send.acme.com"
    assert identity.mail_from_status == "pending"
    assert identity.verification_status == "pending"
    # 3 DKIM CNAMEs ...
    assert (
        DnsRecord(name="aaaaa._domainkey.acme.com", value="aaaaa.dkim.amazonses.com")
        in identity.dkim_records
    )
    # ... plus the MAIL FROM MX (region from the us-east-1 test client) ...
    assert (
        DnsRecord(
            name="send.acme.com",
            value="feedback-smtp.us-east-1.amazonses.com",
            type="MX",
            priority=10,
        )
        in identity.dkim_records
    )
    # ... plus the SPF TXT.
    assert (
        DnsRecord(
            name="send.acme.com", value="v=spf1 include:amazonses.com ~all", type="TXT"
        )
        in identity.dkim_records
    )
    assert len(identity.dkim_records) == 5
    stub.assert_no_pending_responses()


async def test_create_identity_returns_dkim_cnames(ses_client, stub: Stubber) -> None:
    stub.add_response(
        "create_email_identity",
        {
            "IdentityType": "DOMAIN",
            "DkimAttributes": {
                "Tokens": ["aaaaa", "bbbbb", "ccccc"],
                "Status": "PENDING",
            },
        },
        {"EmailIdentity": "acme.com"},
    )
    stub.add_response(
        "put_email_identity_mail_from_attributes",
        {},
        {
            "EmailIdentity": "acme.com",
            "MailFromDomain": "send.acme.com",
            "BehaviorOnMxFailure": "USE_DEFAULT_VALUE",
        },
    )

    provider = SesEmailProvider(client=ses_client)
    identity = await provider.create_identity("acme.com")

    assert isinstance(identity, ProviderIdentity)
    assert identity.domain == "acme.com"
    assert identity.verification_status == "pending"
    for token in ("aaaaa", "bbbbb", "ccccc"):
        assert (
            DkimRecord(
                name=f"{token}._domainkey.acme.com", value=f"{token}.dkim.amazonses.com"
            )
            in identity.dkim_records
        )
    stub.assert_no_pending_responses()


# ------------------------------------------------------------- get_identity


async def test_get_identity_maps_success_to_verified(ses_client, stub: Stubber) -> None:
    stub.add_response(
        "get_email_identity",
        {
            "IdentityType": "DOMAIN",
            "VerificationStatus": "SUCCESS",
            "DkimAttributes": {
                "Status": "SUCCESS",
                "Tokens": ["aaaaa", "bbbbb", "ccccc"],
            },
            "MailFromAttributes": {
                "MailFromDomain": "bounces.acme.com",
                "MailFromDomainStatus": "SUCCESS",
                "BehaviorOnMxFailure": "USE_DEFAULT_VALUE",
            },
        },
        {"EmailIdentity": "acme.com"},
    )

    provider = SesEmailProvider(client=ses_client)
    identity = await provider.get_identity("acme.com")
    assert identity.verification_status == "verified"
    assert identity.mail_from_domain == "bounces.acme.com"


async def test_get_identity_treats_temporary_failure_as_pending(
    ses_client, stub: Stubber
) -> None:
    # SES sometimes returns TEMPORARY_FAILURE while it retries verification —
    # we keep that row in 'pending' so the next /verify call can flip it.
    stub.add_response(
        "get_email_identity",
        {
            "IdentityType": "DOMAIN",
            "VerificationStatus": "TEMPORARY_FAILURE",
            "DkimAttributes": {
                "Status": "TEMPORARY_FAILURE",
                "Tokens": ["aaaaa", "bbbbb", "ccccc"],
            },
        },
        {"EmailIdentity": "acme.com"},
    )

    provider = SesEmailProvider(client=ses_client)
    identity = await provider.get_identity("acme.com")
    assert identity.verification_status == "pending"


async def test_get_identity_maps_mail_from_status(ses_client, stub: Stubber) -> None:
    stub.add_response(
        "get_email_identity",
        {
            "IdentityType": "DOMAIN",
            "VerificationStatus": "SUCCESS",
            "DkimAttributes": {
                "Status": "SUCCESS",
                "Tokens": ["aaaaa", "bbbbb", "ccccc"],
            },
            "MailFromAttributes": {
                "MailFromDomain": "send.acme.com",
                "MailFromDomainStatus": "PENDING",
                "BehaviorOnMxFailure": "USE_DEFAULT_VALUE",
            },
        },
        {"EmailIdentity": "acme.com"},
    )
    provider = SesEmailProvider(client=ses_client)
    identity = await provider.get_identity("acme.com")
    assert identity.verification_status == "verified"  # DKIM SUCCESS
    assert identity.mail_from_status == "pending"  # MAIL FROM still PENDING
    # get_identity must return all 5 records (3 DKIM + MX + SPF TXT) so that
    # mail_from records are never lost when the stored dns_records are refreshed.
    assert len(identity.dkim_records) == 5
    assert (
        DkimRecord(
            name="send.acme.com",
            value="feedback-smtp.us-east-1.amazonses.com",
            type="MX",
            priority=10,
        )
        in identity.dkim_records
    )
    assert (
        DkimRecord(
            name="send.acme.com",
            value="v=spf1 include:amazonses.com ~all",
            type="TXT",
        )
        in identity.dkim_records
    )
    stub.assert_no_pending_responses()


async def test_get_identity_missing_raises_lookup_error(
    ses_client, stub: Stubber
) -> None:
    stub.add_client_error(
        "get_email_identity",
        service_error_code="NotFoundException",
        expected_params={"EmailIdentity": "missing.com"},
    )

    provider = SesEmailProvider(client=ses_client)
    with pytest.raises(LookupError, match="missing.com"):
        await provider.get_identity("missing.com")


# ------------------------------------------------------------ delete_identity


async def test_delete_identity_idempotent_on_missing(ses_client, stub: Stubber) -> None:
    stub.add_client_error(
        "delete_email_identity",
        service_error_code="NotFoundException",
        expected_params={"EmailIdentity": "gone.com"},
    )
    provider = SesEmailProvider(client=ses_client)
    # Should not raise — delete is idempotent on already-removed identities.
    await provider.delete_identity("gone.com")
    stub.assert_no_pending_responses()


async def test_delete_identity_happy_path(ses_client, stub: Stubber) -> None:
    stub.add_response("delete_email_identity", {}, {"EmailIdentity": "acme.com"})
    provider = SesEmailProvider(client=ses_client)
    await provider.delete_identity("acme.com")
    stub.assert_no_pending_responses()
