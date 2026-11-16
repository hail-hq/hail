"""The document check: values read from a file, mapped to the form, and the
warnings worked out from them. The model is a fake; every name is made up."""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import date
from types import SimpleNamespace

import pytest
from hailhq.core.config import settings
from hailhq.core.providers.llm import document_reader
from hailhq.core.providers.verification import document_check
from hailhq.core.providers.verification.base import (
    DocumentInput,
    DocumentOption,
    DocumentSlot,
    FieldSpec,
    Requirements,
    UploadedFile,
)
from hailhq.core.providers.verification.document_check import (
    check_documents,
    name_matches,
)
from hailhq.core.schemas import VerificationPrintedName
from PIL import Image

TODAY = date(2026, 11, 16)
_PNG = UploadedFile(filename="upload", content_type="image/png", data=b"\x89PNG")


def _req(subject_type="person", address_required=False, fields=None) -> Requirements:
    return Requirements(
        provider="fake",
        country_code="EE",
        number_type="mobile",
        subject_type=subject_type,
        required=True,
        fields=fields
        or (
            FieldSpec(name="first_name", label="First Name"),
            FieldSpec(name="last_name", label="Last Name"),
            FieldSpec(
                name="birth_date", label="Birth date", pattern=r"^\d{4}-\d{2}-\d{2}$"
            ),
        ),
        documents=(
            DocumentSlot(
                name="proof_of_identity",
                label="Proof of identity",
                options=(
                    DocumentOption(
                        key="passport",
                        label="Passport",
                        fields=(
                            FieldSpec(name="document_number", label="Document number"),
                        ),
                    ),
                    DocumentOption(
                        key="id_card",
                        label="ID card",
                        fields=(
                            FieldSpec(name="document_number", label="Document number"),
                        ),
                    ),
                ),
            ),
            DocumentSlot(
                name="proof_of_address",
                label="Proof of address",
                options=(
                    DocumentOption(
                        key="utility_bill", label="Utility bill", needs_address=True
                    ),
                ),
            ),
        ),
        address_required=address_required,
    )


def _answer(**over) -> dict:
    answer = {
        "readable": True,
        "orientation": "upright",
        "kind": "national ID card",
        "option": "id_card",
        "given_names": "MARIA ELENA",
        "surnames": "TESTER SAMPLE",
        "business_name": "",
        "expiry_date": "2031-06-01",
        "values": [
            {"key": "first_name", "value": "MARIA ELENA"},
            {"key": "last_name", "value": "TESTER SAMPLE"},
            {"key": "birth_date", "value": "1990-03-12"},
            {"key": "proof_of_identity.document_number", "value": "XK0000000"},
        ],
    }
    answer.update(over)
    return answer


def _reader(*answers):
    """A fake model: the answers in order, and the calls it got."""
    calls: list[dict] = []
    queue = list(answers)

    async def read(**kwargs):
        calls.append(kwargs)
        return queue.pop(0) if queue else None

    read.calls = calls  # type: ignore[attr-defined]
    return read


def _docs(option="id_card", **fields) -> dict[str, DocumentInput]:
    return {"proof_of_identity": DocumentInput(option=option, fields=fields, file=_PNG)}


async def _check(req=None, fields=None, address=None, docs=None, reader=None):
    return await check_documents(
        req or _req(),
        fields or {},
        address or {},
        docs if docs is not None else _docs(),
        today=TODAY,
        reader=reader or _reader(_answer()),
    )


# -- names -----------------------------------------------------------------


def _printed(given="JOSÉ-MARÍA", family="EXAMPLE TESTER") -> VerificationPrintedName:
    return VerificationPrintedName(given_names=given, surnames=family, business_name="")


def test_order_accents_case_and_hyphens_match() -> None:
    assert name_matches(_printed(), "maria jose", "Tester Example")


def test_a_missing_part_does_not_match() -> None:
    assert not name_matches(_printed(), "Maria", "Example Tester")


def test_swapped_parts_do_not_match() -> None:
    assert not name_matches(_printed(), "Example Tester", "Jose Maria")


def test_an_empty_box_is_not_compared() -> None:
    assert name_matches(_printed(), "", "Tester Example")
    assert not name_matches(_printed(), "", "Tester")


# -- values ------------------------------------------------------------------


async def test_values_are_mapped_to_the_form_and_the_slot() -> None:
    out = await _check()
    assert out.fields == {
        "first_name": "MARIA ELENA",
        "last_name": "TESTER SAMPLE",
        "birth_date": "1990-03-12",
    }
    doc = out.documents["proof_of_identity"]
    assert doc.read and doc.option == "id_card"
    assert doc.fields == {"document_number": "XK0000000"}
    assert doc.expiry_date == "2031-06-01"
    assert out.warnings == []


