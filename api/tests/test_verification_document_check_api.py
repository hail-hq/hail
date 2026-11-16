# ruff: noqa: F811  (fixtures imported from test_verifications_api)
"""``POST /verifications/document-check``: read the files, return values and
warnings, file and save nothing. The model is a fake; names are made up."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.api import verification_files
from hailhq.api.deps import get_s3_mail_optional
from hailhq.api.main import app
from hailhq.api.routes import verifications as routes
from hailhq.api.verification_files import (
    MAX_PARALLEL_FETCHES,
    CollectedFiles,
    fetch_files,
)
from hailhq.core import document_reads
from hailhq.core.config import settings
from hailhq.core.document_reads import document_reads_today
from hailhq.core.models import AgentSendLog, CarrierVerification
from hailhq.core.providers.llm import document_reader
from hailhq.core.providers.llm.document_reader import (
    read_document as _real_read_document,
)
from hailhq.core.providers.verification import (
    DocumentOption,
    DocumentSlot,
    FieldSpec,
    Requirements,
    document_check,
)
from PIL import Image
from sqlalchemy import func, select

from . import test_verifications_api as base
from .test_verifications_api import (  # noqa: F401  (fixtures)
    _auth,
    _passport,
    carrier,
    pinned_catalog,
)
from .test_verifications_uploads import _JPEG, _upload

_ANSWER = {
    "readable": True,
    "orientation": "upright",
    "kind": "passport",
    "option": "passport",
    "given_names": "MARIA ELENA",
    "surnames": "TESTER",
    "business_name": "",
    "expiry_date": "2024-01-15",
    "values": [
        {"key": "first_name", "value": "MARIA ELENA"},
        {"key": "last_name", "value": "TESTER"},
    ],
}


def _fake_reader(monkeypatch, answer=_ANSWER):

    monkeypatch.setattr(routes, "document_reader_configured", lambda: True)
    calls: list[dict] = []

    async def read(**kwargs):
        calls.append(kwargs)
        # As the real reader: the read is used once the file is sent.
        if answer is not None and kwargs.get("on_send"):
            kwargs["on_send"]()
        return answer

    monkeypatch.setattr(document_check, "read_document", read)
    return calls


def _form(**over) -> dict:
    data = {
        "quote_id": base._QUOTE_ID or "",
        "country_code": "GB",
        "number_type": "mobile",
        "subject_type": "person",
        "fields": json.dumps({"first_name": "Elena", "last_name": "Tester"}),
        "documents": json.dumps({"proof_of_identity": {"option": "passport"}}),
    }
    data.update(over)
    return data


async def _post(client, key, files=None, **over):
    return await client.post(
        "/verifications/document-check",
        data=_form(**over),
        files=_passport() if files is None else files,
        headers=_auth(key),
    )


async def test_returns_values_and_warnings_and_files_nothing(
    client, org_and_key, carrier, monkeypatch, async_session, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    calls = _fake_reader(monkeypatch)
    _, _, key = org_and_key
    resp = await _post(client, key)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fields"] == {"first_name": "MARIA ELENA", "last_name": "TESTER"}
    assert body["documents"]["proof_of_identity"]["read"] is True
    assert sorted(w["kind"] for w in body["warnings"]) == ["expired", "name"]
    assert calls[0]["content_type"] == "image/jpeg"
    # Nothing filed or saved.
    assert carrier.drafts == []
    count = await async_session.scalar(select(func.count(CarrierVerification.id)))
    assert count == 0
    for secret in ("MARIA", "Elena", "TESTER"):
        assert secret not in caplog.text


async def test_the_type_may_be_left_out_and_names_may_be_empty(
    client, org_and_key, carrier, monkeypatch
) -> None:
    _fake_reader(monkeypatch, {**_ANSWER, "expiry_date": "2031-01-01"})
    _, _, key = org_and_key
    resp = await _post(
        client,
        key,
        fields="",
        documents=json.dumps({"proof_of_identity": {}}),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["warnings"] == []
    assert body["documents"]["proof_of_identity"]["option"] == "passport"


async def test_a_failed_read_is_200_with_nothing_read(
    client, org_and_key, carrier, monkeypatch
) -> None:
    _fake_reader(monkeypatch, None)
    _, _, key = org_and_key
    resp = await _post(client, key)
    assert resp.status_code == 200
    body = resp.json()
    assert body["warnings"] == [] and body["fields"] == {}
    assert body["documents"]["proof_of_identity"]["read"] is False


async def test_quote_and_slots_are_checked(client, org_and_key, carrier) -> None:
    _, _, key = org_and_key
    assert (await _post(client, key, quote_id="")).status_code == 422
    unknown = await _post(
        client,
        key,
        documents=json.dumps({"nope": {}}),
        files={"file.nope": ("a.jpg", b"\xff\xd8jpeg", "image/jpeg")},
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"][0]["loc"] == ["body", "documents", "nope"]
    # A file part for a slot not in documents is not read, as submit does,
    # and says so.
    stray = await _post(client, key, documents="{}")
    assert stray.status_code == 200
    assert stray.json()["documents"]["proof_of_identity"]["read"] is False


async def test_requires_auth(client) -> None:
    resp = await client.post("/verifications/document-check", data={})
    assert resp.status_code == 401


async def test_an_upload_id_is_read_after_the_transaction_ends(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:

    calls = _fake_reader(monkeypatch)
    store = AsyncMock()
    store.fetch_raw.return_value = _JPEG
    app.dependency_overrides[get_s3_mail_optional] = lambda: store
    try:
        org_id, _, key = org_and_key
        row = await _upload(async_session, org_id)
        resp = await client.post(
            "/verifications/document-check",
            data=_form(**{"upload.proof_of_identity": str(row.id)}),
            files={"x": (None, "")},
            headers=_auth(key),
        )
    finally:
        app.dependency_overrides.pop(get_s3_mail_optional, None)
    assert resp.status_code == 200, resp.text
    assert calls[0]["data"] == _JPEG
    # The upload is not used up.
    assert store.fetch_raw.await_count == 1


async def test_a_workspace_gets_a_daily_number_of_reads(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:

    monkeypatch.setattr(document_reads, "DAILY_DOCUMENT_READS", 2)
    calls = _fake_reader(monkeypatch, {**_ANSWER, "expiry_date": "2031-01-01"})
    org_id, _, key = org_and_key
    for _ in range(2):
        assert (await _post(client, key)).json()["documents"]["proof_of_identity"][
            "read"
        ]
    # The third read of the day is skipped: read false, no error, no warning.
    resp = await _post(client, key)
    assert resp.status_code == 200
    body = resp.json()
    assert body["documents"]["proof_of_identity"]["read"] is False
    # It says why, so the console does not blame the file.
    assert body["documents"]["proof_of_identity"]["skipped"] == "daily_limit"
    assert body["warnings"] == [] and body["fields"] == {}
    assert len(calls) == 2
    rows = (
        await async_session.execute(
            select(AgentSendLog.recipient).where(
                AgentSendLog.organization_id == org_id,
                AgentSendLog.channel == "document_read",
            )
        )
    ).scalars()
    # One row per read sent, with the slot name only.
    assert list(rows) == ["proof_of_identity", "proof_of_identity"]


async def test_a_read_not_sent_is_not_counted(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:

    _fake_reader(monkeypatch, None)
    org_id, _, key = org_and_key
    # A PDF over the page limit never reaches the model.
    pdf = b"%PDF-1.7\n" + b"<< /Type /Page >>\n" * 101
    files = {"file.proof_of_identity": ("a.pdf", pdf, "application/pdf")}
    monkeypatch.setattr(document_check, "read_document", _real_read_document)

    monkeypatch.setattr(settings, "anthropic_backup_model", "claude-haiku-5-5")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "anthropic_bedrock_region", "")
    resp = await _post(client, key, files=files)
    assert resp.json()["documents"]["proof_of_identity"]["read"] is False
    assert await document_reads_today(async_session, org_id) == 0


async def test_over_the_cap_no_upload_is_fetched(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:
    """The cap is checked before any file is fetched or decoded."""

    monkeypatch.setattr(document_reads, "DAILY_DOCUMENT_READS", 0)
    calls = _fake_reader(monkeypatch)
    store = AsyncMock()
    store.fetch_raw.return_value = _JPEG
    app.dependency_overrides[get_s3_mail_optional] = lambda: store
    try:
        org_id, _, key = org_and_key
        row = await _upload(async_session, org_id)
        resp = await client.post(
            "/verifications/document-check",
            data=_form(**{"upload.proof_of_identity": str(row.id)}),
            files={"x": (None, "")},
            headers=_auth(key),
        )
    finally:
        app.dependency_overrides.pop(get_s3_mail_optional, None)
    assert resp.status_code == 200
    assert resp.json()["documents"]["proof_of_identity"]["read"] is False
    store.fetch_raw.assert_not_awaited()
    assert calls == []


async def test_a_slot_not_in_the_form_is_refused_before_any_file_is_read(
    client, org_and_key, carrier, monkeypatch
) -> None:

    async def boom(*_a, **_k):
        raise AssertionError("no file may be read")

    monkeypatch.setattr(verification_files, "read_file", boom)
    _, _, key = org_and_key
    resp = await _post(
        client,
        key,
        documents=json.dumps({"proof_of_identity": {}, "nope": {}}),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "documents", "nope"]


def _real_reader(monkeypatch, error: Exception | None = None):
    """The real reader with a fake model client: it decodes in the child
    process, then sends to the fake model."""

    monkeypatch.setattr(document_check, "read_document", _real_read_document)
    monkeypatch.setattr(settings, "anthropic_backup_model", "claude-haiku-5-5")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "anthropic_bedrock_region", "")
    sent: list[dict] = []

    async def create(**kwargs):
        sent.append(kwargs)
        if error:
            raise error
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(_ANSWER))],
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(document_reader, "_client", client)
    return sent


def _png_file() -> dict:

    out = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(out, "PNG")
    return {"file.proof_of_identity": ("id.png", out.getvalue(), "image/png")}


async def test_a_read_whose_file_cannot_be_decoded_is_given_back(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:

    sent = _real_reader(monkeypatch)
    org_id, _, key = org_and_key
    broken = {"file.proof_of_identity": ("id.png", b"\x89PNG broken", "image/png")}
    resp = await _post(client, key, files=broken)
    assert resp.json()["documents"]["proof_of_identity"]["read"] is False
    assert sent == []
    assert await document_reads_today(async_session, org_id) == 0


async def test_a_read_sent_to_the_model_counts_even_when_the_call_fails(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:

    sent = _real_reader(monkeypatch, error=TimeoutError())
    org_id, _, key = org_and_key
    resp = await _post(client, key, files=_png_file())
    assert resp.json()["documents"]["proof_of_identity"]["read"] is False
    assert len(sent) == 1
    assert await document_reads_today(async_session, org_id) == 1
    _real_reader(monkeypatch, error=RuntimeError("upstream 500"))
    await _post(client, key, files=_png_file())
    assert await document_reads_today(async_session, org_id) == 2


async def test_without_a_reader_nothing_is_counted_or_fetched(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:

    monkeypatch.setattr(routes, "document_reader_configured", lambda: False)
    org_id, _, key = org_and_key
    resp = await _post(client, key)
    assert resp.json()["documents"]["proof_of_identity"]["read"] is False
    assert await document_reads_today(async_session, org_id) == 0


async def test_one_upload_in_two_slots_is_read_once_and_counted_once(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:

    async def two_slots(country_code, number_type, subject_type):
        passport = (DocumentOption(key="passport", label="Passport"),)
        return Requirements(
            provider="fake",
            country_code=country_code,
            number_type=number_type,
            subject_type=subject_type,
            required=True,
            fields=(FieldSpec(name="first_name", label="First name"),),
            documents=(
                DocumentSlot(name="identity", label="Identity", options=passport),
                DocumentSlot(name="birth", label="Birth date", options=passport),
            ),
            subject_types=("person",),
        )

    monkeypatch.setattr(carrier, "requirements", two_slots)
    calls = _fake_reader(monkeypatch)
    store = AsyncMock()
    store.fetch_raw.return_value = _JPEG
    app.dependency_overrides[get_s3_mail_optional] = lambda: store
    try:
        org_id, _, key = org_and_key
        row = await _upload(async_session, org_id)
        resp = await client.post(
            "/verifications/document-check",
            data=_form(
                documents=json.dumps({"identity": {}, "birth": {}}),
                **{"upload.identity": str(row.id), "upload.birth": str(row.id)},
            ),
            files={"x": (None, "")},
            headers=_auth(key),
        )
    finally:
        app.dependency_overrides.pop(get_s3_mail_optional, None)
    assert resp.status_code == 200, resp.text
    docs = resp.json()["documents"]
    assert docs["identity"]["read"] and docs["birth"]["read"]
    assert len(calls) == 1
    assert store.fetch_raw.await_count == 1
    assert await document_reads_today(async_session, org_id) == 1


async def test_a_failed_give_back_never_hides_the_answer(
    client, org_and_key, carrier, monkeypatch
) -> None:

    _fake_reader(monkeypatch, None)

    async def broken(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(routes, "release_document_reads", broken)
    _, _, key = org_and_key
    resp = await _post(client, key)
    assert resp.status_code == 200


async def test_a_failed_upload_fetch_leaves_the_file_parts_read(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:

    async def two_slots(country_code, number_type, subject_type):
        passport = (DocumentOption(key="passport", label="Passport"),)
        return Requirements(
            provider="fake",
            country_code=country_code,
            number_type=number_type,
            subject_type=subject_type,
            required=True,
            documents=(
                DocumentSlot(name="identity", label="Identity", options=passport),
                DocumentSlot(name="birth", label="Birth date", options=passport),
            ),
            subject_types=("person",),
        )

    monkeypatch.setattr(carrier, "requirements", two_slots)
    calls = _fake_reader(monkeypatch)
    store = AsyncMock()
    store.fetch_raw.side_effect = RuntimeError("bucket down")
    app.dependency_overrides[get_s3_mail_optional] = lambda: store
    try:
        org_id, _, key = org_and_key
        row = await _upload(async_session, org_id)
        resp = await client.post(
            "/verifications/document-check",
            data=_form(
                documents=json.dumps({"identity": {}, "birth": {}}),
                **{"upload.birth": str(row.id)},
            ),
            files={"file.identity": ("id.jpg", b"\xff\xd8jpeg", "image/jpeg")},
            headers=_auth(key),
        )
    finally:
        app.dependency_overrides.pop(get_s3_mail_optional, None)
    assert resp.status_code == 200, resp.text
    docs = resp.json()["documents"]
    assert docs["identity"]["read"] is True
    assert docs["birth"]["read"] is False
    assert len(calls) == 1
    # The upload's read was never sent: given back.
    assert await document_reads_today(async_session, org_id) == 1


async def test_wrong_json_types_are_422_and_null_is_empty(
    client, org_and_key, carrier, monkeypatch
) -> None:
    _fake_reader(monkeypatch)
    _, _, key = org_and_key
    for part in ({"fields": "[]"}, {"address": '""'}, {"documents": "[]"}):
        assert (await _post(client, key, **part)).status_code == 422, part
    ok = await _post(client, key, fields="null", address="null")
    assert ok.status_code == 200


async def test_a_failed_reservation_reads_nothing_and_still_answers(
    client, org_and_key, carrier, monkeypatch
) -> None:

    calls = _fake_reader(monkeypatch)

    async def broken(*_a, **_k):
        raise RuntimeError("lock timeout")

    monkeypatch.setattr(routes, "reserve_document_reads", broken)
    _, _, key = org_and_key
    resp = await _post(client, key)
    assert resp.status_code == 200
    assert resp.json()["documents"]["proof_of_identity"]["read"] is False
    # Not the cap: no reason given.
    assert resp.json()["documents"]["proof_of_identity"]["skipped"] is None
    assert calls == []


async def test_an_upload_without_a_bucket_is_read_false(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:

    calls = _fake_reader(monkeypatch)
    app.dependency_overrides[get_s3_mail_optional] = lambda: None
    try:
        org_id, _, key = org_and_key
        row = await _upload(async_session, org_id)
        resp = await client.post(
            "/verifications/document-check",
            data=_form(**{"upload.proof_of_identity": str(row.id)}),
            files={"x": (None, "")},
            headers=_auth(key),
        )
    finally:
        app.dependency_overrides.pop(get_s3_mail_optional, None)
    assert resp.status_code == 200, resp.text
    assert resp.json()["documents"]["proof_of_identity"]["read"] is False
    assert calls == []


async def test_a_covering_file_is_sent_once_and_fills_its_slots(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:

    async def two_slots(country_code, number_type, subject_type):
        passport = (DocumentOption(key="passport", label="Passport"),)
        return Requirements(
            provider="fake",
            country_code=country_code,
            number_type=number_type,
            subject_type=subject_type,
            required=True,
            documents=(
                DocumentSlot(name="identity", label="Identity", options=passport),
                DocumentSlot(name="birth", label="Birth date", options=passport),
                DocumentSlot(name="extra", label="Extra proof", options=passport),
            ),
            subject_types=("person",),
        )

    monkeypatch.setattr(carrier, "requirements", two_slots)
    calls = _fake_reader(monkeypatch)
    org_id, _, key = org_and_key
    resp = await _post(
        client,
        key,
        documents=json.dumps(
            {"identity": {"option": "passport", "covers": ["birth"]}, "birth": {}}
        ),
        files={"file.identity": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")},
    )
    assert resp.status_code == 200, resp.text
    docs = resp.json()["documents"]
    assert docs["identity"]["read"] and docs["birth"]["read"]
    assert len(calls) == 1
    assert "It is also given as: Birth date." in calls[0]["prompt"]
    assert await document_reads_today(async_session, org_id) == 1
    # A chain (identity covers birth, birth covers extra), listed in any
    # order: one read for all three.
    chain = await _post(
        client,
        key,
        documents=json.dumps(
            {
                "birth": {"covers": ["extra"]},
                "identity": {"option": "passport", "covers": ["birth"]},
            }
        ),
        files={"file.identity": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")},
    )
    assert chain.status_code == 200, chain.text
    assert all(chain.json()["documents"][s]["read"] for s in ("birth", "extra"))
    # The holder sent no file: the covered slot still comes back, read false.
    orphan = await _post(
        client,
        key,
        documents=json.dumps({"identity": {"covers": ["birth"]}}),
        files={"x": (None, "")},
    )
    assert orphan.status_code == 200, orphan.text
    assert orphan.json()["documents"]["birth"]["read"] is False
    bad = await _post(
        client,
        key,
        documents=json.dumps({"identity": {"covers": ["nope"]}}),
        files={"file.identity": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")},
    )
    assert bad.status_code == 422


# -- POST /verifications/document-cautions -------------------------------------


@pytest.fixture(autouse=True)
def _no_cached_forms():
    routes._caution_forms.clear()
    yield
    routes._caution_forms.clear()


def _two_types(carrier) -> None:
    """The fake carrier's identity slot takes a passport or an ID card."""
    base = carrier.requirements

    async def requirements(country_code, number_type, subject_type):
        req = await base(country_code, number_type, subject_type)
        slot = req.documents[0]
        options = (*slot.options, DocumentOption(key="id_card", label="ID card"))
        return req.model_copy(
            update={"documents": (slot.model_copy(update={"options": options}),)}
        )

    carrier.requirements = requirements


