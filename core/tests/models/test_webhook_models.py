from hailhq.core.models import WebhookDelivery, WebhookSubscription


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
    assert "webhook_deliveries_target_check" in names
    assert "webhook_deliveries_status_check" in names


def test_delivery_subscription_or_domain_both_nullable():
    assert WebhookDelivery.__table__.c.subscription_id.nullable is True
    assert WebhookDelivery.__table__.c.email_domain_id.nullable is True
