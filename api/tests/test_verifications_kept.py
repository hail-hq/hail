# ruff: noqa: F811  (fixtures imported from test_verifications_api)
"""A number registration keeps its answers and files for a resubmit.

Kept on submit (answers in ``provider_refs.application``, an encrypted copy
of each file as a ``verification_kept`` upload), returned by
``GET /verifications/{id}/application``, filed again by
``POST /verifications/{id}/resubmit``, and deleted 30 days after submit or
when the registration is approved or cancelled or the account closes.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from hailhq.api.deps import get_s3_mail_optional
from hailhq.api.main import app
from hailhq.api.number_orders import purge_expired_quotes
from hailhq.api.routes import verifications as verifications_routes
from hailhq.api.routes.verifications import (
    forget_kept_registrations,
    sweep_verifications,
)
from hailhq.api.superadmin import require_superadmin
from hailhq.core.config import settings
from hailhq.core.file_uploads import KEPT_PURPOSE, VERIFICATION_KEPT_PREFIX
from hailhq.core.models import (
    CarrierVerification,
    FileUpload,
    NumberOffer,
    OrgClosure,
)
from hailhq.core.providers.verification import Problem, ProviderStatus
from hailhq.core.secret_cipher import generate_key
from sqlalchemy import select, update

from .test_verifications_api import (  # noqa: F401  (fixtures)
    _auth,
    _create,
    _unsent,
    carrier,
    pinned_catalog,
)

_JPEG = b"\xff\xd8jpeg"


class FakeStore:
    """An in-memory mail bucket."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[dict] = []
        self.fail_put = False

    async def put_attachment(self, key, payload, content_type, *, encrypt=False):
        if self.fail_put:
            raise RuntimeError("s3 down")
        self.puts.append({"key": key, "content_type": content_type, "encrypt": encrypt})
        self.objects[key] = payload

    async def fetch_raw(self, key, *, max_bytes=None):
        return self.objects[key]

    async def delete_prefix(self, prefix, *, keep=None):
        gone = [k for k in self.objects if k.startswith(prefix) and k != keep]
        for k in gone:
            del self.objects[k]
        return len(gone)


@pytest.fixture()
def store(monkeypatch):
    fake = FakeStore()
    app.dependency_overrides[get_s3_mail_optional] = lambda: fake
    # The inline deletes and the worker read the bucket themselves.
    monkeypatch.setattr(verifications_routes, "get_s3_mail_optional", lambda: fake)
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_s3_mail_optional, None)


def _registry(carrier):
    return lambda name, purpose="number": carrier if name == "fake" else None


async def _kept(async_session, vid=None) -> list[FileUpload]:
    async_session.expire_all()
    query = select(FileUpload).where(FileUpload.purpose == KEPT_PURPOSE)
    if vid is not None:
        query = query.where(FileUpload.verification_id == uuid.UUID(str(vid)))
    return list((await async_session.execute(query)).scalars().all())


async def _row(async_session, vid) -> CarrierVerification:
    async_session.expire_all()
    return await async_session.get(CarrierVerification, uuid.UUID(str(vid)))


