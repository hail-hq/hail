"""The document check: values read from a file, mapped to the form, and the
warnings worked out from them. The model is a fake; every name is made up."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import sys
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
# Another file: a file given for two slots is read once.
_BILL = UploadedFile(filename="upload", content_type="image/png", data=b"\x89PNG-bill")


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
    assert 0 < call["timeout_s"] <= document_check.CHECK_TIMEOUT_S


async def test_the_proof_of_address_fills_the_address_first() -> None:
    req = _req(address_required=True)
    docs = {
        **_docs(),
        "proof_of_address": DocumentInput(option="utility_bill", file=_BILL),
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
    assert await _read(content_type="application/pdf", data=_pdf(1)) == {
        "readable": True
    }
    call = calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["thinking"] == {"type": "disabled"}
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": {}}
    assert call["messages"][0]["content"][0]["type"] == "document"
    assert 14.9 < call["timeout"] <= 15.0


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


# -- names in other scripts and spellings (all made up) ------------------------


def test_umlauts_may_be_written_out_or_dropped() -> None:
    printed = _printed(given="Jürgen", family="Müller")
    assert name_matches(printed, "Juergen", "Mueller")
    assert name_matches(printed, "Jurgen", "Muller")
    assert not name_matches(printed, "Juergen", "Miller")


def test_apostrophes_and_hyphens_join_or_split() -> None:
    printed = _printed(given="Siobhan", family="O'Brien")
    for typed in ("OBrien", "O Brien", "o'brien", "O-Brien"):
        assert name_matches(printed, "Siobhan", typed), typed


def test_a_greek_id_matches_its_latin_mrz() -> None:
    printed = VerificationPrintedName(
        given_names="ΕΛΕΝΗ",
        surnames="ΠΑΠΑΔΑΚΗ",
        latin_given_names="ELENI",
        latin_surnames="PAPADAKI",
        business_name="",
    )
    assert name_matches(printed, "Eleni", "Papadaki")
    assert not name_matches(printed, "Elena", "Papadaki")


def test_a_cyrillic_id_matches_its_latin_form() -> None:
    printed = VerificationPrintedName(
        given_names="ИВАН",
        surnames="ПЕТРОВСКИЙ",
        latin_given_names="IVAN",
        latin_surnames="PETROVSKIY",
        business_name="",
    )
    assert name_matches(printed, "Ivan", "Petrovskiy")
    assert name_matches(printed, "Иван", "Петровский")


async def test_the_message_shows_the_latin_form() -> None:
    answer = _answer(
        given_names="ΕΛΕΝΗ",
        surnames="ΠΑΠΑΔΑΚΗ",
        latin_given_names="ELENI",
        latin_surnames="PAPADAKI",
    )
    out = await _check(
        fields={"first_name": "Elena", "last_name": "Papadaki"},
        reader=_reader(answer),
    )
    assert "first name ΕΛΕΝΗ (ELENI)" in out.warnings[0].message


async def test_a_name_is_compared_with_its_own_slot_only() -> None:
    req = _req(fields=(FieldSpec(name="email", label="Email"),))
    other = DocumentSlot(
        name="director",
        label="Director",
        options=(
            DocumentOption(
                key="passport",
                label="Passport",
                fields=(
                    FieldSpec(name="first_name", label="First name"),
                    FieldSpec(name="last_name", label="Last name"),
                ),
            ),
        ),
    )
    req = req.model_copy(update={"documents": (*req.documents, other)})
    docs = {
        **_docs(),
        "director": DocumentInput(
            option="passport", fields={"first_name": "Other", "last_name": "Person"}
        ),
    }
    out = await _check(req=req, docs=docs)
    assert "name" not in _kinds(out)


async def test_the_chosen_type_decides_a_proof_of_address() -> None:
    req = _req()
    slot = req.documents[0].model_copy(
        update={
            "options": (
                *req.documents[0].options,
                DocumentOption(
                    key="utility_bill", label="Utility bill", needs_address=True
                ),
            )
        }
    )
    req = req.model_copy(update={"documents": (slot, *req.documents[1:])})
    typed = {"first_name": "Elena", "last_name": "Maria"}
    as_id = await _check(req=req, fields=typed)
    assert "name" in _kinds(as_id)
    bill = _answer(option="utility_bill", expiry_date="2024-01-15")
    as_bill = await _check(req=req, fields=typed, reader=_reader(bill))
    assert _kinds(as_bill) == ["document_type"]


# -- reader limits ---------------------------------------------------------------


def _jpeg(size) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, "JPEG")
    return out.getvalue()


def _pdf(pages: int) -> bytes:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(595, 842)
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    return buf.getvalue()


def _pages_sent(call: dict) -> int:
    import pypdfium2 as pdfium

    block = call["messages"][0]["content"][0]
    assert block["type"] == "document"
    with pdfium.PdfDocument(base64.b64decode(block["source"]["data"])) as pdf:
        return len(pdf)


async def test_reader_sends_only_the_first_pages_of_a_short_pdf(
    reader_settings, monkeypatch
) -> None:
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)
    sends: list[int] = []
    for pages in (1, 3, 5):
        assert await _read(content_type="application/pdf", data=_pdf(pages)) == {}
    assert [_pages_sent(c) for c in calls] == [1, 3, 3]
    # Over 5 pages: not read at all, and no read is used.
    out = await _read(
        content_type="application/pdf", data=_pdf(6), on_send=lambda: sends.append(1)
    )
    assert out is None and len(calls) == 3 and sends == []
    # Not a PDF at all: the same.
    assert await _read(content_type="application/pdf", data=b"%PDF-1.7") is None
    assert len(calls) == 3


async def test_reader_reports_a_send_even_when_the_call_fails(
    reader_settings, monkeypatch
) -> None:
    sends: list[int] = []
    client, _ = _fake_client(error=RuntimeError("boom"))
    monkeypatch.setattr(document_reader, "_client", client)
    assert await _read(on_send=lambda: sends.append(1)) is None
    assert sends == [1]
    # Stopped before the model (not decodable): no send.
    assert await _read(data=b"not an image", on_send=lambda: sends.append(1)) is None
    assert sends == [1]


async def test_reader_uses_the_document_model_setting(
    reader_settings, monkeypatch
) -> None:
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)
    monkeypatch.setattr(settings, "document_reader_model", "claude-sonnet-x")
    await _read(content_type="application/pdf", data=_pdf(1))
    assert calls[0]["model"] == "claude-sonnet-x"


# -- image decoding (in a child process) ------------------------------------


def test_decode_refuses_a_png_over_the_hard_bound(monkeypatch) -> None:
    from hailhq.core.providers.llm import document_image

    monkeypatch.setattr(document_image, "MAX_PIXELS", 40 * 30 - 1)
    with pytest.raises(ValueError):
        document_image.decode("image/png", _png((40, 30)))


def test_decode_drafts_a_large_jpeg_before_the_cap(monkeypatch) -> None:
    from hailhq.core.providers.llm import document_image

    # 12 million pixels in the header; drafted to 1/4 per side for a 500 px
    # long side, under 1 million. (Real sizes: a 200 million pixel photo
    # drafts to about 12 million for 1568 px.)
    monkeypatch.setattr(document_image, "LONG_SIDE", 500)
    monkeypatch.setattr(document_image, "MAX_PIXELS", 4_000_000)
    out = Image.open(
        io.BytesIO(document_image.decode("image/jpeg", _jpeg((4000, 3000))))
    )
    assert max(out.size) == 500
    with pytest.raises(ValueError):
        document_image.decode("image/png", _png((4000, 3000)))


def test_decode_puts_a_transparent_image_on_white() -> None:
    from hailhq.core.providers.llm import document_image

    src = io.BytesIO()
    Image.new("RGBA", (40, 30), (0, 0, 0, 0)).save(src, "PNG")
    out = Image.open(io.BytesIO(document_image.decode("image/png", src.getvalue())))
    assert out.convert("RGB").getpixel((5, 5))[0] > 240


async def test_reader_drops_a_decode_over_its_limit(
    reader_settings, monkeypatch
) -> None:
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)
    # The real child process, given no time: killed, and the read fails open.
    monkeypatch.setattr(document_reader, "_DECODE_LIMIT_S", 0.001)
    assert await _read(data=_png((400, 300))) is None
    assert calls == []


async def test_reader_gives_the_model_only_the_time_left(
    reader_settings, monkeypatch
) -> None:
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)

    async def decode(*_):
        await asyncio.sleep(0.3)
        return _jpeg((40, 30))

    monkeypatch.setattr(document_reader, "_run_decoder", decode)
    await _read(timeout_s=2.0)
    assert 1.5 < calls[0]["timeout"] < 1.75
    # Under a second left: the decode does not start.
    assert await _read(timeout_s=0.5) is None


async def test_reader_waits_for_a_busy_decoder_without_counting_the_wait(
    reader_settings, monkeypatch
) -> None:
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)
    monkeypatch.setattr(document_reader, "_DECODE_LIMIT_S", 0.5)
    started: list[float] = []

    async def decode(*_):
        started.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.4)
        return _jpeg((40, 30))

    monkeypatch.setattr(document_reader, "_run_decoder", decode)
    begin = asyncio.get_running_loop().time()
    results = await asyncio.gather(*(_read() for _ in range(5)))
    # Four decode at once; the fifth waits 0.4 s for its turn, then decodes
    # in 0.4 s: 0.8 s in all, over the 0.5 s limit, and still read.
    assert results == [{}] * 5
    assert started[4] - begin >= 0.35
    assert len(calls) == 5


async def test_reader_decodes_a_real_image_in_a_child_process(
    reader_settings, monkeypatch
) -> None:
    """The real child process: it decodes with no credentials in its
    environment and gives back a JPEG."""
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "never-seen-by-the-child")
    assert await _read(data=_png((400, 300))) == {}
    block = calls[0]["messages"][0]["content"][0]
    assert block["source"]["media_type"] == "image/jpeg"


# -- review round 3 --------------------------------------------------------------


async def test_a_part_printed_alone_must_be_whole_words() -> None:
    req = _req(fields=(FieldSpec(name="x1", label="Email"),), address_required=True)
    smith = _answer(given_names="", surnames="SMITH")
    out = await _check(
        req=req, address={"customer_name": "John Goldsmith"}, reader=_reader(smith)
    )
    assert _kinds(out) == ["name"]
    ann = _answer(given_names="ANN", surnames="")
    out = await _check(
        req=req, address={"customer_name": "Joanne"}, reader=_reader(ann)
    )
    assert _kinds(out) == ["name"]
    obrien = _answer(given_names="", surnames="O'BRIEN")
    ok = await _check(
        req=req, address={"customer_name": "Siobhan OBrien"}, reader=_reader(obrien)
    )
    assert ok.warnings == []


async def test_a_skipped_slot_is_read_false() -> None:
    out = await check_documents(
        _req(),
        {},
        {},
        {"proof_of_identity": DocumentInput(option="id_card")},
        today=TODAY,
        reader=_reader(),
        skipped=["proof_of_identity"],
    )
    assert out.documents["proof_of_identity"].read is False
    assert out.warnings == []


async def test_a_cancelled_check_stops_its_reads() -> None:
    stopped = asyncio.Event()

    async def slow(**kwargs):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            stopped.set()
            raise

    task = asyncio.ensure_future(_check(reader=slow))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_reads_over_the_time_limit_are_logged_as_counts(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(document_check, "CHECK_TIMEOUT_S", 0.05)

    async def slow(**kwargs):
        await asyncio.sleep(5)

    out = await _check(reader=slow)
    assert out.documents["proof_of_identity"].read is False
    assert "1 of 1 reads over the time limit" in caplog.text


async def test_isolated_process_is_killed_at_its_limit() -> None:
    from hailhq.core.isolated_process import run_isolated

    with pytest.raises(TimeoutError):
        await run_isolated(
            "hailhq.core.providers.llm.document_image",
            "image/png",
            stdin=_png((400, 300)),
            timeout_s=0.001,
            max_output=1024,
        )
    with pytest.raises(ValueError):
        await run_isolated(
            "hailhq.core.providers.llm.document_image",
            "image/jpeg",
            stdin=b"not an image",
            timeout_s=10,
            max_output=1024,
        )


def test_decode_scales_a_16_bit_grey_image() -> None:
    from hailhq.core.providers.llm import document_image

    src = io.BytesIO()
    Image.new("I;16", (40, 30), 30000).save(src, "PNG")
    out = Image.open(io.BytesIO(document_image.decode("image/png", src.getvalue())))
    # Scaled to 8 bits (about 255 for the brightest pixel), not clipped from
    # a darker grey: here every pixel is the brightest, so white.
    gradient = io.BytesIO()
    img = Image.new("I;16", (2, 1))
    img.putpixel((0, 0), 1000)
    img.putpixel((1, 0), 30000)
    img.save(gradient, "PNG")
    g = Image.open(io.BytesIO(document_image.decode("image/png", gradient.getvalue())))
    left, right = g.convert("L").getpixel((0, 0)), g.convert("L").getpixel((1, 0))
    assert left < 40 and right > 200
    assert out.size == (40, 30)


async def test_a_last_name_box_alone_is_compared() -> None:
    req = _req(fields=(FieldSpec(name="last_name", label="Last Name"),))
    out = await _check(req=req, fields={"last_name": "Other"})
    assert [(w.kind, w.field) for w in out.warnings] == [("name", "last_name")]


async def test_a_part_with_no_letters_is_not_taken_for_the_name() -> None:
    req = _req(fields=(FieldSpec(name="x1", label="Email"),), address_required=True)
    answer = _answer(given_names="-", surnames="SMITH")
    out = await _check(
        req=req, address={"customer_name": "John Smith"}, reader=_reader(answer)
    )
    assert out.warnings == []


# -- review round 4 ----------------------------------------------------------------


def test_decode_scales_down_a_600_dpi_a4_scan() -> None:
    from hailhq.core.providers.llm import document_image

    # 4960 x 7016 pixels: 35 million, over the old 25 million cap.
    out = Image.open(io.BytesIO(document_image.decode("image/png", _png((4960, 7016)))))
    assert max(out.size) == document_image.LONG_SIDE


async def test_company_legal_forms_with_dots_match() -> None:
    req = _req(
        subject_type="business",
        fields=(FieldSpec(name="business_name", label="Business Name"),),
    )
    answer = _answer(business_name="ACME S.A.", given_names="", surnames="")
    same = await _check(
        req=req, fields={"business_name": "Acme SA"}, reader=_reader(answer)
    )
    assert same.warnings == []
    kg = _answer(business_name="MUSTER GMBH & CO. KG", given_names="", surnames="")
    plain = await _check(
        req=req, fields={"business_name": "Muster"}, reader=_reader(kg)
    )
    assert plain.warnings == []


async def test_one_file_for_two_slots_is_read_once() -> None:
    req = _req(address_required=True)
    docs = {
        **_docs(),
        "proof_of_address": DocumentInput(option="utility_bill", file=_PNG),
    }
    reader = _reader(
        _answer(
            values=[
                {"key": "proof_of_identity.document_number", "value": "XK1"},
                {"key": "address.city", "value": "Sampleton"},
            ]
        )
    )
    sends: list[str] = []
    out = await check_documents(
        req, {}, {}, docs, today=TODAY, reader=reader, on_send=sends.append
    )
    assert len(reader.calls) == 1
    call = reader.calls[0]
    assert "It is also given as: Proof of address." in call["prompt"]
    assert set(call["schema"]["properties"]["option"]["enum"]) == {
        "passport",
        "id_card",
        "utility_bill",
        "none",
    }
    assert out.documents["proof_of_identity"].fields == {"document_number": "XK1"}
    assert out.documents["proof_of_address"].read is True
    assert out.address == {"city": "Sampleton"}


async def test_reader_marks_the_send_only_once_the_request_can_go(
    reader_settings, monkeypatch
) -> None:
    sends: list[int] = []

    def broken_client():
        raise RuntimeError("no client")

    monkeypatch.setattr(document_reader, "_get_client", broken_client)
    assert await _read(on_send=lambda: sends.append(1)) is None
    assert sends == []


# -- review round 5 --------------------------------------------------------------


def test_decode_scales_down_before_any_full_size_copy(monkeypatch) -> None:
    """A 100 million pixel RGBA scan is 400 MB once decoded; any second
    full-size copy (the EXIF turn, the white background) would pass the
    child's 1 GB limit. So the image is scaled down first."""
    from hailhq.core.providers.llm import document_image

    seen: list[tuple[int, int]] = []
    real = document_image.ImageOps.exif_transpose

    def watch(image, **kw):
        seen.append(image.size)
        return real(image, **kw)

    monkeypatch.setattr(document_image.ImageOps, "exif_transpose", watch)
    src = io.BytesIO()
    Image.new("RGBA", (4000, 3000), (0, 0, 0, 0)).save(src, "PNG")
    document_image.decode("image/png", src.getvalue())
    assert max(seen[0]) <= document_image.LONG_SIDE
    # The size math: the most RGBA pixels decoded, then only small copies.
    assert document_image.MAX_PIXELS * 4 < document_image.MEMORY_LIMIT_BYTES // 2


