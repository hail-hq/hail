from uuid import uuid4

from hailhq.core.schemas import (
    EmailAttachmentResponse,
    EmailDomainPatch,
    EmailDomainResponse,
    EmailResponse,
)


def test_email_response_has_inbound_fields():
    fields = EmailResponse.model_fields
    for name in (
        "direction",
        "message_id",
        "in_reply_to",
        "references_ids",
        "raw_url",
        "attachments",
        "spam_verdict",
        "virus_verdict",
        "dkim_verdict",
        "spf_verdict",
        "dmarc_verdict",
        "provider_received_at",
    ):
        assert name in fields, name


def test_email_domain_response_has_action_fields():
    fields = EmailDomainResponse.model_fields
    for name in (
        "inbound_enabled",
        "forward_to",
        "forward_rate_per_hour",
    ):
        assert name in fields


def test_email_domain_patch_allows_partial_updates():
    p = EmailDomainPatch(inbound_enabled=True)
    assert p.inbound_enabled is True
    assert p.forward_to is None


def test_attachment_response_round_trip():
    a = EmailAttachmentResponse(
        id=uuid4(),
        filename="a.pdf",
        content_type="application/pdf",
        size_bytes=10,
        content_id=None,
        url="https://api.hail.so/emails/x/attachments/y",
    )
    assert a.filename == "a.pdf"


def test_webhook_secret_hash_not_exposed_on_response():
    fields = EmailDomainResponse.model_fields
    assert "webhook_secret_hash" not in fields