async def test_a_value_the_field_would_refuse_is_dropped() -> None:
    answer = _answer(
        values=[
            {"key": "birth_date", "value": "12.03.1990"},
            {"key": "nope", "value": "x"},
        ]
    )
    out = await _check(reader=_reader(answer))
    assert out.fields == {}


async def test_the_prompt_lists_every_type_of_the_slot_and_the_address() -> None:
    reader = _reader(_answer())
    await _check(req=_req(address_required=True), reader=reader)
    call = reader.calls[0]
    keys = call["schema"]["properties"]["values"]["items"]["properties"]["key"]["enum"]
    assert "proof_of_identity.document_number" in keys
    assert "address.street" in keys
    assert call["schema"]["properties"]["option"]["enum"] == [
        "passport",
        "id_card",
        "none",
    ]
    assert call["timeout_s"] == document_check.CHECK_TIMEOUT_S


async def test_the_proof_of_address_fills_the_address_first() -> None:
    req = _req(address_required=True)
    docs = {
        **_docs(),
        "proof_of_address": DocumentInput(option="utility_bill", file=_PNG),
    }
    id_answer = _answer(values=[{"key": "address.city", "value": "Old Town"}])
    bill = _answer(
        option="utility_bill",
        expiry_date="",
        values=[{"key": "address.city", "value": "New Town"}],
    )
    out = await _check(req=req, docs=docs, reader=_reader(id_answer, bill))
    assert out.address == {"city": "New Town"}


# -- warnings ----------------------------------------------------------------


def _kinds(out) -> list[str]:
    return [w.kind for w in out.warnings]


async def test_a_typed_name_that_differs_is_a_name_warning() -> None:
    out = await _check(fields={"first_name": "Elena", "last_name": "Maria"})
    assert _kinds(out) == ["name"]
    w = out.warnings[0]
    assert w.field == "first_name"
    assert w.message == (
        "Your ID card shows first name MARIA ELENA and last name TESTER SAMPLE. "
        "You typed Elena / Maria. Type them exactly as printed."
    )


async def test_empty_name_boxes_are_not_compared() -> None:
    out = await _check(fields={})
    assert out.warnings == []


async def test_a_name_on_the_document_fields_is_compared_too() -> None:
    req = _req(fields=(FieldSpec(name="email", label="Email"),))
    slot = req.documents[0]
    option = slot.options[1].model_copy(
        update={
            "fields": (
                FieldSpec(name="first_name", label="First name"),
                FieldSpec(name="last_name", label="Last name"),
            )
        }
    )
    req = req.model_copy(
        update={
            "documents": (
                slot.model_copy(update={"options": (option,)}),
                *req.documents[1:],
            )
        }
    )
    out = await _check(req=req, docs=_docs(first_name="Maria", last_name="Tester"))
    assert [(w.kind, w.field) for w in out.warnings] == [
        ("name", "proof_of_identity.first_name")
    ]


async def test_one_name_box_is_checked_against_the_full_name() -> None:
    req = _req(fields=(FieldSpec(name="x1", label="Email"),), address_required=True)
    out = await _check(req=req, address={"customer_name": "Maria Tester"})
    assert [(w.kind, w.field) for w in out.warnings] == [
        ("name", "address.customer_name")
    ]


async def test_company_names_ignore_the_legal_form() -> None:
    req = _req(
        subject_type="business",
        fields=(FieldSpec(name="business_name", label="Business Name"),),
    )
    answer = _answer(
        business_name="EXAMPLE TRADING LIMITED", given_names="", surnames=""
    )
    same = await _check(
        req=req, fields={"business_name": "Example Trading Ltd"}, reader=_reader(answer)
    )
    assert same.warnings == []
    other = await _check(
        req=req, fields={"business_name": "Sample Trading"}, reader=_reader(answer)
    )
    assert _kinds(other) == ["name"]


async def test_an_expired_document_is_flagged() -> None:
    out = await _check(reader=_reader(_answer(expiry_date="2024-01-15")))
    assert _kinds(out) == ["expired"]
    assert out.warnings[0].field == "proof_of_identity"
    assert "expired on 15 January 2024." in out.warnings[0].message


async def test_an_upside_down_document_is_flagged() -> None:
    out = await _check(reader=_reader(_answer(orientation="upside_down")))
    assert _kinds(out) == ["rotated"]
    assert "upside down" in out.warnings[0].message


