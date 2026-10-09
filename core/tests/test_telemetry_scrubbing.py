"""Tool-call arguments that carry personal data must not reach traces.

``logfire.instrument_mcp()`` records every MCP request, including the
arguments of ``tools/call``. ``submit_verification`` arguments hold identity
details and base64 documents, so the scrubbing options redact them.
"""

from __future__ import annotations

import json

import logfire
from hailhq.core import telemetry
from logfire.testing import TestExporter
from mcp.types import CallToolRequest, CallToolRequestParams
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


def _spans_for(arguments: dict, tool: str) -> str:
    exporter = TestExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        additional_span_processors=[SimpleSpanProcessor(exporter)],
        scrubbing=telemetry.scrubbing_options(),
    )
    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=tool, arguments=arguments),
    )
    with logfire.span("MCP server handle request", request=request):
        pass
    return json.dumps(exporter.exported_spans_as_dict())


def test_submit_verification_arguments_are_scrubbed() -> None:
    out = _spans_for(
        {
            "attest_authorized": True,
            "fields": {"first_name": "SENTINEL_NAME_123"},
            "files": [{"slot": "identity", "content_base64": "SENTINEL_B64"}],
        },
        "submit_verification",
    )
    assert "SENTINEL_NAME_123" not in out
    assert "SENTINEL_B64" not in out
    assert "submit_verification" in out


def test_other_tool_arguments_are_still_recorded() -> None:
    out = _spans_for({"country_code": "GB"}, "quote_numbers")
    assert "GB" in out


def test_text_containing_the_word_arguments_is_kept() -> None:
    exporter = TestExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        additional_span_processors=[SimpleSpanProcessor(exporter)],
        scrubbing=telemetry.scrubbing_options(),
    )
    with logfire.span("check", note="bad arguments for call"):
        pass
    assert "bad arguments for call" in json.dumps(exporter.exported_spans_as_dict())
