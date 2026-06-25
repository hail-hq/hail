from hailhq.core.models import Email, EmailAttachment, EmailDomain


def test_email_has_inbound_columns():
    cols = {c.name for c in Email.__table__.columns}
    assert {
        "direction",
        "message_id",
        "in_reply_to",
        "references_ids",
        "raw_s3_key",
        "spam_verdict",
        "virus_verdict",
        "dkim_verdict",
        "spf_verdict",
        "dmarc_verdict",
        "provider_received_at",
    } <= cols


def test_email_domain_id_nullable():
    assert Email.__table__.c.email_domain_id.nullable is True


def test_email_domain_has_action_columns():
    cols = {c.name for c in EmailDomain.__table__.columns}
    assert {
        "inbound_enabled",
        "forward_to",
        "forward_rate_per_hour",
    } <= cols


def test_email_attachments_table_exists():
    assert EmailAttachment.__table__.name == "email_attachments"


def test_email_direction_check_constraint_present():
    names = {c.name for c in Email.__table__.constraints}
    assert "emails_direction_check" in names
    assert "emails_outbound_has_domain" in names


def test_email_domains_inbound_action_constraint_present():
    names = {c.name for c in EmailDomain.__table__.constraints}
    assert "email_domains_inbound_action" in names
