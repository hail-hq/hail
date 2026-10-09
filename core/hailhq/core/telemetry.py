"""Optional EU Logfire telemetry shared by every backend process.

A filtering OTLP exporter keeps conversation/tool content out of native LiveKit
spans too (Agents 1.6 lacks the newer allow_pii option). No exporter is created
unless explicitly enabled. Existing console logging is preserved.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import logfire
from hailhq.core.config import settings
from hailhq.core.telemetry_identity import (
    IDENTITY_FIELDS,
    IdentitySpanProcessor,
    get_identity,
)
from hailhq.core.urls import join_url
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Link, Status

logger = logging.getLogger(__name__)
_configured_pid: int | None = None
_span_processor: BatchSpanProcessor | None = None
_extra_providers: list[TracerProvider] = []
_CONTENT = re.compile(
    r"(?:^|[._])(?:arguments|result|body|content|messages|"
    r"input_messages|output_messages|system_instructions|tool_definitions|"
    r"chat_ctx|instructions|transcript|user_input|input_text|user_transcript|text|audio|"
    r"tool_call_arguments|tool_call_result|function_call|function_call_output|"
    r"phone|email|otp|authorization|cookie|token|secret|api_key)(?:$|[._])",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w-])\+\d[\d ()-]{7,}\d")
_SECRET = re.compile(
    r"(?i)(bearer\s+\S+|(?:api[_-]?key|token|secret|password|otp)[=:]\s*\S+)"
)


def _safe_text(value: str) -> str:
    return _SECRET.sub(
        "[redacted]", _PHONE.sub("[phone]", _EMAIL.sub("[email]", value))
    )


def _safe_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    result = {}
    for key, value in (attributes or {}).items():
        if key in IDENTITY_FIELDS:
            result[key] = value
            continue
        if key in {
            "request",
            "response",
            "lk.session_options",
            "lk.function_tools",
            "lk.provider_tools",
            "lk.tool_sets",
            "lk.function_tool.output",
            "lk.participant_identity",
            "lk.participant_id",
            "lk.amd.reason",
            "exception.message",
            "exception.stacktrace",
        } or key.startswith("lk.pii."):
            continue
        if (
            _CONTENT.search(key)
            and not key.startswith("gen_ai.usage.")
            and key
            not in {
                "gen_ai.request.model",
                "gen_ai.response.model",
                "email_id",
                "lk.transcript_confidence",
            }
        ):
            continue
        if key in {"http.url", "url.full"} and isinstance(value, str):
            parts = urlsplit(value)
            # Drop userinfo, query and fragment, which can carry signed URLs/OTPs.
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            value = urlunsplit((parts.scheme, host, parts.path, "", ""))
        elif key in {"url.query", "http.target", "db.statement", "db.query.text"}:
            # SQL can contain literals; preserve operation/timing, not query text.
            continue
        if isinstance(value, str):
            value = _safe_text(value)
        elif isinstance(value, (tuple, list)):
            value = tuple(_safe_text(v) if isinstance(v, str) else v for v in value)
        result[key] = value
    return result


def private_span(
    span: ReadableSpan, resource_attributes: Mapping[str, Any] | None = None
) -> ReadableSpan:
    """Copy, rather than mutate, spans before the export boundary."""
    return ReadableSpan(
        name=_safe_text(span.name),
        context=span.context,
        parent=span.parent,
        resource=(span.resource or Resource({})).merge(
            Resource(resource_attributes or {})
        ),
        attributes=_safe_attributes(span.attributes),
        events=[
            Event(_safe_text(e.name), _safe_attributes(e.attributes), e.timestamp)
            for e in span.events
            if not e.name.startswith("gen_ai.")
        ],
        links=[
            Link(link.context, _safe_attributes(link.attributes)) for link in span.links
        ],
        kind=span.kind,
        status=Status(
            span.status.status_code,
            _safe_text(span.status.description) if span.status.description else None,
        ),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class PrivateSpanExporter(SpanExporter):
    def __init__(
        self,
        delegate: SpanExporter,
        resource_attributes: Mapping[str, Any] | None = None,
    ) -> None:
        self.delegate = delegate
        self.resource_attributes = resource_attributes

    def export(self, spans: Sequence[ReadableSpan]):
        filtered = [
            private_span(span, self.resource_attributes)
            for span in spans
            if (span.attributes or {}).get("http.route") != "/healthz"
        ]
        if not filtered:
            return SpanExportResult.SUCCESS
        return self.delegate.export(filtered)

    def shutdown(self) -> None:
        self.delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.delegate.force_flush(timeout_millis)


class BackendLoggingHandler(logfire.LogfireLoggingHandler):
    def fill_attributes(self, record: logging.LogRecord) -> dict[str, Any]:
        attributes = super().fill_attributes(record)
        attributes.update(get_identity())
        return attributes


class BackendLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Avoid exporter recursion and redundant access logs/health probes.
        if record.name.startswith(("opentelemetry", "logfire", "uvicorn.access")):
            return False
        return record.levelno >= (
            logging.INFO if record.name.startswith("hailhq") else logging.WARNING
        )


def _scrub_identity(match: logfire.ScrubMatch):
    if (
        len(match.path) == 2
        and match.path[0] == "attributes"
        and match.path[1] in IDENTITY_FIELDS
    ):
        return match.value
    if match.pattern_match.group(0).lower() == "arguments":
        # The extra pattern below, not a default one. Keep the value, except
        # the arguments of submit_verification: they hold identity details
        # and documents.
        if isinstance(match.value, dict) and "attest_authorized" in match.value:
            return None
        return match.value
    return None


def scrubbing_options() -> logfire.ScrubbingOptions:
    # "arguments" is the key under which logfire.instrument_mcp() records the
    # arguments of a tools/call request.
    return logfire.ScrubbingOptions(
        extra_patterns=["arguments"], callback=_scrub_identity
    )


def configure_telemetry(service_name: str) -> bool:
    global _configured_pid, _span_processor
    if not settings.hail_logfire_enabled:
        return False
    if _configured_pid == os.getpid():
        return True
    if not settings.logfire_base_url:
        logger.warning("Telemetry disabled: LOGFIRE_BASE_URL is not set")
        return False
    token = settings.logfire_token
    if not token:
        credentials = (
            Path(settings.logfire_credentials_dir) / "logfire_credentials.json"
        )
        try:
            if credentials.is_symlink() or credentials.parent.is_symlink():
                raise RuntimeError("Logfire credentials must not be symlinks")
            token = json.loads(credentials.read_text())["token"]
        except Exception as exc:
            # Optional telemetry must never stop the service from starting.
            logger.warning("Telemetry disabled: no usable Logfire token (%s)", exc)
            return False
    processor = BatchSpanProcessor(
        PrivateSpanExporter(
            OTLPSpanExporter(
                endpoint=join_url(settings.logfire_base_url, "v1/traces"),
                headers={"Authorization": token},
                timeout=5,
            ),
            resource_attributes={
                "service.name": service_name,
                "deployment.environment.name": settings.logfire_environment,
                "service.version": settings.logfire_service_version,
            },
        )
    )
    # One explicit exporter ensures every span passes through the privacy filter.
    logfire.configure(
        send_to_logfire=False,
        console=False,
        metrics=logfire.MetricsOptions(
            additional_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=join_url(settings.logfire_base_url, "v1/metrics"),
                        headers={"Authorization": token},
                        timeout=5,
                    ),
                    export_interval_millis=60000,
                )
            ]
        ),
        service_name=service_name,
        service_version=settings.logfire_service_version or None,
        environment=settings.logfire_environment,
        additional_span_processors=[IdentitySpanProcessor(), processor],
        sampling=logfire.SamplingOptions(head=settings.hail_logfire_sample_rate),
        scrubbing=scrubbing_options(),
        inspect_arguments=False,
        distributed_tracing=True,
    )
    handler = BackendLoggingHandler()
    handler.addFilter(BackendLogFilter())
    root = logging.getLogger()
    for existing in root.handlers[:]:
        if isinstance(existing, logfire.LogfireLoggingHandler):
            root.removeHandler(existing)
    root.addHandler(handler)
    logging.getLogger("hailhq").setLevel(logging.INFO)
    logfire.instrument_httpx()
    logfire.instrument_requests()
    logfire.instrument_aiohttp_client()
    _span_processor = processor
    _extra_providers.clear()
    _configured_pid = os.getpid()
    atexit.register(flush_telemetry)
    return True


def telemetry_enabled() -> bool:
    return _configured_pid == os.getpid()


def flush_telemetry() -> None:
    if telemetry_enabled():
        logfire.force_flush(timeout_millis=5000)
        for provider in _extra_providers:
            provider.force_flush(timeout_millis=5000)


@contextmanager
def operation(name: str, **attributes: Any):
    if not telemetry_enabled():
        yield None
        return
    with trace.get_tracer("hailhq").start_as_current_span(
        name, attributes=attributes
    ) as span:
        yield span


def share_native_provider(provider: TracerProvider) -> None:
    """Fan out existing native traces, preserving their original exporters."""
    if (
        telemetry_enabled()
        and _span_processor is not None
        and provider not in _extra_providers
    ):
        provider.add_span_processor(IdentitySpanProcessor())
        provider.add_span_processor(_span_processor)
        _extra_providers.append(provider)


def request_span_attributes(span, scope) -> None:
    if span.is_recording():
        span.set_attribute(
            "request_id", format(span.get_span_context().span_id, "016x")
        )