async def test_another_document_type_is_flagged_with_the_fix() -> None:
    out = await _check(docs=_docs(option="passport"))
    assert _kinds(out) == ["document_type"]
    assert out.warnings[0].message == (
        "You chose Passport as the document type, but this looks like another "
        "document: ID card. Change the document type to ID card."
    )


async def test_no_type_chosen_yet_gives_no_type_warning() -> None:
    out = await _check(docs=_docs(option=""))
    assert out.warnings == []
    assert out.documents["proof_of_identity"].option == "id_card"


async def test_an_unreadable_file_gives_one_warning_and_no_name_check() -> None:
    out = await _check(
        fields={"first_name": "Other", "last_name": "Person"},
        reader=_reader(_answer(readable=False)),
    )
    assert _kinds(out) == ["unreadable"]
    assert out.documents["proof_of_identity"].printed_name is None
    # What it guessed from an unreadable file fills nothing.
    assert out.fields == {} and out.documents["proof_of_identity"].fields == {}


async def test_only_the_typed_part_of_a_name_is_compared() -> None:
    out = await _check(fields={"first_name": "", "last_name": "TESTER SAMPLE"})
    assert out.warnings == []
    out = await _check(fields={"first_name": "", "last_name": "Sample"})
    assert [(w.kind, w.field) for w in out.warnings] == [("name", "last_name")]


async def test_a_proof_of_address_is_not_name_checked() -> None:
    docs = {"proof_of_address": DocumentInput(option="utility_bill", file=_PNG)}
    bill = _answer(option="utility_bill", given_names="M", surnames="TESTER")
    out = await _check(
        fields={"first_name": "Maria Elena", "last_name": "Tester Sample"},
        docs=docs,
        reader=_reader(bill),
    )
    assert "name" not in _kinds(out)


# -- fail open ---------------------------------------------------------------


async def test_no_answer_means_no_values_and_no_warnings() -> None:
    out = await _check(reader=_reader(None))
    assert out.fields == {} and out.warnings == []
    assert out.documents["proof_of_identity"].read is False


async def test_a_slow_read_is_dropped(monkeypatch) -> None:
    monkeypatch.setattr(document_check, "CHECK_TIMEOUT_S", 0.05)

    async def slow(**kwargs):
        await asyncio.sleep(5)
        return _answer()

    out = await _check(reader=slow)
    assert out.documents["proof_of_identity"].read is False
    assert out.warnings == []


async def test_a_reader_that_raises_is_skipped() -> None:
    async def broken(**kwargs):
        raise RuntimeError("boom")

    out = await _check(reader=broken)
    assert out.documents["proof_of_identity"].read is False


async def test_nothing_personal_is_logged(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    await _check(fields={"first_name": "Elena", "last_name": "Maria"})
    text = caplog.text
    for secret in ("MARIA", "Elena", "TESTER", "XK0000000"):
        assert secret not in text


async def test_a_slot_without_a_file_is_not_read() -> None:
    reader = _reader(_answer())
    out = await _check(
        docs={"proof_of_identity": DocumentInput(option="id_card")}, reader=reader
    )
    assert reader.calls == [] and out.documents == {}


# -- the reader --------------------------------------------------------------


@pytest.fixture()
def reader_settings(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_backup_model", "claude-haiku-5-5")
    monkeypatch.setattr(settings, "anthropic_bedrock_region", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(document_reader, "_client", None)


def _fake_client(response=None, error: Exception | None = None):
    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        if error:
            raise error
        return response

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return client, calls


def _response(text: str, stop: str = "end_turn"):
    return SimpleNamespace(
        stop_reason=stop, content=[SimpleNamespace(type="text", text=text)]
    )


def _png(size=(4000, 3000)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, "PNG")
    return out.getvalue()


async def _read(**over):
    kwargs = {
        "content_type": "image/png",
        "data": _png((40, 30)),
        "prompt": "p",
        "schema": {},
        "timeout_s": 15.0,
    }
    kwargs.update(over)
    return await document_reader.read_document(**kwargs)


async def test_reader_sends_the_file_and_the_schema(
    reader_settings, monkeypatch
) -> None:
    client, calls = _fake_client(_response('{"readable": true}'))
    monkeypatch.setattr(document_reader, "_client", client)
    assert await _read(content_type="application/pdf") == {"readable": True}
    call = calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["thinking"] == {"type": "disabled"}
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": {}}
    assert call["messages"][0]["content"][0]["type"] == "document"
    assert call["timeout"] == 15.0


async def test_reader_sends_an_image_as_a_smaller_jpeg(
    reader_settings, monkeypatch
) -> None:
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)
    await _read(data=_png())
    block = calls[0]["messages"][0]["content"][0]
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/jpeg"
    sent = Image.open(
        io.BytesIO(__import__("base64").b64decode(block["source"]["data"]))
    )
    assert max(sent.size) == 1568


async def test_reader_skips_a_file_that_is_not_an_image(
    reader_settings, monkeypatch
) -> None:
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)
    assert await _read(data=b"not an image") is None
    assert calls == []


async def test_reader_skips_a_file_of_another_format_than_declared(
    reader_settings, monkeypatch, caplog
) -> None:
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)
    out = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(out, "GIF")
    # A GIF sent as a PNG, and a PNG sent as a JPEG: neither is decoded.
    assert await _read(content_type="image/png", data=out.getvalue()) is None
    assert await _read(content_type="image/jpeg", data=_png((40, 30))) is None
    assert calls == []
    assert "document read failed" in caplog.text


