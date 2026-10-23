"""Trace tags, exported LLM content and the MCP noise filter."""

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from hailhq.core import telemetry, telemetry_identity
from hailhq.core.models import EmailDomain, PhoneNumber
from hailhq.core.telemetry_identity import (
    IdentitySpanProcessor,
    bind_root,
    identity_scope,
    mask_party,
    tag_context,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, SpanKind, TraceFlags

FIX = Path(__file__).parent / "fixtures" / "inbound"

PROMPT = "You are Hail. Never read the caller's password or session code aloud."


def _span(name, kind=SpanKind.INTERNAL, **attributes):
    context = SpanContext(11, 22, False, TraceFlags(1))
    return ReadableSpan(
        name,
        context=context,
        attributes=attributes,
        kind=kind,
        start_time=1,
        end_time=2,
    )


def _export(spans, service="hail-voicebot"):
    memory = InMemorySpanExporter()
    telemetry.PrivateSpanExporter(memory, {"service.name": service}).export(spans)
    return memory.get_finished_spans()


@pytest.fixture
def traced(monkeypatch):
    """A provider wired like production: identity stamping, then the
    private exporter. Telemetry counts as enabled."""
    memory = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({"service.name": "hail-api"}))
    provider.add_span_processor(IdentitySpanProcessor())
    provider.add_span_processor(
        SimpleSpanProcessor(telemetry.PrivateSpanExporter(memory))
    )
    monkeypatch.setattr(telemetry, "_configured_pid", os.getpid())
    root_token = telemetry_identity._root.set(None)
    yield provider, memory
    telemetry_identity._root.reset(root_token)
    provider.shutdown()


@pytest.mark.parametrize(
    ("value", "masked"),
    [
        ("+46701234567", "+46 ***4567"),
        ("+14155551234", "+1 ***1234"),
        ("person@example.com", "***@example.com"),
        ("0701234567", "***4567"),
        ("123", "***"),
    ],
)
def test_mask_party_keeps_country_code_last_four_or_domain(value, masked):
    assert mask_party(value) == masked


def test_llm_content_passes_unmasked():
    content = {
        "gen_ai.system_instructions": PROMPT,
        "gen_ai.input.messages": '[{"role": "user", "parts": "mail a@example.org"}]',
        "gen_ai.output.messages": '[{"role": "assistant", "parts": "Sure."}]',
        "gen_ai.tool.call.arguments": '{"to": "+46701234567"}',
        "gen_ai.tool.call.result": '{"ok": true}',
        "lk.pii.user_transcript": "call me on +46701234567",
    }
    exported = _export(
        [_span("llm_request", **content, **{"lk.pii.chat_ctx": "PRIVATE"})]
    )[0]
    assert {k: exported.attributes[k] for k in content} == content
    # Only the listed keys open up: other content stays out.
    assert "lk.pii.chat_ctx" not in exported.attributes


def test_logfire_scrubber_keeps_content_and_tags():
    import logfire

    def match(*path):
        found = re.search("password", "password")
        return logfire.ScrubMatch(path, "x password", found)

    assert telemetry._scrub_identity(match("attributes", "hail.to")) == "x password"
    assert (
        telemetry._scrub_identity(
            match("attributes", "gen_ai.tool.call.arguments", "secret")
        )
        == "x password"
    )
    assert telemetry._scrub_identity(match("attributes", "lk.pii.chat_ctx")) is None


