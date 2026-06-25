from __future__ import annotations

import uuid

from hailhq.core.models import EmailDomain


def test_email_domain_has_no_webhook_columns():
    cols = set(EmailDomain.__table__.columns.keys())
    assert "webhook_url" not in cols
    assert "webhook_secret_encrypted" not in cols


def test_email_domain_has_dns_records_and_mail_from_status() -> None:
    sd = EmailDomain(
        organization_id=uuid.uuid4(),
        kind="custom",
        domain="acme.com",
        dns_records=[{"name": "x", "value": "y", "type": "CNAME"}],
        mail_from_status="pending",
    )
    assert sd.dns_records[0]["type"] == "CNAME"
    assert sd.mail_from_status == "pending"
    assert not hasattr(sd, "dkim_records")