async def test_reader_skips_an_image_over_the_pixel_cap(
    reader_settings, monkeypatch
) -> None:
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)
    monkeypatch.setattr(document_reader, "_MAX_PIXELS", 40 * 30 - 1)
    assert await _read(data=_png((40, 30))) is None
    assert calls == []


async def test_reader_decodes_a_large_jpeg(reader_settings, monkeypatch) -> None:
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)
    out = io.BytesIO()
    Image.new("RGB", (4000, 3000), "white").save(out, "JPEG")
    assert await _read(content_type="image/jpeg", data=out.getvalue()) == {}
    block = calls[0]["messages"][0]["content"][0]
    sent = Image.open(
        io.BytesIO(__import__("base64").b64decode(block["source"]["data"]))
    )
    assert max(sent.size) == 1568


async def test_reader_is_off_without_a_model(reader_settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "anthropic_backup_model", "")
    assert await _read() is None


async def test_reader_skips_other_file_types(reader_settings) -> None:
    assert await _read(content_type="image/gif") is None


async def test_reader_fails_open_and_logs_only_the_type(
    reader_settings, monkeypatch, caplog
) -> None:
    client, _ = _fake_client(error=RuntimeError("secret body"))
    monkeypatch.setattr(document_reader, "_client", client)
    assert await _read() is None
    assert "RuntimeError" in caplog.text and "secret body" not in caplog.text


async def test_reader_drops_a_refusal_or_bad_json(reader_settings, monkeypatch) -> None:
    client, _ = _fake_client(_response("", stop="refusal"))
    monkeypatch.setattr(document_reader, "_client", client)
    assert await _read() is None
    client, _ = _fake_client(_response("not json"))
    monkeypatch.setattr(document_reader, "_client", client)
    assert await _read() is None


async def test_an_unreadable_file_is_not_read() -> None:
    out = await _check(reader=_reader(_answer(readable=False)))
    assert out.documents["proof_of_identity"].read is False


async def test_a_proof_of_address_has_no_expiry_warning() -> None:
    docs = {"proof_of_address": DocumentInput(option="utility_bill", file=_PNG)}
    bill = _answer(option="utility_bill", expiry_date="2024-01-15")
    out = await _check(docs=docs, reader=_reader(bill))
    assert "expired" not in _kinds(out)


async def test_one_typed_part_is_named_alone_in_the_message() -> None:
    out = await _check(fields={"first_name": "Elena", "last_name": ""})
    assert "You typed first name Elena." in out.warnings[0].message


async def test_a_full_name_holds_a_part_printed_alone() -> None:
    req = _req(fields=(FieldSpec(name="x1", label="Email"),), address_required=True)
    answer = _answer(given_names="")
    ok = await _check(
        req=req,
        address={"customer_name": "Maria Tester Sample"},
        reader=_reader(answer),
    )
    assert ok.warnings == []


async def test_reader_puts_a_transparent_image_on_white(
    reader_settings, monkeypatch
) -> None:
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)
    out = io.BytesIO()
    Image.new("RGBA", (40, 30), (0, 0, 0, 0)).save(out, "PNG")
    await _read(data=out.getvalue())
    block = calls[0]["messages"][0]["content"][0]
    sent = Image.open(
        io.BytesIO(__import__("base64").b64decode(block["source"]["data"]))
    )
    assert sent.convert("RGB").getpixel((5, 5))[0] > 240