def _cautions_body(**over) -> dict:
    body = {
        "quote_id": base._QUOTE_ID,
        "country_code": "GB",
        "number_type": "mobile",
        "subject_type": "person",
        "fields": {"first_name": "Elena", "last_name": "Tester"},
        "address": {},
        "documents": {
            "proof_of_identity": {
                "option": "passport",
                "fields": {},
                "read": {
                    "option": "passport",
                    "printed_name": {
                        "given_names": "MARIA ELENA",
                        "surnames": "TESTER",
                        "latin_given_names": "",
                        "latin_surnames": "",
                        "business_name": "",
                    },
                },
            }
        },
    }
    body.update(over)
    return body


async def _cautions(client, key, body):
    return await client.post(
        "/verifications/document-cautions", json=body, headers=_auth(key)
    )


async def test_cautions_give_the_name_warning_with_no_read_used(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:
    async def no_model(**_):
        raise AssertionError("no model is called")

    monkeypatch.setattr(document_check, "read_document", no_model)
    org_id, _, key = org_and_key
    resp = await _cautions(client, key, _cautions_body())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "warnings": [
            {
                "field": "first_name",
                "kind": "name",
                "message": (
                    "Your Passport shows first name MARIA ELENA and last name "
                    "TESTER. You typed Elena / Tester. Type them exactly as "
                    "printed."
                ),
            }
        ]
    }
    fixed = _cautions_body(fields={"first_name": "Maria Elena", "last_name": ""})
    assert (await _cautions(client, key, fixed)).json() == {"warnings": []}
    assert await document_reads_today(async_session, org_id) == 0
    assert carrier.drafts == []
    count = await async_session.scalar(select(func.count(CarrierVerification.id)))
    assert count == 0


