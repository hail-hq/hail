"""Voice job telemetry propagates API context and closes at job shutdown."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.voicebot import agent
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.mark.asyncio
async def test_job_trace_restores_parent_and_flushes_on_shutdown(monkeypatch):
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    monkeypatch.setattr(agent, "configure_telemetry", lambda _: True)
    monkeypatch.setattr(agent, "telemetry_enabled", lambda: True)
    monkeypatch.setattr(agent.trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(
        agent.trace, "get_tracer", lambda _: provider.get_tracer("test")
    )
    monkeypatch.setattr(agent, "connect_livekit_tracing", lambda: None)
    monkeypatch.setattr(agent, "flush_telemetry", lambda: None)
    callbacks = []
    ctx = SimpleNamespace(
        job=SimpleNamespace(
            metadata=json.dumps(
                {
                    "hail_actor_identity": {
                        "user_email": "actor@example.org",
                        "organization_name": "Example Workspace",
                    },
                    "hail_trace_context": {
                        "traceparent": "00-0000000000000000000000000000007b-00000000000001c8-01",
                    },
                }
            )
        ),
        add_shutdown_callback=callbacks.append,
    )

    async def run_call(ctx, **kwargs):
        with provider.get_tracer("test").start_as_current_span("native model call"):
            pass

    monkeypatch.setattr(agent, "_run_call", run_call)
    await agent.entrypoint(ctx)
    child = memory.get_finished_spans()[0]
    assert child.context.trace_id == 123
    assert child.parent.span_id != 456
    assert len(memory.get_finished_spans()) == 1
    await callbacks[0]()
    root = memory.get_finished_spans()[1]
    assert root.attributes["user_email"] == "actor@example.org"
    assert root.attributes["organization_name"] == "Example Workspace"
    from hailhq.core.telemetry_identity import get_identity

    assert get_identity() == {}
    assert root.parent.span_id == 456
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root.attributes["gen_ai.agent.name"] == "hail-voicebot"
    assert not trace.get_current_span().get_span_context().is_valid
    provider.shutdown()


@pytest.mark.asyncio
async def test_disabled_job_keeps_existing_behavior(monkeypatch):
    monkeypatch.setattr(agent, "configure_telemetry", lambda _: False)
    monkeypatch.setattr(agent, "telemetry_enabled", lambda: False)
    run = AsyncMock()
    monkeypatch.setattr(agent, "_run_call", run)
    ctx = object()
    await agent.entrypoint(ctx)
    run.assert_awaited_once_with(ctx)


def test_existing_livekit_cloud_provider_is_preserved(monkeypatch):
    from hailhq.voicebot import telemetry

    provider = TracerProvider()
    monkeypatch.setattr(telemetry, "tracer", SimpleNamespace(_tracer_provider=provider))
    mirrored = []
    monkeypatch.setattr(telemetry, "share_native_provider", mirrored.append)

    def unexpected(_):
        raise AssertionError("Existing provider must not be replaced")

    monkeypatch.setattr(telemetry, "set_tracer_provider", unexpected)
    telemetry.connect_livekit_tracing()
    assert mirrored == [provider]
    provider.shutdown()
