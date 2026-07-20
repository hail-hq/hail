from uuid import uuid4

import pytest
from pydantic import ValidationError

from hailhq.core.schemas import (
    WebhookSubscriptionCreate,
    WebhookSubscriptionPatch,
    WebhookSubscriptionResponse,
)


def test_create_requires_at_least_one_event():
    with pytest.raises(ValidationError):
        WebhookSubscriptionCreate(target_url="https://example.com/h", event_types=[])


def test_create_rejects_unknown_event_type():
    with pytest.raises(ValidationError):
        WebhookSubscriptionCreate(
            target_url="https://example.com/h",
            event_types=["call.ringing"],  # type: ignore[arg-type]
        )


def test_create_accepts_email_received():
    sub = WebhookSubscriptionCreate(
        target_url="https://example.com/h", event_types=["email.received"]
    )
    assert sub.event_types == ["email.received"]


def test_patch_allows_partial():
    p = WebhookSubscriptionPatch(status="disabled")
    assert p.status == "disabled"
    assert p.target_url is None


def test_response_secret_is_optional_and_default_none():
    fields = WebhookSubscriptionResponse.model_fields
    assert "secret" in fields
    # Build a minimal instance: secret should default to None when not provided.
    from datetime import datetime, timezone

    r = WebhookSubscriptionResponse(
        id=uuid4(),
        organization_id=uuid4(),
        target_url="https://example.com/h",
        event_types=["email.received"],
        status="active",
        consecutive_failures=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert r.secret is None