def test_a_decomposed_umlaut_reads_as_one() -> None:
    import unicodedata as ud

    printed = _printed(given="Jürgen", family="Müller")
    decomposed = ud.normalize("NFD", "Mueller")
    assert name_matches(printed, "Juergen", decomposed)
    assert name_matches(
        _printed(given="Jurgen", family=ud.normalize("NFD", "Müller")),
        "Jurgen",
        "Mueller",
    )


async def test_the_callers_groups_name_the_reads() -> None:
    docs = {
        **_docs(),
        "proof_of_address": DocumentInput(option="utility_bill", file=_BILL),
    }
    sends: list[str] = []
    reader = _reader(_answer())
    await check_documents(
        _req(),
        {},
        {},
        docs,
        today=TODAY,
        reader=reader,
        on_send=sends.append,
        groups=[["proof_of_identity", "proof_of_address"]],
    )
    assert len(reader.calls) == 1
    reader.calls[0]["on_send"]()
    assert sends == ["proof_of_identity"]


async def test_under_a_second_left_the_file_is_not_sent(
    reader_settings, monkeypatch
) -> None:
    client, calls = _fake_client(_response("{}"))
    monkeypatch.setattr(document_reader, "_client", client)
    sends: list[int] = []
    out = await _read(
        content_type="application/pdf", timeout_s=0.5, on_send=lambda: sends.append(1)
    )
    assert out is None and calls == [] and sends == []


