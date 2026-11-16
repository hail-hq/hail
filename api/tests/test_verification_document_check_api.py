# ruff: noqa: F811  (fixtures imported from test_verifications_api)
"""``POST /verifications/document-check``: read the files, return values and
warnings, file and save nothing. The model is a fake; names are made up."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

from hailhq.core.models import CarrierVerification
from hailhq.core.providers.verification import document_check
from sqlalchemy import func, select

from . import test_verifications_api as base
from .test_verifications_api import (  # noqa: F401  (fixtures)
    _auth,
    _passport,
    carrier,
    pinned_catalog,
)

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
    calls: list[dict] = []

    async def read(**kwargs):
        calls.append(kwargs)
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
    stray = await _post(client, key, documents="{}")
    assert stray.status_code == 422
    assert stray.json()["detail"][0]["loc"] == ["body", "file.proof_of_identity"]


async def test_requires_auth(client) -> None:
    resp = await client.post("/verifications/document-check", data={})
    assert resp.status_code == 401


async def test_an_upload_id_is_read_after_the_transaction_ends(
    client, org_and_key, carrier, monkeypatch, async_session
) -> None:
    from hailhq.api.deps import get_s3_mail_optional
    from hailhq.api.main import app

    from .test_verifications_uploads import _JPEG, _upload

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
