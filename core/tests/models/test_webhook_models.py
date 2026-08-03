from hailhq.core.models import WebhookDelivery, WebhookSubscription
from sqlalchemy import ForeignKey


def test_subscription_columns():
    cols = {c.name for c in WebhookSubscription.__table__.columns}
    assert {
        "target_url",
        "secret_encrypted",
        "event_types",
        "status",
        "consecutive_failures",
        "last_success_at",
        "last_failure_at",
    } <= cols


def test_subscription_constraints():
    names = {c.name for c in WebhookSubscription.__table__.constraints}
    assert "webhook_subscriptions_status_check" in names
    assert "webhook_subscriptions_event_types_nonempty" in names


def test_delivery_columns():
    cols = {c.name for c in WebhookDelivery.__table__.columns}
    assert {
        "subscription_id",
        "email_domain_id",
        "event_type",
        "event_id",
        "payload",
        "next_attempt_at",
        "status",
        "attempt",
    } <= cols


def test_delivery_constraints():
    names = {c.name for c in WebhookDelivery.__table__.constraints}
    # The degenerate target CHECK is gone — subscription ownership is now a
    # column-level NOT NULL (see migration 0014).
    assert "webhook_deliveries_target_check" not in names
    assert "webhook_deliveries_status_check" in names


def test_delivery_subscription_owned_domain_informational():
    # Every delivery is subscription-owned; the source domain is optional and
    # informational (stamps the X-Hail-Email-Domain header).
    assert WebhookDelivery.__table__.c.subscription_id.nullable is False
    assert WebhookDelivery.__table__.c.email_domain_id.nullable is True


def test_delivery_email_domain_fk_sets_null_on_delete():
    # email_domain_id is informational, so deleting a domain must NOT cascade
    # away delivery audit/retry rows.
    fks = [
        fk
        for fk in WebhookDelivery.__table__.c.email_domain_id.foreign_keys
        if isinstance(fk, ForeignKey)
    ]
    assert len(fks) == 1
    assert fks[0].ondelete == "SET NULL"