def test_tags_reach_child_spans_livekit_spans_root_and_logs(traced):
    provider, memory = traced
    # A second provider stands in for LiveKit's own, as share_native_provider wires it.
    livekit = TracerProvider(resource=Resource({"service.name": "livekit-agents"}))
    livekit.add_span_processor(IdentitySpanProcessor())
    livekit_memory = InMemorySpanExporter()
    livekit.add_span_processor(
        SimpleSpanProcessor(telemetry.PrivateSpanExporter(livekit_memory))
    )
    tracer = provider.get_tracer("hailhq")
    with (
        identity_scope({"organization_id": "org-1", "organization_name": "Acme"}),
        tracer.start_as_current_span("job_entrypoint"),
    ):
        bind_root()
        with tracer.start_as_current_span("invoke_agent"):
            tag_context(
                channel="voice",
                direction="outbound",
                agent_id="agent-1",
                call_id="call-1",
                from_="+46812345678",
                to="+46701234567",
            )
            with livekit.get_tracer("livekit-agents").start_as_current_span(
                "llm_request"
            ):
                pass
            record = logging.LogRecord(
                "hailhq.test", logging.INFO, __file__, 1, "log", (), None
            )
            log_attributes = telemetry.BackendLoggingHandler().fill_attributes(record)
    expected = {
        "organization_id": "org-1",
        "organization_name": "Acme",
        "hail.channel": "voice",
        "hail.direction": "outbound",
        "hail.agent_id": "agent-1",
        "hail.call_id": "call-1",
        "hail.from": "+46812345678",
        "hail.to": "+46 ***4567",
    }
    spans = {s.name: s for s in memory.get_finished_spans()}
    llm = livekit_memory.get_finished_spans()[0]
    for span in (spans["job_entrypoint"], spans["invoke_agent"], llm):
        assert {k: span.attributes.get(k) for k in expected} == expected
    assert {k: log_attributes.get(k) for k in expected} == expected
    assert spans["job_entrypoint"].attributes["logfire.tags"] == (
        "voice",
        "outbound",
    )
    livekit.shutdown()


def test_inbound_masks_the_sender_and_keeps_the_hail_number():
    with identity_scope({}):
        tag_context(
            channel="sms", direction="inbound", from_="+46701234567", to="+4681234"
        )
        tags = telemetry_identity.get_tags()
    assert tags["hail.from"] == "+46 ***4567"
    assert tags["hail.to"] == "+4681234"
    assert telemetry_identity.get_tags() == {}


@pytest.mark.parametrize(
    "name, attributes",
    [
        ("MCP server handle request: ping", {}),
        ("MCP server handle request: tools/list", {}),
        ("MCP server handle request: prompts/list", {}),
        ("MCP server handle request: resources/list", {}),
        ("MCP server handle request: resources/templates/list", {}),
        ("GET /", {"http.route": "/"}),
        (
            "GET /.well-known/oauth-protected-resource",
            {"http.route": "/.well-known/oauth-protected-resource"},
        ),
        ("GET /.well-known/glama.json", {"http.route": "/.well-known/glama.json"}),
        ("POST /", {"http.route": "/", "http.status_code": 200}),
        ("POST /", {"http.route": "/", "http.response.status_code": 202}),
        ("DELETE /", {"http.route": "/", "http.status_code": 200}),
    ],
)
def test_mcp_noise_is_not_exported(name, attributes):
    assert _export([_span(name, **attributes)], service="hail-mcp") == ()


def test_mcp_scanner_404_is_not_exported_but_client_404_is():
    probe = _span("GET", kind=SpanKind.SERVER, **{"http.status_code": 404})
    client = _span("GET", kind=SpanKind.CLIENT, **{"http.status_code": 404})
    kept = _export([probe, client], service="hail-mcp")
    assert [s.kind for s in kept] == [SpanKind.CLIENT]


def test_mcp_tool_calls_and_other_services_are_exported():
    kept = _export(
        [
            _span(
                "MCP server handle request: tools/call",
                **{"mcp.tool.name": "place_call", "hail.channel": "mcp"},
            ),
            # A failed POST stays: it is where a 401 shows.
            _span("POST /", **{"http.route": "/", "http.status_code": 401}),
            _span("DELETE /", **{"http.route": "/", "http.status_code": 404}),
        ],
        service="hail-mcp",
    )
    assert [s.name for s in kept] == [
        "MCP server handle request: tools/call",
        "POST /",
        "DELETE /",
    ]
    assert kept[0].attributes["mcp.tool.name"] == "place_call"
    assert kept[0].attributes["hail.channel"] == "mcp"
    # The same route on another service is not MCP traffic.
    assert len(_export([_span("GET /", **{"http.route": "/"})], "hail-api")) == 1


