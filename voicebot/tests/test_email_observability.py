"""Email outcomes must agree with persisted state and reach safe telemetry."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.voicebot import emailbot
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def telemetry(monkeypatch):
    from opentelemetry import metrics

    exporter = InMemorySpanExporter()
    traces = TracerProvider()
    traces.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(metrics, "get_meter", meters.get_meter)
    monkeypatch.setattr(
        emailbot,
        "operation",
        lambda name, **attrs: traces.get_tracer("test").start_as_current_span(
            name, attributes=attrs
        ),
    )
    yield exporter, reader
    traces.shutdown()
    meters.shutdown()


def points(reader):
    return {
        metric.name: metric.data.data_points
        for resource in reader.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


@pytest.mark.parametrize(
    "case,want",
    [
        ("done", "done"),
        ("skipped", "skipped"),
        ("empty", "skipped"),
        ("refused", "skipped"),
        ("failed", "failed"),
        ("retry", "retry"),
        ("exhausted", "failed"),
        ("model_error", "retry"),
        ("send_error", "retry"),
        ("bad_key", "failed"),
    ],
)
async def test_reply_reports_actual_outcome_with_bounded_metrics(
    monkeypatch, telemetry, case, want
):
    exporter, reader = telemetry
    claimed = SimpleNamespace(
        email=SimpleNamespace(
            id="private-email-id",
            requested_at=datetime.now(timezone.utc) - timedelta(seconds=60),
        ),
        attempt=3,
    )
    prepared = "sender_unverified" if case == "refused" else ([], [])
    monkeypatch.setattr(emailbot, "_prepare", AsyncMock(return_value=prepared))
    generation = AsyncMock(return_value="" if case == "empty" else "private reply")
    if case == "model_error":
        generation.side_effect = RuntimeError("private message")
    if case == "bad_key":
        generation.side_effect = emailbot.ProviderKeyError("bad key")
    monkeypatch.setattr(emailbot, "generate_reply", generation)
    state = "retry" if case == "exhausted" else case
    api = SimpleNamespace(post=AsyncMock(return_value={"state": state}))
    if case == "send_error":
        api.post.side_effect = RuntimeError("private body")
    # Database settlement is external here; exercise the runner's reporting of its result.
    monkeypatch.setattr(emailbot, "_settle", AsyncMock(return_value=want))
    with emailbot.operation("reply") as span:
        await emailbot._reply_claimed(api, claimed, span)
    recorded = exporter.get_finished_spans()[-1]
    assert recorded.attributes["reply.state"] == want
    if case in {"model_error", "send_error", "bad_key", "failed", "exhausted"}:
        assert recorded.status.status_code.name == "ERROR"
    emitted = points(reader)
    outcomes = emitted["hail.email.reply.attempts"][0]
    assert outcomes.value == 1
    assert dict(outcomes.attributes) == {"outcome": want}
    duration = emitted["hail.email.reply.duration"][0]
    assert duration.count == 1 and duration.sum >= 0
    assert dict(duration.attributes) == {"outcome": want}
    age = emitted["hail.email.reply.queue_age"][0]
    assert age.count == 1 and age.sum >= 60
    assert not age.attributes


@pytest.mark.parametrize(
    "persisted,want", [("pending", "retry"), ("failed", "failed"), (None, "superseded")]
)
async def test_settle_returns_actual_retry_state(monkeypatch, persisted, want):
    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(emailbot, "session_scope", session)
    monkeypatch.setattr(emailbot, "retry_reply", AsyncMock(return_value=persisted))
    assert await emailbot._settle(object(), None, retry=True) == want


async def test_manual_tool_span_has_no_arguments_or_result(monkeypatch, telemetry):
    exporter, _ = telemetry
    call = SimpleNamespace(name="search_emails", arguments='{"query":"private query"}')
    spec = SimpleNamespace(
        name="search_emails", execute=AsyncMock(return_value="private result")
    )
    output, error = await emailbot._run_tool(call, {call.name: spec}, object())
    assert output == "private result" and not error
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert dict(spans[0].attributes) == {
        "gen_ai.tool.name": "search_emails",
        "tool.outcome": "done",
    }
    assert spans[0].end_time >= spans[0].start_time


@pytest.mark.parametrize("known", [False, True])
async def test_tool_errors_are_visible_without_untrusted_tool_names(
    monkeypatch, telemetry, known
):
    exporter, _ = telemetry
    call = SimpleNamespace(
        name="read_email" if known else "private arbitrary model text", arguments="{}"
    )
    specs = (
        {
            "read_email": SimpleNamespace(
                name="read_email",
                execute=AsyncMock(side_effect=ValueError("private content")),
            )
        }
        if known
        else {}
    )
    output, error = await emailbot._run_tool(call, specs, object())
    assert error and output == emailbot.TOOL_FAILED
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code.name == "ERROR"
    assert span.attributes["gen_ai.tool.name"] == ("read_email" if known else "unknown")
    assert span.attributes["tool.outcome"] == ("failed" if known else "unavailable")


async def test_cancelled_attempt_is_recorded_and_cancellation_propagates(
    monkeypatch, telemetry
):
    import asyncio

    exporter, reader = telemetry
    claimed = SimpleNamespace(
        email=SimpleNamespace(id="private-id", requested_at=datetime.now(timezone.utc))
    )
    monkeypatch.setattr(
        emailbot, "_prepare", AsyncMock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError), emailbot.operation("reply") as span:
        await emailbot._reply_claimed(object(), claimed, span)
    assert exporter.get_finished_spans()[0].attributes["reply.state"] == "cancelled"
    assert points(reader)["hail.email.reply.attempts"][0].attributes == {
        "outcome": "cancelled"
    }