async def test_one_file_in_two_slots_gives_one_name_warning() -> None:
    base = _req()
    # Its only type is not the one the file shows: its label is "document".
    second = base.documents[0].model_copy(
        update={
            "name": "proof_of_birth",
            "label": "Proof of birth",
            "options": base.documents[0].options[:1],
        }
    )
    req = base.model_copy(update={"documents": (base.documents[0], second)})
    docs = {
        **_docs(option="passport"),
        "proof_of_birth": DocumentInput(option="", file=_PNG),
    }
    out = await check_documents(
        req,
        {"first_name": "Someone", "last_name": "Else"},
        {},
        docs,
        today=TODAY,
        reader=_reader(_answer()),
    )
    names = [w for w in out.warnings if w.field == "first_name"]
    assert len(names) == 1


def test_decode_scales_a_palette_image_with_a_real_filter(monkeypatch) -> None:
    """Pillow scales palette and 1-bit images with NEAREST; they are
    converted before the scale-down."""
    from hailhq.core.providers.llm import document_image

    modes: list[str] = []
    real = document_image.ImageOps.exif_transpose

    def watch(image, **kw):
        modes.append(image.mode)
        return real(image, **kw)

    monkeypatch.setattr(document_image.ImageOps, "exif_transpose", watch)
    for mode in ("P", "1"):
        src = io.BytesIO()
        Image.new(mode, (3200, 2400)).save(src, "PNG")
        document_image.decode("image/png", src.getvalue())
    assert modes == ["RGBA", "L"]


