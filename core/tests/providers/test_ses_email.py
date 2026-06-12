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
    # Raw MIME bytes contain a nondeterministic multipart boundary, so the
    # Stubber's exact-params matching can't pin them. Record the call shape
    # with a fake client instead and assert on the raw payload directly.
    class FakeClient:
        def __init__(self) -> None:
            self.kwargs: dict | None = None

        def send_email(self, **kwargs):
            self.kwargs = kwargs
            return {"MessageId": "m-2"}

    fake = FakeClient()
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


# ---------------------------------------------------------- create_identity


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

    provider = SesEmailProvider(client=ses_client)
    identity = await provider.create_identity("acme.com")

    assert isinstance(identity, ProviderIdentity)
    assert identity.domain == "acme.com"
    assert identity.verification_status == "pending"
    assert identity.dkim_records == [
        DkimRecord(name="aaaaa._domainkey.acme.com", value="aaaaa.dkim.amazonses.com"),
        DkimRecord(name="bbbbb._domainkey.acme.com", value="bbbbb.dkim.amazonses.com"),
        DkimRecord(name="ccccc._domainkey.acme.com", value="ccccc.dkim.amazonses.com"),
    ]
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