async def test_inbound_sms_span_carries_org_channel_and_direction(
    async_session, traced
):
    from hailhq.core.sms_ingest import ingest_inbound_sms

    provider, memory = traced
    org_id = uuid.uuid4()
    async_session.add(
        PhoneNumber(
            organization_id=org_id,
            e164="+14155559999",
            country_code="US",
            number_type="local",
            provider_resource_id="PN_tags",
            provisioning_state="active",
        )
    )
    await async_session.commit()
    tracer = provider.get_tracer("hailhq")
    with identity_scope({}), tracer.start_as_current_span("POST /sms/twilio"):
        bind_root()
        result = await ingest_inbound_sms(
            async_session,
            carrier="twilio",
            from_e164="+14155551234",
            to_e164="+14155559999",
            body="hello",
            provider_message_sid="SM_tags",
            opt_out_type=None,
        )
        with tracer.start_as_current_span("after"):
            pass
    spans = {s.name: s for s in memory.get_finished_spans()}
    for name in ("POST /sms/twilio", "after"):
        attributes = spans[name].attributes
        assert attributes["organization_id"] == str(org_id)
        assert attributes["hail.channel"] == "sms"
        assert attributes["hail.direction"] == "inbound"
        assert attributes["hail.sms_id"] == str(result.sms_id)
        assert attributes["hail.from"] == "+1 ***1234"
        assert attributes["hail.to"] == "+14155559999"


async def test_inbound_email_span_carries_org_channel_and_direction(
    async_session, traced
):
    from hailhq.core.email_ingest import ingest_inbound
    from hailhq.core.providers.email.inbound.base import InboundMessage

    provider, memory = traced
    org_id = uuid.uuid4()
    async_session.add(
        EmailDomain(
            organization_id=org_id,
            kind="custom",
            domain="mail.tags.example",
            verification_status="verified",
            provider="ses",
            inbound_enabled=True,
            forward_to=["ops@example.com"],
        )
    )
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "multipart_attachment.eml").read_bytes()
    message = InboundMessage(
        provider_message_id="tags1",
        envelope_from="x@example.com",
        envelope_recipients=["support@mail.tags.example"],
        raw_s3_bucket="b",
        raw_s3_key="raw/tags1",
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    tracer = provider.get_tracer("hailhq")
    with identity_scope({}), tracer.start_as_current_span("POST /internal/ses"):
        bind_root()
        result = await ingest_inbound(
            async_session,
            message=message,
            s3=s3,
            hail_mail_base_domain="mail.hail.so",
            org_rate_per_hour=10_000,
        )
    attributes = memory.get_finished_spans()[0].attributes
    assert attributes["organization_id"] == str(org_id)
    assert attributes["hail.channel"] == "email"
    assert attributes["hail.direction"] == "inbound"
    assert attributes["hail.email_id"] == str(result.email_ids[0])
    assert attributes["hail.to"] == "support@mail.tags.example"
    assert attributes["hail.from"].startswith("***@")


def test_configured_pipeline_exports_content_unscrubbed_in_a_fresh_process():
    """Through logfire.configure: its scrubber must not blank a prompt that
    says "password", and the tags must survive."""
    import subprocess
    import sys

    script = """
import json
from hailhq.core import telemetry
from hailhq.core.telemetry_identity import identity_scope, tag_context
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
memory = InMemorySpanExporter()
telemetry.OTLPSpanExporter = lambda **kwargs: memory
telemetry.OTLPMetricExporter = lambda **kwargs: None
telemetry.PeriodicExportingMetricReader = lambda *args, **kwargs: InMemoryMetricReader()
assert telemetry.configure_telemetry("hail-voicebot")
prompt = "Never say the password or the session code. " + "x" * 20000
args = json.dumps({"password": "hunter2"})
with identity_scope({"organization_id": "org-1"}):
    tag_context(channel="voice", direction="inbound", from_="+46701234567")
    with trace.get_tracer("livekit-agents").start_as_current_span(
        "llm_request",
        attributes={
            "gen_ai.system_instructions": prompt,
            "gen_ai.tool.call.arguments": args,
            "lk.pii.user_transcript": "my password is hunter2",
        },
    ):
        pass
telemetry.flush_telemetry()
span = next(s for s in memory.get_finished_spans() if s.name == "llm_request")
assert span.attributes["gen_ai.system_instructions"] == prompt, span.attributes
assert span.attributes["gen_ai.tool.call.arguments"] == args, span.attributes
assert span.attributes["lk.pii.user_transcript"] == "my password is hunter2"
assert span.attributes["hail.from"] == "+46 ***4567"
assert span.attributes["hail.channel"] == "voice"
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