async def _rejected(client, key, carrier, reason="The passport photo is blurry."):
    """A filed registration the carrier then rejects (read on the next GET)."""
    vid = (await _create(client, key)).json()["id"]
    carrier.remote_status = ProviderStatus(state="rejected", reason=reason)
    verifications_routes._last_polled.clear()
    body = (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()
    assert body["state"] == "rejected"
    carrier.remote_status = ProviderStatus(state="pending")
    return vid


async def _age(async_session, vid, days: int) -> None:
    await async_session.execute(
        update(CarrierVerification)
        .where(CarrierVerification.id == uuid.UUID(str(vid)))
        .values(created_at=datetime.now(timezone.utc) - timedelta(days=days))
    )
    await async_session.commit()


# -- keep -----------------------------------------------------------------


async def test_submit_keeps_the_answers_and_an_encrypted_copy_of_each_file(
    client, org_and_key, carrier, store, async_session
) -> None:
    org_id, _, key = org_and_key
    resp = await _create(client, key)
    assert resp.status_code == 201, resp.text
    vid = resp.json()["id"]

    [kept] = await _kept(async_session, vid)
    assert kept.slot == "proof_of_identity"
    assert kept.filename == "ada.jpg" and kept.content_type == "image/jpeg"
    assert kept.size_bytes == len(_JPEG) and kept.uploaded_at is not None
    assert kept.organization_id == org_id
    assert kept.s3_key.startswith(f"{VERIFICATION_KEPT_PREFIX}{org_id}/")
    assert store.objects[kept.s3_key] == _JPEG
    # Encrypted at rest by S3, whatever the bucket default.
    assert store.puts == [
        {"key": kept.s3_key, "content_type": "image/jpeg", "encrypt": True}
    ]
    body = resp.json()
    assert body["kept_until"] is not None and body["resubmittable"] is False


async def test_the_application_lists_the_answers_and_files_never_the_bytes(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    resp = await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["subject_type"] == "person"
    assert body["fields"] == {"first_name": "Ada", "last_name": "Lovelace"}
    assert body["documents"] == {
        "proof_of_identity": {"option": "passport", "fields": {}}
    }
    assert body["files"] == [
        {
            "slot": "proof_of_identity",
            "filename": "ada.jpg",
            "content_type": "image/jpeg",
            "size_bytes": len(_JPEG),
        }
    ]
    row = await _row(async_session, vid)
    assert datetime.fromisoformat(body["kept_until"]) == row.created_at + timedelta(
        days=30
    )
    listed = (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()
    assert listed["resubmittable"] is True


async def test_a_failed_file_copy_does_not_fail_the_filing(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    store.fail_put = True
    resp = await _create(client, key)
    assert resp.status_code == 201, resp.text
    assert resp.json()["state"] == "submitted"
    assert await _kept(async_session) == []
    # The answers are still kept.
    row = await _row(async_session, resp.json()["id"])
    assert "application" in row.provider_refs


async def test_a_draft_the_carrier_refuses_at_submit_keeps_its_answers(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    carrier.check_problems = [Problem(field="", message="document unreadable")]

    async def refuse(refs):
        from hailhq.core.providers.verification import SubmitRefused

        raise SubmitRefused([Problem(field="", message="document unreadable")])

    carrier.submit = refuse
    resp = await _create(client, key)
    assert resp.status_code == 201, resp.text
    row = await _row(async_session, resp.json()["id"])
    assert row.state == "rejected"
    assert row.provider_refs["application"]["fields"]["first_name"] == "Ada"
    assert len(await _kept(async_session, row.id)) == 1


# -- delete ---------------------------------------------------------------


async def test_approval_deletes_the_answers_and_files(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = (await _create(client, key)).json()["id"]
    carrier.remote_status = ProviderStatus(state="approved")
    verifications_routes._last_polled.clear()
    body = (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()
    assert body["state"] == "approved" and body["kept_until"] is None
    row = await _row(async_session, vid)
    assert "application" not in row.provider_refs
    assert await _kept(async_session) == [] and store.objects == {}
    resp = await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    assert resp.status_code == 404


async def test_cancel_deletes_the_answers_and_files(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200 and resp.json()["state"] == "cancelled"
    row = await _row(async_session, vid)
    assert "application" not in row.provider_refs
    assert await _kept(async_session) == [] and store.objects == {}


async def test_the_worker_deletes_them_30_days_after_submit(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    fresh = await _rejected(client, key, carrier, reason="Other")
    await _age(async_session, vid, 30)
    await _age(async_session, fresh, 29)

    counts = await sweep_verifications(async_session, _registry(carrier))
    assert counts["kept_answers_deleted"] == 1
    assert counts["kept_files_deleted"] == 1
    assert "application" not in (await _row(async_session, vid)).provider_refs
    assert await _kept(async_session, vid) == []
    # 29 days: still kept.
    assert "application" in (await _row(async_session, fresh)).provider_refs
    assert len(await _kept(async_session, fresh)) == 1
    resp = await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    assert resp.status_code == 404


async def test_past_30_days_the_answers_are_gone_before_the_worker_runs(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    await _age(async_session, vid, 31)
    resp = await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    assert resp.status_code == 404
    resp = await client.post(
        f"/verifications/{vid}/resubmit",
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 404


async def test_account_close_deletes_them(
    client, org_and_key, carrier, store, async_session
) -> None:
    org_id, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    async_session.add(
        OrgClosure(
            organization_id=org_id,
            closed_at=datetime.now(timezone.utc),
            source="test",
        )
    )
    await async_session.commit()
    answers, files = await forget_kept_registrations(
        async_session, datetime.now(timezone.utc), organization_id=org_id
    )
    assert (answers, files) == (1, 1)
    assert "application" not in (await _row(async_session, vid)).provider_refs
    assert await _kept(async_session) == [] and store.objects == {}


async def test_a_failed_object_delete_keeps_the_row_for_the_next_pass(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    await _age(async_session, vid, 30)

    async def down(prefix, *, keep=None):
        raise RuntimeError("s3 down")

    real = store.delete_prefix
    store.delete_prefix = down
    _, files = await forget_kept_registrations(
        async_session, datetime.now(timezone.utc)
    )
    assert files == 0 and len(await _kept(async_session, vid)) == 1
    store.delete_prefix = real
    _, files = await forget_kept_registrations(
        async_session, datetime.now(timezone.utc)
    )
    assert files == 1 and await _kept(async_session, vid) == []


# -- resubmit -------------------------------------------------------------


async def test_resubmit_files_again_from_the_kept_answers_and_files(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    carrier.drafts.clear()
    resp = await client.post(
        f"/verifications/{vid}/resubmit",
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    new = resp.json()
    assert new["id"] != vid and new["state"] == "submitted"
    draft = carrier.drafts[0]
    assert draft["fields"] == {"first_name": "Ada", "last_name": "Lovelace"}
    doc = draft["documents"]["proof_of_identity"]
    assert doc.option == "passport" and doc.file.data == _JPEG

    # The old filing is dismissed, with nothing kept.
    old = await _row(async_session, vid)
    assert old.state == "cancelled" and "application" not in old.provider_refs
    assert await _kept(async_session, vid) == []
    # The new one keeps its own copy, by the same name.
    [kept] = await _kept(async_session, new["id"])
    assert kept.filename == "ada.jpg" and store.objects[kept.s3_key] == _JPEG
    assert len(store.objects) == 1


async def test_resubmit_with_changes(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    carrier.drafts.clear()
    clear = b"\xff\xd8clear"
    resp = await client.post(
        f"/verifications/{vid}/resubmit",
        data={"fields": json.dumps({"first_name": "Ada", "last_name": "King"})},
        files={"file.proof_of_identity": ("sharp.jpg", clear, "image/jpeg")},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["subject_name"] == "Ada King"
    draft = carrier.drafts[0]
    assert draft["fields"] == {"first_name": "Ada", "last_name": "King"}
    assert draft["documents"]["proof_of_identity"].file.data == clear
    [kept] = await _kept(async_session, resp.json()["id"])
    assert kept.filename == "sharp.jpg" and store.objects[kept.s3_key] == clear


async def test_resubmit_refuses_what_it_cannot_send_again(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    # Under review: nothing to fix yet.
    live = (
        await _create(
            client,
            key,
            fields=json.dumps({"first_name": "Grace", "last_name": "Hopper"}),
        )
    ).json()["id"]
    resp = await client.post(
        f"/verifications/{live}/resubmit",
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 409

    resp = await client.post(
        f"/verifications/{vid}/resubmit",
        data={"country_code": "FR"},
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "country_code"]
    # Another org's verification is not found.
    resp = await client.post(
        f"/verifications/{uuid.uuid4()}/resubmit",
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 404


async def test_a_refused_resubmit_leaves_the_rejected_one_as_it_was(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    carrier.problems = [Problem(field="last_name", message="Last name is wrong.")]
    resp = await client.post(
        f"/verifications/{vid}/resubmit",
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 422
    old = await _row(async_session, vid)
    assert old.state == "rejected" and "application" in old.provider_refs
    assert len(await _kept(async_session, vid)) == 1


async def test_only_one_resubmit_of_a_registration_succeeds(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    first = await client.post(
        f"/verifications/{vid}/resubmit",
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        f"/verifications/{vid}/resubmit",
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert second.status_code in (404, 409)


async def test_the_answers_never_reach_the_log(
    client, org_and_key, carrier, store, async_session, caplog
) -> None:
    _, _, key = org_and_key
    caplog.set_level("DEBUG")
    vid = await _rejected(client, key, carrier)
    await client.post(
        f"/verifications/{vid}/resubmit",
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    await _age(async_session, vid, 31)
    await sweep_verifications(async_session, _registry(carrier))
    assert "Lovelace" not in caplog.text and "ada.jpg" not in caplog.text


async def test_the_kept_quote_outlives_the_quote_purge(
    client, org_and_key, carrier, store, async_session
) -> None:
    """A carrier review takes days; the quote must still work for the
    resubmit and the console's form while the answers are kept."""
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    application = (
        await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    ).json()
    quote_id = uuid.UUID(application["quote_id"])
    await async_session.execute(
        update(NumberOffer)
        .where(NumberOffer.id == quote_id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(days=3))
    )
    await async_session.commit()
    await purge_expired_quotes()
    async_session.expire_all()
    assert await async_session.get(NumberOffer, quote_id) is not None

    resp = await client.post(
        f"/verifications/{vid}/resubmit",
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    # The old filing gave its answers to the new one, which names the quote.
    await purge_expired_quotes()
    async_session.expire_all()
    assert await async_session.get(NumberOffer, quote_id) is not None
    new = await _row(async_session, resp.json()["id"])
    await async_session.execute(
        update(CarrierVerification)
        .where(CarrierVerification.id == new.id)
        .values(provider_refs={})
    )
    await async_session.commit()
    await purge_expired_quotes()
    async_session.expire_all()
    assert await async_session.get(NumberOffer, quote_id) is None


async def test_a_superadmin_reject_keeps_the_answers(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    try:
        resp = await client.post(
            f"/admin/verifications/{vid}/reject",
            json={"reason": "ID unreadable"},
            headers=_auth(key),
        )
    finally:
        app.dependency_overrides.pop(require_superadmin, None)
    assert resp.status_code == 200 and resp.json()["state"] == "rejected"
    # Answers and files stay; the admin view shows no refs.
    assert "provider_refs" not in resp.json()
    row = await _row(async_session, vid)
    assert "application" in row.provider_refs
    listed = (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()
    assert listed["resubmittable"] is True


async def test_with_the_key_set_the_answers_are_stored_encrypted(
    client, org_and_key, carrier, store, async_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    row = await _row(async_session, vid)
    stored = row.provider_refs["application"]
    assert set(stored) == {"schema_version", "quote_id", "sealed"}
    assert "Lovelace" not in json.dumps(row.provider_refs)
    # Read back in the clear, and filed again from them.
    body = (
        await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    ).json()
    assert body["fields"]["last_name"] == "Lovelace"
    carrier.drafts.clear()
    resp = await client.post(
        f"/verifications/{vid}/resubmit",
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    assert carrier.drafts[0]["fields"]["last_name"] == "Lovelace"


async def test_answers_sealed_with_another_key_are_not_kept(
    client, org_and_key, carrier, store, async_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())
    resp = await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    assert resp.status_code == 404


async def test_an_approval_seen_in_the_list_deletes_the_files(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = (await _create(client, key)).json()["id"]
    carrier.remote_status = ProviderStatus(state="approved")
    verifications_routes._last_polled.clear()
    rows = (await client.get("/verifications", headers=_auth(key))).json()
    assert [r["state"] for r in rows] == ["approved"]
    assert "application" not in (await _row(async_session, vid)).provider_refs
    assert await _kept(async_session) == [] and store.objects == {}


async def test_a_new_document_type_does_not_reuse_the_kept_file(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    carrier.drafts.clear()
    resp = await client.post(
        f"/verifications/{vid}/resubmit",
        data={"documents": json.dumps({"proof_of_identity": {"option": "id_card"}})},
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    doc = carrier.drafts[0]["documents"]["proof_of_identity"]
    assert doc.option == "id_card" and doc.file is None


async def test_a_resubmit_ignores_the_deprecated_provider_part(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    resp = await client.post(
        f"/verifications/{vid}/resubmit",
        data={"provider": "fake"},
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text


async def test_answers_sealed_without_a_key_now_are_not_offered(
    client, org_and_key, carrier, store, async_session, monkeypatch
) -> None:
    """The key was removed: the list says so without decrypting each row (a
    changed key shows up as 404 on the read and the resubmit)."""
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    monkeypatch.setattr(settings, "hail_provider_secret_key", "")
    body = (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()
    assert body["resubmittable"] is False and body["kept_until"] is None


async def test_a_resubmit_moves_the_reused_files_to_the_new_filing(
    client, org_and_key, carrier, store, async_session
) -> None:
    """No copy, so a storage error during the resubmit cannot lose them."""
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    [before] = await _kept(async_session, vid)
    store.fail_put = True
    resp = await client.post(
        f"/verifications/{vid}/resubmit",
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    [after] = await _kept(async_session, resp.json()["id"])
    assert after.id == before.id and store.objects[after.s3_key]


async def test_a_resubmit_accepts_the_parts_as_filed(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    resp = await client.post(
        f"/verifications/{vid}/resubmit",
        data={"country_code": "gb", "subject_type": "person", "purpose": "number"},
        files={"_": (None, "1")},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text


async def test_the_saved_answers_name_the_credit_set_aside(
    client, org_and_key, carrier, store, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _rejected(client, key, carrier)
    body = (
        await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    ).json()
    offer = (
        await async_session.execute(
            select(NumberOffer.offer).where(
                NumberOffer.id == uuid.UUID(body["quote_id"])
            )
        )
    ).scalar_one()
    assert body["hold_cents"] == offer["setup_cents"] + offer["monthly_cents"]
