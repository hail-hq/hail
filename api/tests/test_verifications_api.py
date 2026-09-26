"""Tests for /verifications: collect details and documents, build the carrier
record through a provider plug-in, superadmin approval, and purchase.

A fake carrier stands in for Twilio so nothing here depends on it.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from hailhq.api.main import app
from hailhq.api.routes import verifications as verifications_routes
from hailhq.api.routes.verifications import (
    get_default_provider_name,
    get_verification_registry,
)
from hailhq.api.superadmin import require_superadmin
from hailhq.core import telephony_catalog
from hailhq.core.config import settings
from hailhq.core.models import AuditLog, CarrierVerification
from hailhq.core.providers.verification import (
    DocumentOption,
    DocumentSlot,
    DraftResult,
    FieldSpec,
    Problem,
    ProviderStatus,
    Requirements,
    UnsupportedSubjectType,
    VerificationProvider,
)
from sqlalchemy import select

from .conftest import insert_org_and_key


class FakeCarrier(VerificationProvider):
    name = "fake"

    def __init__(self) -> None:
        self.required = True
        self.problems: list[Problem] = []
        self.remote_status = ProviderStatus(state="pending")
        self.drafts: list[dict] = []
        self.submitted: list[dict] = []
        self.discarded: list[dict] = []
        self.check_problems: list[Problem] = []

    async def requirements(self, country_code, number_type, subject_type):
        return Requirements(
            provider=self.name,
            country_code=country_code,
            number_type=number_type,
            subject_type=subject_type,
            required=self.required,
            fields=(
                FieldSpec(name="first_name", label="First name"),
                FieldSpec(name="last_name", label="Last name"),
            ),
            documents=(
                DocumentSlot(
                    name="proof_of_identity",
                    label="Proof of identity",
                    options=(DocumentOption(key="passport", label="Passport"),),
                ),
            ),
            subject_types=("person",),
        )

    async def create_draft(self, **kwargs):
        self.drafts.append(kwargs)
        if self.problems:
            return DraftResult(refs={}, problems=self.problems)
        return DraftResult(refs={"bundle_sid": f"B{len(self.drafts)}"})

    async def check(self, refs):
        return self.check_problems

    async def submit(self, refs):
        self.submitted.append(refs)

    async def status(self, refs):
        return self.remote_status

    async def purchase_handle(self, refs):
        return {"bundle_sid": refs["bundle_sid"]}

    async def discard(self, refs):
        self.discarded.append(refs)


@pytest.fixture()
def carrier():
    fake = FakeCarrier()
    app.dependency_overrides[get_verification_registry] = lambda: (
        lambda name: fake if name == "fake" else None
    )
    verifications_routes._last_polled.clear()
    app.dependency_overrides[get_default_provider_name] = lambda: "fake"
    yield fake
    app.dependency_overrides.pop(get_verification_registry, None)
    app.dependency_overrides.pop(get_default_provider_name, None)
    app.dependency_overrides.pop(require_superadmin, None)


@pytest.fixture(autouse=True)
def pinned_catalog(tmp_path, monkeypatch):
    data = {
        "version": 2,
        "license": "CC-BY-4.0",
        "numbers": [
            {
                "country_code": "GB",
                "number_type": "mobile",
                "usd_per_month": "2.50",
                "voice": True,
                "sms": True,
                "mms": False,
            }
        ],
        "a2p_10dlc": [],
    }
    path = tmp_path / "telephony.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_PATH", str(path))
    telephony_catalog._load.cache_clear()
    yield
    telephony_catalog._load.cache_clear()


def _auth(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}


def _submission(**overrides) -> dict:
    data = {
        "provider": "fake",
        "country_code": "GB",
        "number_type": "mobile",
        "subject_type": "person",
        "fields": json.dumps({"first_name": "Ada", "last_name": "Lovelace"}),
        "documents": json.dumps({"proof_of_identity": {"option": "passport"}}),
    }
    data.update(overrides)
    return data


def _passport(
    name: str = "ada.jpg", body: bytes = b"\xff\xd8jpeg", ctype: str = "image/jpeg"
):
    return {"file.proof_of_identity": (name, body, ctype)}


async def _create(client, key, **overrides):
    return await client.post(
        "/verifications",
        data=_submission(**overrides),
        files=_passport(),
        headers=_auth(key),
    )


async def _unsent(client, key, carrier, async_session) -> str:
    """A verification still 'awaiting_review': creation submits at once, so
    tests of the retry/approve paths put the row back to the waiting state."""
    vid = (await _create(client, key)).json()["id"]
    await _set_state(async_session, vid, "awaiting_review", age_s=0)
    from sqlalchemy import update

    await async_session.execute(
        update(CarrierVerification)
        .where(CarrierVerification.id == uuid.UUID(vid))
        .values(submitted_at=None)
    )
    await async_session.commit()
    carrier.submitted.clear()
    return vid


# -- requirements ---------------------------------------------------------


async def test_requirements_returns_the_form(client, org_and_key, carrier) -> None:
    _, _, key = org_and_key
    resp = await client.get(
        "/verifications/requirements",
        params={"country_code": "GB", "number_type": "mobile", "provider": "fake"},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["required"] is True
    assert [f["name"] for f in body["fields"]] == ["first_name", "last_name"]
    assert body["documents"][0]["needs_input"] is True


async def test_requirements_unknown_provider_404(client, org_and_key, carrier) -> None:
    _, _, key = org_and_key
    resp = await client.get(
        "/verifications/requirements",
        params={"country_code": "GB", "number_type": "mobile", "provider": "nope"},
        headers=_auth(key),
    )
    assert resp.status_code == 404


async def test_requirements_rejects_unknown_number_type(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    resp = await client.get(
        "/verifications/requirements",
        params={"country_code": "GB", "number_type": "satellite", "provider": "fake"},
        headers=_auth(key),
    )
    assert resp.status_code == 422


async def test_requires_auth(client) -> None:
    assert (await client.get("/verifications")).status_code in (401, 403)


# -- create ---------------------------------------------------------------


async def test_create_stores_state_and_opaque_refs_only(
    client, org_and_key, carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    resp = await _create(client, key)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # No human gate: the draft went to the carrier during the request.
    assert body["state"] == "submitted" and body["submitted_at"]
    assert carrier.submitted == [{"bundle_sid": "B1"}]
    assert body["country_code"] == "GB" and body["number_type"] == "mobile"

    draft = carrier.drafts[0]
    assert draft["organization_id"] == str(org_id)
    # the carrier's notices go to the operator, never to the customer
    assert draft["contact_email"] == settings.hail_support_email
    assert draft["fields"] == {"first_name": "Ada", "last_name": "Lovelace"}
    file = draft["documents"]["proof_of_identity"].file
    assert file.data == b"\xff\xd8jpeg"
    assert file.filename == "upload"  # the customer's file name is dropped

    row = (await async_session.execute(select(CarrierVerification))).scalar_one()
    assert row.provider_refs == {"bundle_sid": "B1"}
    stored = " ".join(str(v) for v in vars(row).values())
    assert "Ada" not in stored and "Lovelace" not in stored and "jpeg" not in stored


async def test_create_returns_field_problems_and_stores_nothing(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    carrier.problems = [
        Problem(field="proof_of_identity.first_name", message="Name mismatch.")
    ]
    resp = await _create(client, key)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail[0]["loc"] == ["body", "proof_of_identity", "first_name"]
    assert detail[0]["msg"] == "Name mismatch."
    assert (await async_session.execute(select(CarrierVerification))).first() is None


async def test_create_when_no_verification_needed_is_422(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    carrier.required = False
    resp = await _create(client, key)
    assert resp.status_code == 422
    assert carrier.drafts == []


async def test_create_rejects_bad_file_type(client, org_and_key, carrier) -> None:
    _, _, key = org_and_key
    resp = await client.post(
        "/verifications",
        data=_submission(),
        files=_passport("a.exe", b"MZ", "application/x-msdownload"),
        headers=_auth(key),
    )
    assert resp.status_code == 422
    assert carrier.drafts == []


async def test_create_rejects_oversize_file(client, org_and_key, carrier) -> None:
    _, _, key = org_and_key
    resp = await client.post(
        "/verifications",
        data=_submission(),
        files=_passport(body=b"0" * (10 * 1024 * 1024 + 1), ctype="application/pdf"),
        headers=_auth(key),
    )
    assert resp.status_code == 422
    assert carrier.drafts == []


async def test_uploads_never_spool_to_disk(
    client, org_and_key, carrier, monkeypatch
) -> None:
    # Starlette spools file parts over 1 MB to a temp file. The parser we use
    # raises that limit above the request cap, so a scan stays in memory.
    import tempfile

    rollovers: list[int] = []
    original = tempfile.SpooledTemporaryFile.rollover

    def spy(self):
        rollovers.append(1)
        return original(self)

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", spy)
    _, _, key = org_and_key
    resp = await client.post(
        "/verifications",
        data=_submission(),
        files=_passport(body=b"0" * (3 * 1024 * 1024), ctype="application/pdf"),
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    assert rollovers == []


async def test_create_second_live_verification_is_409(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    assert (await _create(client, key)).status_code == 201
    resp = await _create(client, key)
    assert resp.status_code == 409
    assert len(carrier.drafts) == 1


# -- read / cancel --------------------------------------------------------


async def test_get_and_list_are_scoped_to_the_org(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = (await _create(client, key)).json()["id"]
    _, _, other_key = await insert_org_and_key(async_session)

    assert (
        await client.get(f"/verifications/{vid}", headers=_auth(key))
    ).status_code == 200
    assert (
        await client.get(f"/verifications/{vid}", headers=_auth(other_key))
    ).status_code == 404
    assert len((await client.get("/verifications", headers=_auth(key))).json()) == 1
    assert (await client.get("/verifications", headers=_auth(other_key))).json() == []


async def test_cancel_discards_at_the_carrier(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = (await _create(client, key)).json()["id"]
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200 and resp.json()["state"] == "cancelled"
    assert carrier.discarded == [{"bundle_sid": "B1"}]
    assert (
        await client.delete(f"/verifications/{vid}", headers=_auth(key))
    ).status_code == 409
    # a cancelled verification does not block a new one
    assert (await _create(client, key)).status_code == 201


async def test_rejected_verification_can_be_dismissed(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = (await _create(client, key)).json()["id"]
    await _set_state(async_session, vid, "rejected", age_s=0)
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200 and resp.json()["state"] == "cancelled"
    assert carrier.discarded == [{"bundle_sid": "B1"}]


# -- superadmin -----------------------------------------------------------


async def test_admin_routes_are_denied_by_default(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    for method, path in (
        ("GET", "/admin/verifications"),
        ("POST", f"/admin/verifications/{vid}/approve"),
    ):
        resp = await client.request(method, path, headers=_auth(key))
        assert resp.status_code == 403, (method, path)
    assert carrier.submitted == []


async def test_superadmin_approve_submits_to_the_carrier(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    admin_user_id = uuid.uuid4()
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=admin_user_id, api_key_id=None, superadmin=True
    )

    listed = await client.get("/admin/verifications", headers=_auth(key))
    assert [r["id"] for r in listed.json()] == [vid]

    resp = await client.post(f"/admin/verifications/{vid}/approve", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "submitted"
    assert carrier.submitted == [{"bundle_sid": "B1"}]
    again = await client.post(f"/admin/verifications/{vid}/approve", headers=_auth(key))
    assert again.status_code == 409

    audits = (
        (
            await async_session.execute(
                select(AuditLog).where(AuditLog.action == "verification.approve")
            )
        )
        .scalars()
        .all()
    )
    assert audits[0].actor_kind == "superadmin"
    assert audits[0].actor_user_id == admin_user_id


async def test_superadmin_approve_refuses_a_draft_the_carrier_now_rejects(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    carrier.check_problems = [Problem(field="", message="expired")]
    resp = await client.post(f"/admin/verifications/{vid}/approve", headers=_auth(key))
    assert resp.status_code == 409
    assert carrier.submitted == []


async def test_superadmin_reject_records_reason_and_discards(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    resp = await client.post(
        f"/admin/verifications/{vid}/reject",
        json={"reason": "ID unreadable"},
        headers=_auth(key),
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "rejected"
    assert resp.json()["rejection_reason"] == "ID unreadable"
    assert carrier.discarded == [{"bundle_sid": "B1"}]
    # rejected does not block starting again
    app.dependency_overrides.pop(require_superadmin)
    assert (await _create(client, key)).status_code == 201


async def test_status_is_pulled_from_the_carrier_after_submit(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    vid = (await _create(client, key)).json()["id"]
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    await client.post(f"/admin/verifications/{vid}/approve", headers=_auth(key))

    assert (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()[
        "state"
    ] == "submitted"
    carrier.remote_status = ProviderStatus(state="approved")
    got = (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()
    assert got["state"] == "approved" and got["approved_at"]


async def test_list_polls_the_carrier_at_most_once_a_minute(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    vid = (await _create(client, key)).json()["id"]
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    await client.post(f"/admin/verifications/{vid}/approve", headers=_auth(key))
    calls = []
    original = carrier.status

    async def counted(refs):
        calls.append(refs)
        return await original(refs)

    carrier.status = counted
    await client.get("/verifications", headers=_auth(key))
    await client.get("/verifications", headers=_auth(key))
    assert len(calls) == 1

    verifications_routes._last_polled.clear()
    carrier.remote_status = ProviderStatus(state="approved")
    listed = (await client.get("/verifications", headers=_auth(key))).json()
    assert listed[0]["state"] == "approved"


async def _set_state(async_session, vid: str, state: str, age_s: int) -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    await async_session.execute(
        update(CarrierVerification)
        .where(CarrierVerification.id == uuid.UUID(vid))
        .values(
            state=state,
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
        )
    )
    await async_session.commit()


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("pending", "submitted"),
        ("draft", "awaiting_review"),
        ("approved", "approved"),
        ("rejected", "rejected"),
    ],
)
async def test_stuck_submitting_row_settles_from_the_carrier(
    client, org_and_key, carrier, async_session, remote, expected
) -> None:
    # An approve cut off after the carrier call leaves 'submitting' behind.
    # Once it is old enough, a read asks the carrier what really happened.
    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    await _set_state(async_session, vid, "submitting", age_s=10 * 60)
    carrier.remote_status = ProviderStatus(state=remote)
    got = (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()
    assert got["state"] == expected
    audits = (
        (
            await async_session.execute(
                select(AuditLog).where(
                    AuditLog.action.in_(("verification.submit", "verification.approve"))
                )
            )
        )
        .scalars()
        .all()
    )
    # Creation's own submit wrote one row before _unsent reset the state.
    recovered = [a for a in audits if a.payload.get("recovered")]
    if expected == "awaiting_review":
        # The submit never went through: nothing to audit.
        assert recovered == [] and got["submitted_at"] is None
    else:
        # The interrupted submit never wrote its audit row; the recovery does.
        # Nobody approved this one (approved_by is empty), so it is an
        # automatic submit by the system, not a superadmin approve.
        assert got["submitted_at"]
        assert len(recovered) == 1 and recovered[0].action == "verification.submit"
        assert recovered[0].actor_kind == "system"
        assert recovered[0].actor_user_id is None


async def test_fresh_submitting_row_is_left_alone(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    await _set_state(async_session, vid, "submitting", age_s=5)
    carrier.remote_status = ProviderStatus(state="draft")
    got = (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()
    assert got["state"] == "submitting"
    listed = (await client.get("/verifications", headers=_auth(key))).json()
    assert listed[0]["state"] == "submitting"


async def test_requirements_unsupported_subject_lists_the_allowed_ones(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key

    async def only_business(country_code, number_type, subject_type):
        raise UnsupportedSubjectType("person is not accepted", allowed=("business",))

    carrier.requirements = only_business
    resp = await client.get(
        "/verifications/requirements",
        params={"provider": "fake", "country_code": "DE", "number_type": "local"},
        headers=_auth(key),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["ctx"] == {"subject_types": ["business"]}


async def test_create_with_malformed_documents_is_422(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    bad = json.dumps({"proof_of_identity": {"fields": [1]}})
    resp = await _create(client, key, documents=bad)
    assert resp.status_code == 422
    assert carrier.drafts == []


async def test_approve_returns_502_when_the_carrier_fails(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )

    async def boom(refs):
        raise RuntimeError("twilio down")

    original = carrier.submit
    carrier.submit = boom
    resp = await client.post(f"/admin/verifications/{vid}/approve", headers=_auth(key))
    assert resp.status_code == 502
    carrier.submit = original
    retry = await client.post(f"/admin/verifications/{vid}/approve", headers=_auth(key))
    assert retry.status_code == 200


async def test_approve_marks_submitting_before_calling_the_carrier(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    seen = {}

    async def submit_and_approve_again(refs):
        # The state is already saved, so a second approve is refused.
        seen["again"] = await client.post(
            f"/admin/verifications/{vid}/approve", headers=_auth(key)
        )

    carrier.submit = submit_and_approve_again
    resp = await client.post(f"/admin/verifications/{vid}/approve", headers=_auth(key))
    assert resp.status_code == 200
    assert resp.json()["state"] == "submitted"
    assert seen["again"].status_code == 409
    assert "submitting" in seen["again"].json()["detail"]


# -- purchase -------------------------------------------------------------


async def _approve_and_pull(client, key, carrier) -> str:
    vid = (await _create(client, key)).json()["id"]
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    await client.post(f"/admin/verifications/{vid}/approve", headers=_auth(key))
    app.dependency_overrides.pop(require_superadmin)
    return vid


async def _fund(async_session, org_id) -> None:
    from hailhq.core.models import AccountCredit

    async_session.add(
        AccountCredit(
            organization_id=org_id,
            kind="credit",
            channel="credit",
            amount_cents=1000,
            qty=1,
            ref=f"test-fund:{uuid.uuid4()}",
            source="test",
        )
    )
    await async_session.commit()


async def _quote(async_session, org_id, verification_id):
    from datetime import datetime, timedelta, timezone

    from hailhq.core.models import NumberOffer
    from hailhq.core.number_offers import CarrierOffer

    offer = CarrierOffer(
        provider="twilio",
        e164="+447700900001",
        country_code="GB",
        number_type="mobile",
        capabilities=["voice", "sms"],
        monthly_cents=250,
        setup_cents=0,
        readiness="ready",
        verification_id=verification_id,
    )
    quote = NumberOffer(
        organization_id=org_id,
        offer=offer.model_dump(mode="json"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    async_session.add(quote)
    await async_session.commit()
    return quote, offer


async def test_purchase_passes_the_quoted_verification_to_the_carrier(
    client, org_and_key, async_session, monkeypatch
) -> None:
    """Readiness comes from live carrier discovery: ``twilio_offers`` looks up
    the org's approved bundle (``hail-<org>``, the name the verification
    plug-in gives it) and the quote carries its id into the purchase."""
    from unittest.mock import AsyncMock

    org_id, _, key = org_and_key
    await _fund(async_session, org_id)
    purchase = AsyncMock(return_value="PN_gb_1")
    monkeypatch.setattr("hailhq.api.number_orders.purchase_ordered_number", purchase)
    quote, offer = await _quote(async_session, org_id, "BU_approved")
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    resp = await client.post(
        "/numbers",
        json={"country_code": "GB", "quote_id": str(quote.id)},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    purchase.assert_awaited_once()
    assert purchase.await_args.args[2] == "BU_approved"


async def test_purchase_without_verification_passes_none(
    client, org_and_key, async_session, monkeypatch
) -> None:
    from unittest.mock import AsyncMock

    org_id, _, key = org_and_key
    await _fund(async_session, org_id)
    purchase = AsyncMock(return_value="PN_gb_1")
    monkeypatch.setattr("hailhq.api.number_orders.purchase_ordered_number", purchase)
    quote, offer = await _quote(async_session, org_id, None)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    resp = await client.post(
        "/numbers",
        json={"country_code": "GB", "quote_id": str(quote.id)},
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    assert purchase.await_args.args[2] is None


async def test_requirements_and_create_default_to_the_configured_carrier(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    resp = await client.get(
        "/verifications/requirements",
        params={"country_code": "GB", "number_type": "mobile"},
        headers=_auth(key),
    )
    assert resp.status_code == 200 and resp.json()["provider"] == "fake"
    resp = await client.post(
        "/verifications",
        data=_submission(provider=""),
        files=_passport(),
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["provider"] == "fake"


async def test_no_configured_carrier_is_404(client, org_and_key, carrier) -> None:
    _, _, key = org_and_key
    app.dependency_overrides[get_default_provider_name] = lambda: None
    resp = await client.get(
        "/verifications/requirements",
        params={"country_code": "GB", "number_type": "mobile"},
        headers=_auth(key),
    )
    assert resp.status_code == 404


async def test_create_keeps_waiting_when_the_carrier_refuses_the_submit(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key

    async def boom(refs):
        raise RuntimeError("twilio down")

    carrier.submit = boom
    resp = await _create(client, key)
    assert resp.status_code == 201, resp.text
    assert resp.json()["state"] == "awaiting_review"
    assert resp.json()["submitted_at"] is None


async def test_sweeper_submits_waiting_drafts_and_pulls_decisions(
    client, org_and_key, carrier, async_session
) -> None:
    from hailhq.api.routes.verifications import sweep_verifications

    _, _, key = org_and_key
    waiting = await _unsent(client, key, carrier, async_session)
    # Too fresh: creation may still be running; the sweeper leaves it alone.
    counts = await sweep_verifications(
        async_session, lambda name: carrier if name == "fake" else None
    )
    assert counts == {"submitted": 0, "refreshed": 0} and carrier.submitted == []
    await _set_state(async_session, waiting, "awaiting_review", age_s=3 * 60)
    carrier.remote_status = ProviderStatus(state="approved")
    counts = await sweep_verifications(
        async_session, lambda name: carrier if name == "fake" else None
    )
    # Submitted this pass, then polled: the carrier already approved it.
    assert counts["submitted"] == 1 and carrier.submitted == [{"bundle_sid": "B1"}]
    got = (await client.get(f"/verifications/{waiting}", headers=_auth(key))).json()
    assert got["state"] == "approved"
    audits = (
        (
            await async_session.execute(
                select(AuditLog).where(AuditLog.action == "verification.submit")
            )
        )
        .scalars()
        .all()
    )
    # One row from creation's own submit, one from the sweeper's retry.
    assert len(audits) == 2 and all(a.payload["approved_by"] is None for a in audits)
