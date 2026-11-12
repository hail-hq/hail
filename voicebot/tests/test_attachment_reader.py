"""The worker inspects real file bytes, never exposes them as a tool result."""

import base64
import io
import json
import subprocess
import sys
import uuid
import zipfile
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.core import telemetry
from hailhq.core.agent_tools import read_email
from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.email_attachment_limits import text_chunk
from hailhq.voicebot import attachment_decoder, emailbot
from hailhq.voicebot.attachment_decoder import decode_file
from hailhq.voicebot.attachment_reader import inspect_tool_file
from hailhq.voicebot.tools import _make_handler
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from PIL import Image


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (80, 60), "red").save(buffer, "PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["voice", "email"])
async def test_visual_tool_result_contains_facts_not_binary_or_private_paths(
    monkeypatch,
    transport,
):
    payload = json.dumps(
        {
            "filename": "photo.png",
            "attachment_data": {
                "base64": base64.b64encode(png()).decode(),
                "content_type": "image/png",
                "page": 1,
            },
        }
    )

    async def execute(ctx, args):
        return payload

    async def available(org, db):
        return True

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def __aiter__(self):
            async def items():
                yield SimpleNamespace(
                    delta=SimpleNamespace(content="A red rectangle.", tool_calls=[])
                )

            return items()

    seen = []
    model = SimpleNamespace(chat=lambda **kwargs: (seen.append(kwargs) or Stream()))
    spec = ToolSpec(
        name="read_browser_inbox",
        description="",
        parameters={},
        risk_tier="read_only",
        is_available=available,
        execute=execute,
    )
    ctx = ToolContext(uuid.uuid4(), uuid.uuid4(), None, None, None)
    if transport == "voice":
        result = await _make_handler(spec, ctx)(
            {}, SimpleNamespace(session=SimpleNamespace(llm=model))
        )
    else:
        spec = replace(spec, name="read_email")
        ctx = replace(ctx, call_id=None, inbound_email_id=uuid.uuid4())
        result, error = await emailbot._run_tool(
            SimpleNamespace(name="read_email", arguments="{}"),
            {"read_email": spec},
            ctx,
            model=model,
        )
        assert error is False
    assert "base64" not in result and "attachment_data" not in result
    assert "red rectangle" in result
    assert seen and seen[0]["tool_choice"] == "none"


def pdf_bytes(pages=1):
    """Small real PDF fixture, no paid service or binary test asset needed."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{4+i*2} 0 R' for i in range(pages))}] /Count {pages} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index in range(pages):
        stream = f"BT /F1 18 Tf 30 100 Td (Receipt page {index+1}) Tj ET".encode()
        objects.extend(
            [
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 3 0 R >> >> /Contents {5+index*2} 0 R >>".encode(),
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream",
            ]
        )
    data = b"%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(data)
    data += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode() + b"".join(
        f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]
    )
    return (
        data
        + f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()
    )


def test_small_pdf_reads_all_pages_without_a_fixed_page_count():
    result = decode_file(
        {
            "base64": base64.b64encode(pdf_bytes(5)).decode(),
            "content_type": "application/pdf",
            "page": 1,
        }
    )
    assert "Receipt page 5" in result["text"]
    assert (
        len(result["images"]) == 5
        and result["pages"] == "1-5 of 5"
        and not result["partial"]
    )


def test_image_normalization_reads_pixels_without_mutating_the_original():
    original = png()
    result = decode_file(
        {"base64": base64.b64encode(original).decode(), "content_type": "image/png"}
    )
    assert result["images"][0].startswith("data:image/jpeg;base64,")
    assert original == png()


def test_word_documents_are_read_without_executing_xml_entities():
    def file(xml):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", xml)
        return {
            "base64": base64.b64encode(buffer.getvalue()).decode(),
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }

    assert (
        "ORDER-123"
        in decode_file(file("<document><p><t>ORDER-123</t></p></document>"))["text"]
    )
    with pytest.raises(ValueError):
        decode_file(
            file(
                '<!DOCTYPE doc [<!ENTITY x SYSTEM "file:///etc/passwd">]><document>&x;</document>'
            )
        )


@pytest.mark.asyncio
async def test_unreadable_files_never_return_binary_data_to_the_agent():
    result = await inspect_tool_file(
        json.dumps(
            {
                "filename": "broken.pdf",
                "attachment_data": {
                    "base64": base64.b64encode(b"not a PDF").decode(),
                    "content_type": "application/pdf",
                },
            }
        ),
        None,
    )
    assert "attachment_data" not in result and "base64" not in result
    assert "could not read" in result


def test_excel_shared_strings_are_resolved_to_their_cells():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", "<sst><si><t>Hello</t></si></sst>")
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet><row><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row></worksheet>',
        )
    result = decode_file(
        {
            "base64": base64.b64encode(buffer.getvalue()).decode(),
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
    )
    assert "A1: Hello" in result["text"] and "B1: 42" in result["text"]


def test_transparent_images_get_a_readable_white_background():
    buffer = io.BytesIO()
    Image.new("RGBA", (5, 5), (0, 0, 0, 0)).save(buffer, "PNG")
    result = decode_file(
        {
            "base64": base64.b64encode(buffer.getvalue()).decode(),
            "content_type": "image/png",
        }
    )
    data = base64.b64decode(result["images"][0].split(",", 1)[1])
    with Image.open(io.BytesIO(data)) as decoded:
        assert min(decoded.getpixel((0, 0))) > 245


@pytest.mark.asyncio
async def test_pdf_keeps_extracted_text_when_the_model_cannot_read_images():
    result = json.loads(
        await inspect_tool_file(
            json.dumps(
                {
                    "filename": "receipt.pdf",
                    "attachment_data": {
                        "base64": base64.b64encode(pdf_bytes()).decode(),
                        "content_type": "application/pdf",
                    },
                }
            ),
            None,
        )
    )
    assert "Receipt page 1" in result["quoted_untrusted_content"]
    assert result["visual_read"] is False and result["partial"] is True
    assert "attachment_data" not in result


@pytest.mark.asyncio
async def test_email_read_tool_cannot_replace_its_authenticated_source():
    inbound = uuid.uuid4()
    post = AsyncMock(return_value={"quoted_untrusted_messages": []})
    ctx = ToolContext(
        None,
        uuid.uuid4(),
        SimpleNamespace(post=post),
        None,
        None,
        inbound_email_id=inbound,
    )
    await read_email.SPEC.execute(
        ctx,
        {
            "inbound_email_id": str(uuid.uuid4()),
            "call_id": str(uuid.uuid4()),
            "organization_id": str(uuid.uuid4()),
        },
    )
    path, body = post.call_args.args
    assert path == "/internal/agent/email-run/inbox"
    assert body["inbound_email_id"] == str(inbound)
    assert "organization_id" not in body and "call_id" not in body


def test_pdf_byte_budget_returns_a_cursor_to_the_first_unread_page(monkeypatch):
    monkeypatch.setattr(attachment_decoder, "MAX_DECODED_BYTES", 15000, raising=False)
    data = {
        "base64": base64.b64encode(pdf_bytes(8)).decode(),
        "content_type": "application/pdf",
    }
    result = decode_file(data)
    assert len(json.dumps(result).encode()) <= 15000
    assert result["partial"] is True
    assert 1 < result["continuation"]["page"] < 8
    later = decode_file({**data, **result["continuation"]})
    assert f'Receipt page {result["continuation"]["page"]}' in later["text"]


def test_decoder_rejects_files_larger_than_ten_mib():
    with pytest.raises(ValueError, match="reading limit"):
        decode_file(
            {
                "base64": base64.b64encode(b"x" * (10 * 1024 * 1024 + 1)).decode(),
                "content_type": "image/png",
            }
        )


def test_word_text_has_byte_bounded_continuation_without_lost_characters():
    text = "é" * 25000 + "THE END"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml", f"<document><p><t>{text}</t></p></document>"
        )
    data = {
        "base64": base64.b64encode(buffer.getvalue()).decode(),
        "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    collected = ""
    for _ in range(5):
        result = decode_file(data)
        assert len(result["text"].encode()) <= 24000
        collected += result["text"]
        if not result.get("continuation"):
            break
        assert result["partial"] is True
        data.update(result["continuation"])
    assert collected == text


@pytest.mark.asyncio
async def test_reader_preserves_continuation_when_model_analysis_fails(monkeypatch):
    monkeypatch.setattr(attachment_decoder, "MAX_DECODED_BYTES", 15000)
    data = {
        "base64": base64.b64encode(pdf_bytes(8)).decode(),
        "content_type": "application/pdf",
    }

    async def local_decode(data):
        return decode_file(data)

    monkeypatch.setattr("hailhq.voicebot.attachment_reader.decoded_pages", local_decode)
    result = json.loads(
        await inspect_tool_file(json.dumps({"attachment_data": data}), None)
    )
    assert result["partial"] is True
    assert result["continuation"] == {"page": 2, "text_offset": 0}
    assert result["visual_read"] is False


def test_pdf_long_page_text_can_continue_on_the_same_page(monkeypatch):
    monkeypatch.setattr(attachment_decoder, "TEXT_BYTES", 10)
    # The default function budget is fixed; PDFs pass the current remaining budget.
    data = {
        "base64": base64.b64encode(pdf_bytes()).decode(),
        "content_type": "application/pdf",
    }
    first = decode_file(data)
    assert first["partial"] is True
    assert first["continuation"] == {"page": 1, "text_offset": 10}
    second = decode_file({**data, **first["continuation"]})
    assert first["text"] + second["text"] == "Receipt page 1"
    assert "continuation" not in second


def test_compressed_office_expansion_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * (5 * 1024 * 1024 + 1))
    assert len(buffer.getvalue()) < 10000
    with pytest.raises(ValueError, match="Expanded document"):
        decode_file(
            {
                "base64": base64.b64encode(buffer.getvalue()).decode(),
                "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [["é" * 15000], ["x" * 23999, "é"]])
async def test_long_model_answer_reports_partial_and_can_be_requested_again(parts):
    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            async def items():
                for part in parts:
                    yield SimpleNamespace(delta=SimpleNamespace(content=part))

            return items()

    result = json.loads(
        await inspect_tool_file(
            json.dumps(
                {
                    "attachment_data": {
                        "base64": base64.b64encode(png()).decode(),
                        "content_type": "image/png",
                    }
                }
            ),
            SimpleNamespace(chat=lambda **kwargs: Stream()),
        )
    )
    assert len(result["quoted_untrusted_content"].encode()) <= 24000
    assert result["partial"] is True
    assert result["analysis_incomplete"] is True
    assert result["retry"] == {"page": 1, "text_offset": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("timed_out", [False, True])
async def test_attachment_inspection_traces_outcomes_without_file_content(
    monkeypatch, timed_out
):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "telemetry_enabled", lambda: True)
    monkeypatch.setattr(telemetry.trace, "get_tracer", provider.get_tracer)
    if timed_out:

        async def timeout(data):
            raise TimeoutError()

        monkeypatch.setattr("hailhq.voicebot.attachment_reader.decoded_pages", timeout)
    await inspect_tool_file(
        json.dumps(
            {
                "attachment_data": {
                    "base64": base64.b64encode(png()).decode(),
                    "content_type": "image/png",
                    "question": "PRIVATE QUESTION",
                }
            }
        ),
        None,
    )
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["outcome"] == ("timeout" if timed_out else "unsupported")
    assert spans[0].attributes["duration_seconds"] >= 0
    assert spans[0].attributes["attachment_mime_type"] == "image/png"
    assert "PRIVATE QUESTION" not in str(spans[0].attributes)
    assert "base64" not in str(spans[0].attributes)
    provider.shutdown()


@pytest.mark.parametrize("offset", [-1, 1, 4])
def test_text_byte_cursor_rejects_invalid_utf8_offsets(offset):
    with pytest.raises(ValueError, match="offset"):
        text_chunk("éx", offset=offset)


@pytest.mark.parametrize("budget", [0, -1])
def test_text_byte_cursor_requires_a_positive_budget(budget):
    with pytest.raises(ValueError, match="positive"):
        text_chunk("text", budget=budget)


def test_text_byte_cursor_preserves_characters_across_chunks():
    first, cursor = text_chunk("é€final", budget=4)
    second, end = text_chunk("é€final", offset=cursor)
    assert first == "é"
    assert first + second == "é€final"
    assert end is None
    assert text_chunk("é€final", offset=len("é€final".encode())) == ("", None)


def test_attachment_reader_does_not_load_the_native_decoder():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import hailhq.voicebot.attachment_reader; "
                "assert 'hailhq.voicebot.attachment_decoder' not in sys.modules; "
                "assert 'pypdfium2' not in sys.modules"
            ),
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_pdf_soft_deadline_preserves_pages_and_a_continuation(monkeypatch):
    clock = iter([0, 5])
    monkeypatch.setattr(
        attachment_decoder, "time", SimpleNamespace(monotonic=lambda: next(clock))
    )
    data = {
        "base64": base64.b64encode(pdf_bytes(3)).decode(),
        "content_type": "application/pdf",
    }
    first = decode_file(data)
    assert first["pages"] == "1-1 of 3"
    assert first["partial"] is True
    assert first["continuation"] == {"page": 2, "text_offset": 0}
    monkeypatch.setattr(
        attachment_decoder, "time", SimpleNamespace(monotonic=lambda: 0)
    )
    rest = decode_file({**data, **first["continuation"]})
    assert "Receipt page 2" in rest["text"]
    assert "Receipt page 3" in rest["text"]
    assert "continuation" not in rest


def test_email_file_schema_does_not_cap_the_page_number():
    assert read_email.SPEC.parameters["properties"]["page"] == {
        "type": "integer",
        "minimum": 1,
    }


@pytest.mark.parametrize("remaining", [1, 2])
def test_pdf_preserves_prior_pages_when_budget_cannot_fit_next_character(
    monkeypatch, remaining
):
    # Keep the real PDF's stream lengths and byte offsets unchanged.
    raw = pdf_bytes(2).replace(b"Receipt page 2", b"\xe9" + b"x" * 13)
    data = {
        "base64": base64.b64encode(raw).decode(),
        "content_type": "application/pdf",
    }
    full = decode_file({**data, "page": 2})
    assert ord(full["text"][0]) > 127
    monkeypatch.setattr(
        attachment_decoder, "TEXT_BYTES", len("Receipt page 1".encode()) + 1 + remaining
    )
    first = decode_file(data)
    assert first["text"] == "Receipt page 1"
    assert len(first["images"]) == 1
    assert first["partial"] is True
    assert first["continuation"] == {"page": 2, "text_offset": 0}
    rest = decode_file({**data, **first["continuation"]})
    assert rest["text"] == full["text"]
    assert "continuation" not in rest
