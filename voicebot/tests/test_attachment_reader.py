"""The worker inspects real file bytes, never exposes them as a tool result."""

import base64
import io
import json
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.voicebot.attachment_decoder import decode_file
from hailhq.voicebot.attachment_reader import inspect_tool_file
from hailhq.voicebot.tools import _make_handler
from PIL import Image


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (80, 60), "red").save(buffer, "PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_visual_tool_result_contains_facts_not_binary_or_private_paths(
    monkeypatch,
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
    result = await _make_handler(spec, ctx)(
        {}, SimpleNamespace(session=SimpleNamespace(llm=model))
    )
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


def test_pdf_reading_includes_rendered_scans_and_a_bounded_page_range():
    result = decode_file(
        {
            "base64": base64.b64encode(pdf_bytes(5)).decode(),
            "content_type": "application/pdf",
            "page": 2,
        }
    )
    assert "Receipt page 2" in result["text"]
    assert (
        len(result["images"]) == 3
        and result["pages"] == "2-4 of 5"
        and result["partial"]
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
