# ruff: noqa: F811  (fixtures imported from test_verifications_api)
"""``upload.<slot>`` parts and ``POST /verifications/check``.

A document sent through an upload link (``POST /uploads``) is named by its
id in the form. The check runs every step of submit before filing and
saves nothing.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from hailhq.api.deps import get_s3_mail_optional
from hailhq.api.main import app
from hailhq.core.models import AccountCredit, CarrierVerification, FileUpload
from hailhq.core.providers.verification import Problem
from sqlalchemy import func, select

from . import test_verifications_api as base
from .test_verifications_api import (  # noqa: F401  (fixtures)
    _auth,
    _passport,
    _sms_submission,
    _submission,
    carrier,
    pinned_catalog,
    sms_carrier,
    us_number,
)

_JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64


@pytest.fixture()
def s3():
    store = AsyncMock()
    store.fetch_raw.return_value = _JPEG
    app.dependency_overrides[get_s3_mail_optional] = lambda: store
    try:
        yield store
    finally:
        app.dependency_overrides.pop(get_s3_mail_optional, None)


async def _upload(
    async_session,
    org_id,
    *,
    purpose: str = "verification_document",
    age: timedelta = timedelta(minutes=5),
    uploaded: bool = True,
    data: bytes = _JPEG,
) -> FileUpload:
    upload_id = uuid.uuid4()
    row = FileUpload(
        id=upload_id,
        organization_id=org_id,
        purpose=purpose,
        filename="passport.jpg",
        content_type="image/jpeg",
        size_bytes=len(data) if uploaded else None,
        sha256=hashlib.sha256(data).hexdigest() if uploaded else None,
        s3_key=f"verification-uploads/{org_id}/{upload_id}",
        uploaded_at=datetime.now(timezone.utc) - age if uploaded else None,
        token_hash=None if uploaded else "h",
        link_expires_at=None if uploaded else datetime.now(timezone.utc),
    )
    async_session.add(row)
    await async_session.commit()
    return row


async def _post(client, key, path="/verifications", files=None, **parts):
    # A (None, value) file entry makes httpx send multipart/form-data.
    return await client.post(
        path,
        data={**_submission(), **parts},
        files=files or {"_": (None, "1")},
        headers=_auth(key),
    )


async def _rows(async_session, model) -> int:
    return (
        await async_session.execute(select(func.count()).select_from(model))
    ).scalar_one()


# -- upload.<slot> on submit ------------------------------------------------


async def test_submit_reads_an_uploaded_document(
    client, org_and_key, carrier, s3, async_session
) -> None:
    org_id, _, key = org_and_key
    row = await _upload(async_session, org_id)
    resp = await _post(client, key, **{"upload.proof_of_identity": str(row.id)})
    assert resp.status_code == 201, resp.text
    doc = carrier.drafts[0]["documents"]["proof_of_identity"]
    assert doc.file.data == _JPEG
    assert doc.file.content_type == "image/jpeg"
    s3.fetch_raw.assert_awaited_once_with(row.s3_key)
    # Used: the object and the row are gone.
    s3.delete.assert_awaited_once_with(row.s3_key)
    async_session.expire_all()
    assert await async_session.get(FileUpload, row.id) is None


async def test_submit_keeps_the_upload_when_the_carrier_refuses(
    client, org_and_key, carrier, s3, async_session
) -> None:
    org_id, _, key = org_and_key
    row = await _upload(async_session, org_id)
    row_id = row.id
    carrier.problems = [Problem(field="first_name", message="First name is wrong.")]
    resp = await _post(client, key, **{"upload.proof_of_identity": str(row_id)})
    assert resp.status_code == 422
    s3.delete.assert_not_awaited()
    async_session.expire_all()
    assert await async_session.get(FileUpload, row_id) is not None


async def test_submit_keeps_the_upload_on_a_402(
    client, org_and_key, carrier, s3, async_session
) -> None:
    org_id, _, key = org_and_key
    row_id = (await _upload(async_session, org_id)).id
    await base._set_balance(async_session, org_id, 0)
    resp = await _post(client, key, **{"upload.proof_of_identity": str(row_id)})
    assert resp.status_code == 402
    assert carrier.drafts == []
    async_session.expire_all()
    assert await async_session.get(FileUpload, row_id) is not None


async def test_a_failed_cleanup_does_not_fail_the_submit(
    client, org_and_key, carrier, s3, async_session
) -> None:
    """One failed delete leaves its row for the GC; the next is still tried."""
    org_id, _, key = org_and_key
    carrier_requirements = carrier.requirements

    async def two_slots(*args):
        from hailhq.core.providers.verification import DocumentOption, DocumentSlot

        req = await carrier_requirements(*args)
        extra = DocumentSlot(
            name="proof_of_address",
            label="Proof of address",
            options=(DocumentOption(key="bill", label="Bill"),),
        )
        return req.model_copy(update={"documents": (*req.documents, extra)})

    carrier.requirements = two_slots
    first = await _upload(async_session, org_id)
    second = await _upload(async_session, org_id)
    first_id, second_id = first.id, second.id
    s3.delete.side_effect = [RuntimeError("s3 down"), None]
    resp = await _post(
        client,
        key,
        documents=json.dumps(
            {"proof_of_identity": {"option": "passport"}, "proof_of_address": {}}
        ),
        **{
            "upload.proof_of_identity": str(first_id),
            "upload.proof_of_address": str(second_id),
        },
    )
    assert resp.status_code == 201, resp.text
    assert s3.delete.await_count == 2
    async_session.expire_all()
    assert await async_session.get(FileUpload, first_id) is not None
    assert await async_session.get(FileUpload, second_id) is None


@pytest.mark.parametrize(
    "case", ["other_org", "email_attachment", "too_old", "not_uploaded", "unknown"]
)
async def test_submit_refuses_an_upload_it_may_not_use(
    client, org_and_key, carrier, s3, async_session, case
) -> None:
    org_id, _, key = org_and_key
    if case == "unknown":
        upload_id = uuid.uuid4()
    else:
        row = await _upload(
            async_session,
            uuid.uuid4() if case == "other_org" else org_id,
            purpose=(
                "email_attachment"
                if case == "email_attachment"
                else ("verification_document")
            ),
            age=timedelta(minutes=61) if case == "too_old" else timedelta(minutes=1),
            uploaded=case != "not_uploaded",
        )
        upload_id = row.id
    resp = await _post(client, key, **{"upload.proof_of_identity": str(upload_id)})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail[0]["loc"] == ["body", "upload.proof_of_identity"]
    assert carrier.drafts == []
    s3.fetch_raw.assert_not_awaited()


async def test_submit_refuses_a_file_and_an_upload_for_one_slot(
    client, org_and_key, carrier, s3, async_session
) -> None:
    org_id, _, key = org_and_key
    row = await _upload(async_session, org_id)
    resp = await _post(
        client,
        key,
        files=_passport(),
        **{"upload.proof_of_identity": str(row.id)},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "upload.proof_of_identity"]
    assert carrier.drafts == []


async def test_submit_refuses_an_upload_id_that_is_not_a_uuid(
    client, org_and_key, carrier, s3
) -> None:
    _, _, key = org_and_key
    resp = await _post(client, key, **{"upload.proof_of_identity": "nope"})
    assert resp.status_code == 422
    assert "nope" not in resp.text


async def test_submit_refuses_an_upload_for_a_slot_not_in_documents(
    client, org_and_key, carrier, s3, async_session
) -> None:
    org_id, _, key = org_and_key
    row = await _upload(async_session, org_id)
    resp = await _post(client, key, **{"upload.other": str(row.id)})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "upload.other"]


async def test_submit_with_an_upload_needs_storage(
    client, org_and_key, carrier, async_session, monkeypatch
) -> None:
    from hailhq.core.config import settings

    org_id, _, key = org_and_key
    monkeypatch.setattr(settings, "hail_mail_name_prefix", "")
    row = await _upload(async_session, org_id)
    resp = await _post(client, key, **{"upload.proof_of_identity": str(row.id)})
    assert resp.status_code == 503
    assert carrier.drafts == []


# -- POST /verifications/check ---------------------------------------------


async def test_check_passes_and_names_each_file(
    client, org_and_key, carrier, s3, async_session
) -> None:
    org_id, _, key = org_and_key
    row = await _upload(async_session, org_id)
    row_id = row.id
    balance = await base._balance(async_session, org_id)
    credits = await _rows(async_session, AccountCredit)
    resp = await _post(
        client, key, "/verifications/check", **{"upload.proof_of_identity": str(row_id)}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["files"] == [
        {
            "slot": "proof_of_identity",
            "upload_id": str(row_id),
            "size_bytes": len(_JPEG),
            "sha256": hashlib.sha256(_JPEG).hexdigest(),
        }
    ]
    assert "carrier" in body["note"]
    # Nothing filed, nothing saved, nothing held, nothing consumed.
    assert carrier.drafts == []
    assert carrier.submitted == []
    assert await _rows(async_session, CarrierVerification) == 0
    assert await _rows(async_session, AccountCredit) == credits
    assert await base._balance(async_session, org_id) == balance
    s3.delete.assert_not_awaited()
    async_session.expire_all()
    assert await async_session.get(FileUpload, row_id) is not None


async def test_check_reads_file_parts_too(client, org_and_key, carrier, s3) -> None:
    _, _, key = org_and_key
    resp = await _post(client, key, "/verifications/check", files=_passport())
    assert resp.status_code == 200, resp.text
    (entry,) = resp.json()["files"]
    assert entry["upload_id"] is None
    assert entry["sha256"] == hashlib.sha256(b"\xff\xd8jpeg").hexdigest()


async def test_check_returns_local_problems_as_422(
    client, org_and_key, carrier, s3
) -> None:
    _, _, key = org_and_key
    resp = await _post(
        client,
        key,
        "/verifications/check",
        files=_passport(),
        fields=json.dumps({"first_name": "Ada"}),
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail[0]["loc"] == ["body", "last_name"]
    assert detail[0]["type"] == "verification_problem"
    assert carrier.drafts == []


async def test_check_needs_the_file(client, org_and_key, carrier, s3) -> None:
    _, _, key = org_and_key
    resp = await _post(client, key, "/verifications/check")
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "proof_of_identity"]


async def test_check_gives_the_same_409_as_submit(
    client, org_and_key, carrier, s3
) -> None:
    _, _, key = org_and_key
    first = await _post(client, key, files=_passport())
    assert first.status_code == 201
    resp = await _post(client, key, "/verifications/check", files=_passport())
    assert resp.status_code == 409


async def test_check_gives_the_same_402_as_submit(
    client, org_and_key, carrier, s3, async_session
) -> None:
    org_id, _, key = org_and_key
    await base._set_balance(async_session, org_id, 0)
    resp = await _post(client, key, "/verifications/check", files=_passport())
    assert resp.status_code == 402


@pytest.mark.usefixtures("us_number")
async def test_check_sms_runs_the_application_check(
    client, org_and_key, sms_carrier, s3, async_session
) -> None:
    org_id, _, key = org_and_key
    await base._set_balance(async_session, org_id, 10_000)
    resp = await client.post(
        "/verifications/check",
        data=_sms_submission(),
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    # The shared US form needs much more than the fake's two fields.
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["type"] == "verification_problem"
    assert sms_carrier.drafts == []
    assert await _rows(async_session, CarrierVerification) == 0


@pytest.mark.usefixtures("us_number")
async def test_check_sms_passes_when_the_application_is_complete(
    client, org_and_key, sms_carrier, s3, async_session, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    await base._set_balance(async_session, org_id, 10_000)
    monkeypatch.setattr(sms_carrier, "validate_application", lambda *a: [])
    resp = await client.post(
        "/verifications/check",
        data=_sms_submission(),
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["files"] == []
    assert sms_carrier.drafts == []


async def test_twenty_uploads_fit_in_one_form(
    client, org_and_key, carrier, s3, async_session
) -> None:
    """The MCP sends up to 20 files; every one may be an upload.<slot> part."""
    org_id, _, key = org_and_key
    slots = ["proof_of_identity", *(f"extra_{i}" for i in range(19))]
    ids = []
    for _ in slots:
        ids.append(str((await _upload(async_session, org_id)).id))
    resp = await _post(
        client,
        key,
        "/verifications/check",
        documents=json.dumps({slot: {"option": "passport"} for slot in slots}),
        address=json.dumps(
            {
                "customer_name": "Ada",
                "street": "1 Road",
                "city": "London",
                "region": "LDN",
                "postal_code": "N1",
                "country_code": "GB",
            }
        ),
        **{f"upload.{slot}": upload_id for slot, upload_id in zip(slots, ids)},
    )
    assert resp.status_code == 200, resp.text
    assert [f["slot"] for f in resp.json()["files"]] == slots
    assert s3.fetch_raw.await_count == 20


async def test_uploads_are_fetched_at_the_same_time(
    client, org_and_key, carrier, s3, async_session
) -> None:
    """Every fetch starts before the first one returns."""
    import asyncio

    org_id, _, key = org_and_key
    slots = ["proof_of_identity", "extra_0", "extra_1"]
    ids = [str((await _upload(async_session, org_id)).id) for _ in slots]
    started, release = [], asyncio.Event()

    async def fetch(key_):
        started.append(key_)
        if len(started) == len(slots):
            release.set()
        await asyncio.wait_for(release.wait(), 2)
        return _JPEG

    s3.fetch_raw.side_effect = fetch
    resp = await _post(
        client,
        key,
        "/verifications/check",
        documents=json.dumps({slot: {"option": "passport"} for slot in slots}),
        **{f"upload.{slot}": upload_id for slot, upload_id in zip(slots, ids)},
    )
    assert resp.status_code == 200, resp.text
    assert len(started) == 3


async def test_used_uploads_are_deleted_in_one_statement(
    client, org_and_key, carrier, s3, async_session, monkeypatch
) -> None:
    from hailhq.api.routes import verifications as routes

    org_id, _, key = org_and_key
    carrier_requirements = carrier.requirements

    async def two_slots(*args):
        from hailhq.core.providers.verification import DocumentOption, DocumentSlot

        req = await carrier_requirements(*args)
        extra = DocumentSlot(
            name="proof_of_address",
            label="Proof of address",
            options=(DocumentOption(key="bill", label="Bill"),),
        )
        return req.model_copy(update={"documents": (*req.documents, extra)})

    carrier.requirements = two_slots
    first_id = (await _upload(async_session, org_id)).id
    second_id = (await _upload(async_session, org_id)).id
    statements = []
    real_delete = routes.delete

    def counting_delete(*args, **kwargs):
        statements.append(args)
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(routes, "delete", counting_delete)
    resp = await _post(
        client,
        key,
        documents=json.dumps(
            {"proof_of_identity": {"option": "passport"}, "proof_of_address": {}}
        ),
        **{
            "upload.proof_of_identity": str(first_id),
            "upload.proof_of_address": str(second_id),
        },
    )
    assert resp.status_code == 201, resp.text
    assert len(statements) == 1
    async_session.expire_all()
    assert await async_session.get(FileUpload, first_id) is None
    assert await async_session.get(FileUpload, second_id) is None


async def test_one_upload_can_fill_two_slots_and_is_fetched_once(
    client, org_and_key, carrier, s3, async_session
) -> None:
    """Each slot gets the bytes, and each use counts toward the total."""
    org_id, _, key = org_and_key
    upload_id = str((await _upload(async_session, org_id)).id)
    resp = await _post(
        client,
        key,
        "/verifications/check",
        documents=json.dumps(
            {"proof_of_identity": {"option": "passport"}, "extra_0": {}}
        ),
        **{"upload.proof_of_identity": upload_id, "upload.extra_0": upload_id},
    )
    assert resp.status_code == 200, resp.text
    files = resp.json()["files"]
    assert [f["slot"] for f in files] == ["proof_of_identity", "extra_0"]
    assert {f["upload_id"] for f in files} == {upload_id}
    assert s3.fetch_raw.await_count == 1


async def test_one_upload_in_many_slots_counts_each_use_in_the_total(
    client, org_and_key, carrier, s3, async_session
) -> None:
    from hailhq.api.routes.verifications import MAX_FILE_BYTES

    org_id, _, key = org_and_key
    row = await _upload(async_session, org_id)
    row.size_bytes = MAX_FILE_BYTES
    upload_id = str(row.id)
    await async_session.commit()
    slots = ["proof_of_identity", "extra_0", "extra_1", "extra_2"]
    resp = await _post(
        client,
        key,
        "/verifications/check",
        documents=json.dumps({slot: {} for slot in slots}),
        **{f"upload.{slot}": upload_id for slot in slots},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["msg"] == "the files total more than 30 MB"
    s3.fetch_raw.assert_not_awaited()


async def test_one_failed_fetch_cancels_the_others(
    client, org_and_key, carrier, s3, async_session
) -> None:
    import asyncio

    org_id, _, key = org_and_key
    slots = ["proof_of_identity", "extra_0"]
    rows = [await _upload(async_session, org_id) for _ in slots]
    failing_key = rows[0].s3_key
    cancelled = asyncio.Event()

    async def fetch(key_):
        if key_ == failing_key:
            await asyncio.sleep(0.05)
            raise RuntimeError("s3 down")
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return _JPEG

    s3.fetch_raw.side_effect = fetch
    resp = await asyncio.wait_for(
        _post(
            client,
            key,
            "/verifications/check",
            documents=json.dumps({slot: {} for slot in slots}),
            **{f"upload.{slot}": str(row.id) for slot, row in zip(slots, rows)},
        ),
        5,
    )
    assert resp.status_code == 503, resp.text
    assert cancelled.is_set()


async def test_uploads_over_the_total_are_refused_before_any_fetch(
    client, org_and_key, carrier, s3, async_session
) -> None:
    """The rows' sizes, plus the file parts, are summed before fetching."""
    from hailhq.api.routes.verifications import MAX_FILE_BYTES

    org_id, _, key = org_and_key
    slots = ["proof_of_identity", "extra_0", "extra_1"]
    ids = []
    for _ in slots:
        row = await _upload(async_session, org_id)
        row.size_bytes = MAX_FILE_BYTES
        ids.append(str(row.id))
    await async_session.commit()
    resp = await _post(
        client,
        key,
        "/verifications/check",
        files={"file.extra_2": ("a.jpg", b"\xff\xd8\xff" + b"x" * 64, "image/jpeg")},
        documents=json.dumps({slot: {} for slot in [*slots, "extra_2"]}),
        **{f"upload.{slot}": upload_id for slot, upload_id in zip(slots, ids)},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["msg"] == "the files total more than 30 MB"
    s3.fetch_raw.assert_not_awaited()


async def test_at_most_four_uploads_are_fetched_at_once(
    client, org_and_key, carrier, s3, async_session
) -> None:
    import asyncio

    org_id, _, key = org_and_key
    slots = ["proof_of_identity", *(f"extra_{i}" for i in range(7))]
    ids = [str((await _upload(async_session, org_id)).id) for _ in slots]
    running, peak = 0, 0

    async def fetch(_key):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return _JPEG

    s3.fetch_raw.side_effect = fetch
    resp = await _post(
        client,
        key,
        "/verifications/check",
        documents=json.dumps({slot: {"option": "passport"} for slot in slots}),
        **{f"upload.{slot}": upload_id for slot, upload_id in zip(slots, ids)},
    )
    assert resp.status_code == 200, resp.text
    assert peak == 4
