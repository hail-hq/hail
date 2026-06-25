from hailhq.core.models import EmailDomain


def test_email_domain_has_no_webhook_columns():
    cols = set(EmailDomain.__table__.columns.keys())
    assert "webhook_url" not in cols
    assert "webhook_secret_encrypted" not in cols
