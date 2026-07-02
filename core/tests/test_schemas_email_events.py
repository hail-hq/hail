"""Schema-level contracts for email deliverability events."""

from hailhq.core import schemas
from hailhq.core.email_delivery_events import _FANOUT_KINDS
from hailhq.core.providers.email.inbound.ses_delivery import _KIND_BY_EVENT_TYPE


def test_email_event_kind_values():
    assert set(schemas.EmailEventKind.__args__) == {
        "sent",
        "delivered",
        "delivery_delayed",
        "bounced",
        "complained",
        "rejected",
        "opened",
        "clicked",
    }


def test_email_status_includes_delivered():
    assert "delivered" in schemas.EmailStatus.__args__
    assert "delivered" in schemas.TERMINAL_EMAIL_STATUSES


def test_webhook_event_types_include_lifecycle():
    got = set(schemas.WebhookEventType.__args__)
    assert {
        "email.delivered",
        "email.delivery_delayed",
        "email.bounced",
        "email.complained",
        "email.opened",
        "email.clicked",
    } <= got
    assert "email.rejected" not in got


def test_parser_kinds_match_email_event_kind():
    """Every kind the SES parser can emit — plus the synthetic ``sent`` —
    must be exactly the ``EmailEventKind`` vocabulary, or DB rows (Text
    column) explode only at response-validation time."""
    assert set(_KIND_BY_EVENT_TYPE.values()) | {"sent"} == set(
        schemas.EmailEventKind.__args__
    )


def test_fanout_kinds_have_matching_webhook_event_types():
    """A fanout kind without an ``email.<kind>`` WebhookEventType would emit
    webhooks no subscription can ever match — silently zero deliveries."""
    assert {f"email.{k}" for k in _FANOUT_KINDS} <= set(
        schemas.WebhookEventType.__args__
    )


def test_events_stream_supports_email_resource():
    assert "email" in schemas.SUPPORTED_RESOURCE_TYPES
    rtype, _ = schemas.parse_resource_id("email:0e6f3f52-1c7e-4a75-9c67-000000000001")
    assert rtype == "email"


def test_event_response_shape():
    fields = set(schemas.EventResponse.model_fields)
    assert {
        "id",
        "source",
        "call_id",
        "email_id",
        "kind",
        "payload",
        "occurred_at",
    } <= fields


def test_email_stats_response_round_trips_wire_from_to():
    """The API emits ``from``/``to`` on the wire (serialization_alias); a
    consumer re-validating that same payload must not blow up with
    missing-field errors on those two keys.
    """
    wire = {
        "from": "2026-06-28T00:00:00Z",
        "to": "2026-06-30T00:00:00Z",
        "bucket": "day",
        "totals": {},
        "rates": {},
        "series": [],
    }
    parsed = schemas.EmailStatsResponse.model_validate(wire)
    dumped = parsed.model_dump(mode="json", by_alias=True)
    assert dumped["from"] == "2026-06-28T00:00:00Z"
    assert dumped["to"] == "2026-06-30T00:00:00Z"
    assert "from_ts" not in dumped and "to_ts" not in dumped


def test_email_stats_response_kwarg_construction_still_works():
    """The API route builds this model with ``from_ts=``/``to_ts=`` kwargs
    (populate_by_name); that construction path must keep working alongside
    the new wire-alias validation.
    """
    built = schemas.EmailStatsResponse(
        from_ts="2026-06-28T00:00:00Z",
        to_ts="2026-06-30T00:00:00Z",
        bucket="day",
        totals=schemas.EmailStatsCounts(),
        rates=schemas.EmailStatsRates(),
        series=[],
    )
    dumped = built.model_dump(mode="json", by_alias=True)
    assert dumped["from"] == "2026-06-28T00:00:00Z"
    assert dumped["to"] == "2026-06-30T00:00:00Z"
