import pytest

from hailhq.core.providers.email.inbound import (
    InboundMessage,
    InboundProvider,
)


def test_inbound_message_required_fields():
    msg = InboundMessage(
        provider_message_id="abc",
        envelope_from="alice@example.com",
        envelope_recipients=["bob+acme@mail.hail.so"],
        raw_s3_bucket="hail-inbound",
        raw_s3_key="raw/abc",
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        received_at=None,
    )
    assert msg.provider_message_id == "abc"
    assert msg.envelope_recipients == ["bob+acme@mail.hail.so"]


def test_inbound_message_defaults_for_optional_fields():
    msg = InboundMessage(
        provider_message_id="abc",
        envelope_from="alice@example.com",
        envelope_recipients=["bob+acme@mail.hail.so"],
        raw_s3_bucket="b",
        raw_s3_key="k",
    )
    assert msg.spam_verdict is None
    assert msg.received_at is None


def test_inbound_provider_is_abstract():
    with pytest.raises(TypeError):
        InboundProvider()  # type: ignore[abstract]