def test_memory_limits_fit_the_server() -> None:
    """One decode at a time per API process, in a 512 MB child: the server
    has little memory and no swap, and live calls run beside it."""
    from hailhq.core.providers.llm import document_image

    assert document_reader._DECODES == 1
    assert document_image.MEMORY_LIMIT_BYTES == 512 * 1024 * 1024
    # A palette image converted to RGBA: the source and the copy.
    worst = document_image.MAX_PIXELS * (1 + 4)
    assert worst < document_image.MEMORY_LIMIT_BYTES // 2


def test_a_png_or_progressive_jpeg_over_the_pixel_cap_is_refused(
    monkeypatch,
) -> None:
    from hailhq.core.providers.llm import document_image

    monkeypatch.setattr(document_image, "MAX_PIXELS", 40_000)
    monkeypatch.setattr(document_image, "LONG_SIDE", 100)
    png = io.BytesIO()
    Image.new("L", (400, 300)).save(png, "PNG")
    with pytest.raises(ValueError):
        document_image.decode("image/png", png.getvalue())
    progressive = io.BytesIO()
    Image.new("RGB", (400, 300)).save(progressive, "JPEG", progressive=True)
    with pytest.raises(ValueError):
        document_image.decode("image/jpeg", progressive.getvalue())
    # A baseline JPEG is decoded at a reduced scale: its header may be larger.
    baseline = io.BytesIO()
    Image.new("RGB", (800, 600)).save(baseline, "JPEG")
    assert document_image.decode("image/jpeg", baseline.getvalue())


@pytest.mark.skipif(sys.platform != "linux", reason="the memory limit is Linux only")
async def test_the_largest_images_decode_within_the_child_memory_limit() -> None:
    from hailhq.core.isolated_process import run_isolated
    from hailhq.core.providers.llm import document_image

    side = int(document_image.MAX_PIXELS**0.5)
    for mode in ("RGBA", "P"):
        src = io.BytesIO()
        Image.new(mode, (side, side)).save(src, "PNG")
        out = await run_isolated(
            "hailhq.core.providers.llm.document_image",
            "image/png",
            stdin=src.getvalue(),
            timeout_s=20,
            max_output=8 * 1024 * 1024,
        )
        assert out[:2] == b"\xff\xd8", mode