async def test_cautions_take_the_document_checks_own_answer(
    client, org_and_key, carrier, monkeypatch
) -> None:
    """The console sends back each slot's `documents.<slot>` as the check
    returned it; the cautions then match the check's own warnings."""
    _two_types(carrier)
    _fake_reader(
        monkeypatch, {**_ANSWER, "option": "id_card", "expiry_date": "2031-01-01"}
    )
    _, _, key = org_and_key
    checked = (await _post(client, key)).json()
    assert sorted(w["kind"] for w in checked["warnings"]) == ["document_type", "name"]
    body = _cautions_body(
        fields={"first_name": "Elena", "last_name": "Tester"},
        documents={
            "proof_of_identity": {
                "option": "passport",
                "read": checked["documents"]["proof_of_identity"],
            }
        },
    )
    resp = await _cautions(client, key, body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == checked["warnings"]
    # The type fixed: only the name is left.
    body["documents"]["proof_of_identity"]["option"] = "id_card"
    kinds = [w["kind"] for w in (await _cautions(client, key, body)).json()["warnings"]]
    assert kinds == ["name"]


async def test_cautions_skip_a_slot_without_a_read(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    body = _cautions_body(documents={"proof_of_identity": {"option": "passport"}})
    resp = await _cautions(client, key, body)
    assert resp.status_code == 200 and resp.json() == {"warnings": []}


async def test_cautions_check_the_quote_the_slots_and_auth(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    unknown = await _cautions(client, key, _cautions_body(documents={"nope": {}}))
    assert unknown.status_code == 422
    assert unknown.json()["detail"][0]["loc"] == ["body", "documents", "nope"]
    no_quote = _cautions_body()
    del no_quote["quote_id"]
    assert (await _cautions(client, key, no_quote)).status_code == 422
    wrong = _cautions_body(subject_type="robot")
    assert (await _cautions(client, key, wrong)).status_code == 422
    resp = await client.post("/verifications/document-cautions", json={})
    assert resp.status_code == 401


async def test_cautions_ask_the_carrier_for_its_form_once(
    client, org_and_key, carrier, monkeypatch
) -> None:
    asked: list[tuple] = []
    base_requirements = carrier.requirements

    async def requirements(*args):
        asked.append(args)
        return await base_requirements(*args)

    carrier.requirements = requirements
    _, _, key = org_and_key
    for _ in range(3):
        assert (await _cautions(client, key, _cautions_body())).status_code == 200
    assert len(asked) == 1
    # Past the time limit the form is asked again.
    monkeypatch.setattr(routes, "_CAUTION_FORM_TTL_S", 0)
    assert (await _cautions(client, key, _cautions_body())).status_code == 200
    assert len(asked) == 2


async def test_fetches_of_several_files_share_one_limit() -> None:
    """The document check fetches each file with its own call; the shared
    gate keeps them to MAX_PARALLEL_FETCHES at once in all."""
    running = peak = 0

    async def fetch_raw(_key):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return b"\xff\xd8jpeg"

    s3 = SimpleNamespace(fetch_raw=fetch_raw)
    rows = {
        f"slot{i}": SimpleNamespace(
            id=uuid.uuid4(), s3_key=f"k{i}", content_type="image/jpeg"
        )
        for i in range(MAX_PARALLEL_FETCHES * 2)
    }
    collected = CollectedFiles(rows=rows, parts={})
    gate = asyncio.Semaphore(MAX_PARALLEL_FETCHES)
    await asyncio.gather(
        *(fetch_files(collected, [slot], s3, gate=gate) for slot in rows)
    )
    assert peak == MAX_PARALLEL_FETCHES
