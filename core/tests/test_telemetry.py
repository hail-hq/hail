"""Check the export boundary with the payload shapes emitted by LiveKit/MCP."""

import logging

from hailhq.core import telemetry
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, TraceFlags


def test_export_removes_content_but_preserves_trace_and_usage():
    context = SpanContext(123, 456, False, TraceFlags(1))
    original = ReadableSpan(
        "llm.chat",
        context=context,
        parent=context,
        attributes={
            "gen_ai.request.model": "example-model",
            "gen_ai.usage.input_tokens": 42,
            "gen_ai.usage.output_tokens": 10,
            "lk.response.ttft": 0.2,
            "user_email": "actor@example.org",
            "organization_name": "Example Workspace",
            "call_id": "call-123",
            "email_id": "email-123",
            "lk.chat_ctx": "PRIVATE CHAT",
            "lk.user_transcript": "PRIVATE TRANSCRIPT",
            "lk.input_text": "PRIVATE SPEECH",
            "lk.function_tool.arguments": "PRIVATE ARGS",
            "lk.function_tool.output": "PRIVATE OUTPUT",
            "lk.function_tools": "PRIVATE DEFINITIONS",
            "lk.pii.chat_ctx": "PRIVATE NEW SDK CHAT",
            "gen_ai.input.messages": "PRIVATE GENAI CHAT",
            "gen_ai.input_messages": "PRIVATE GENAI CHAT",
            "request": "PRIVATE MCP REQUEST",
            "response": "PRIVATE MCP RESPONSE",
            "db.statement": "SELECT 'PRIVATE SQL'",
            "url.full": "https://user:password@example.org/test?otp=123456#private",
            "logfire.msg": "Failed for +46701234567 and person@example.org Bearer abcdef",
        },
        events=[
            Event("gen_ai.user.message", {"anything": "PRIVATE EVENT"}, 1),
            Event(
                "exception",
                {
                    "exception.type": "TimeoutError",
                    "exception.message": "person@example.org",
                },
                2,
            ),
        ],
        start_time=1,
        end_time=2,
    )
    memory = InMemorySpanExporter()
    telemetry.PrivateSpanExporter(memory).export([original])
    exported = memory.get_finished_spans()[0]
    assert exported.context == context
    assert exported.parent == context
    assert exported.attributes["gen_ai.usage.input_tokens"] == 42
    assert exported.attributes["gen_ai.request.model"] == "example-model"
    assert exported.attributes["lk.response.ttft"] == 0.2
    assert exported.attributes["user_email"] == "actor@example.org"
    assert exported.attributes["organization_name"] == "Example Workspace"
    assert exported.attributes["call_id"] == "call-123"
    assert exported.attributes["email_id"] == "email-123"
    assert exported.attributes["url.full"] == "https://example.org/test"
    assert "PRIVATE" not in str(exported.attributes)
    assert "person@example.org" not in str(exported.attributes)
    assert "abcdef" not in str(exported.attributes)
    assert "+46701234567" not in str(exported.attributes)
    assert len(exported.events) == 1
    assert exported.events[0].attributes["exception.type"] == "TimeoutError"
    assert "exception.message" not in exported.events[0].attributes
    assert original.attributes["lk.chat_ctx"] == "PRIVATE CHAT"


def test_disabled_telemetry_does_not_construct_exporter(monkeypatch):
    monkeypatch.setattr(telemetry.settings, "hail_logfire_enabled", False)

    def unexpected(*args, **kwargs):
        raise AssertionError("disabled telemetry must never construct an exporter")

    monkeypatch.setattr(telemetry, "OTLPSpanExporter", unexpected)
    assert telemetry.configure_telemetry("hail-api") is False


def test_log_filter_keeps_app_info_and_dependency_errors():
    filter_ = telemetry.BackendLogFilter()

    def record(name, level):
        return logging.LogRecord(name, level, __file__, 1, "test", (), None)

    assert filter_.filter(record("hailhq.api", logging.INFO))
    assert filter_.filter(record("livekit.agents", logging.ERROR))
    assert not filter_.filter(record("httpx", logging.INFO))
    assert not filter_.filter(record("opentelemetry.exporter", logging.ERROR))
    assert not filter_.filter(record("uvicorn.access", logging.INFO))


def test_enabled_export_and_logging_are_idempotent_in_a_fresh_process():
    import os
    import subprocess
    import sys

    script = """
import logging
import logfire
from hailhq.core import telemetry
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
memory = InMemorySpanExporter()
telemetry.OTLPSpanExporter = lambda **kwargs: memory
telemetry.OTLPMetricExporter = lambda **kwargs: None
telemetry.PeriodicExportingMetricReader = lambda *args, **kwargs: InMemoryMetricReader()
assert telemetry.configure_telemetry("hail-api")
assert telemetry.configure_telemetry("hail-api")
from hailhq.core import db
db._ensure_initialized()
handlers = [h for h in logging.getLogger().handlers if isinstance(h, logfire.LogfireLoggingHandler)]
assert len(handlers) == 1
from hailhq.core.telemetry_identity import identity_scope
with identity_scope({"user_email": "secret.member@example.org", "organization_name": "Session Team"}), telemetry.operation("test.operation", call_id="test-call"):
    logging.getLogger("hailhq.test").info("Setup INFO log")
telemetry.flush_telemetry()
spans = memory.get_finished_spans()
assert any(s.name == "test.operation" for s in spans)
assert any(s.attributes.get("logfire.msg") == "Setup INFO log" for s in spans)
assert all(s.resource.attributes["service.name"] == "hail-api" for s in spans)
assert all(s.attributes.get("user_email") == "secret.member@example.org" for s in spans)
assert all(s.attributes.get("organization_name") == "Session Team" for s in spans)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "HAIL_LOGFIRE_ENABLED": "true",
            "LOGFIRE_TOKEN": "test-only-placeholder",
            "LOGFIRE_BASE_URL": "https://logfire.example.test",
        },
        text=True,
        check=False,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_native_provider_keeps_its_exporter_and_mirrors_private_spans(monkeypatch):
    import os

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    original = InMemorySpanExporter()
    mirror = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({"service.name": "livekit-agents"}))
    provider.add_span_processor(SimpleSpanProcessor(original))
    processor = SimpleSpanProcessor(
        telemetry.PrivateSpanExporter(mirror, {"service.name": "hail-voicebot"})
    )
    monkeypatch.setattr(telemetry, "_configured_pid", os.getpid())
    monkeypatch.setattr(telemetry, "_span_processor", processor)
    monkeypatch.setattr(telemetry, "_extra_providers", [])
    telemetry.share_native_provider(provider)
    telemetry.share_native_provider(provider)
    with provider.get_tracer("livekit-agents").start_as_current_span(
        "turn", attributes={"lk.user_input": "PRIVATE", "call_id": "test-call"}
    ):
        pass
    assert len(mirror.get_finished_spans()) == 1
    assert original.get_finished_spans()[0].attributes["lk.user_input"] == "PRIVATE"
    assert "lk.user_input" not in mirror.get_finished_spans()[0].attributes
    assert (
        mirror.get_finished_spans()[0].resource.attributes["service.name"]
        == "hail-voicebot"
    )
    provider.shutdown()
