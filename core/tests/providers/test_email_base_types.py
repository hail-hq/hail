from __future__ import annotations

from hailhq.core.providers.email.base import DkimRecord, DnsRecord, ProviderIdentity


def test_dnsrecord_defaults_to_cname() -> None:
    r = DnsRecord(name="x._domainkey.acme.com", value="x.dkim.amazonses.com")
    assert r.type == "CNAME"
    assert r.priority is None


def test_dnsrecord_supports_mx_with_priority() -> None:
    r = DnsRecord(
        name="send.acme.com",
        value="feedback-smtp.us-east-1.amazonses.com",
        type="MX",
        priority=10,
    )
    assert r.type == "MX"
    assert r.priority == 10


def test_dkimrecord_is_dnsrecord_alias() -> None:
    assert DkimRecord is DnsRecord


def test_provider_identity_mail_from_status_optional() -> None:
    ident = ProviderIdentity(
        domain="acme.com", verification_status="pending", dkim_records=[]
    )
    assert ident.mail_from_status is None
