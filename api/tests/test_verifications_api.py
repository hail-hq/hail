"""Tests for /verifications: collect details and documents, build the carrier
record through a provider plug-in, superadmin approval, and purchase.

A fake carrier stands in for Twilio so nothing here depends on it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from hailhq.api.main import app
from hailhq.api.routes import verifications as verifications_routes
from hailhq.api.routes.verifications import (
    get_default_sms_provider_name,
    get_verification_registry,
)
from hailhq.api.superadmin import require_superadmin
from hailhq.core import sms_registration_fees, telephony_catalog
from hailhq.core.config import SELF_HOSTED_ORG_ID, settings
from hailhq.core.models import (
    AccountCredit,
    AuditLog,
    CarrierVerification,
    PhoneNumber,
)
from hailhq.core.providers.verification import (
    DocumentOption,
    DocumentSlot,
    DraftResult,
    FieldSpec,
    Problem,
    ProviderStatus,
    Requirements,
    SmsRegistrationProvider,
    SubmitRefused,
    UnsupportedSubjectType,
    VerificationProvider,
)
from hailhq.core.providers.verification.base import (
    BRAND_REJECTED,
    NOT_ACCEPTED,
    VALUE_NOT_ACCEPTED,
)
from hailhq.core.providers.verification.forms import display_name
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

    async def check(self, refs, subject_type):
        return self.check_problems

    async def submit(self, refs):
        self.submitted.append(refs)

    async def status(self, refs):
        return self.remote_status

    async def purchase_handle(self, refs):
        return {"verification_id": refs["bundle_sid"], "address_id": None}

    async def discard(self, refs):
        self.discarded.append(refs)

    def subject_name(self, requirements, fields, address, documents):
        return display_name(fields.get("first_name"), fields.get("last_name"))


class FakeSmsCarrier(SmsRegistrationProvider):
    name = "fake"

    def __init__(self) -> None:
        self.remote_status = ProviderStatus(state="pending")
        self.drafts: list[dict] = []
        self.submitted: list[dict] = []
        self.discarded: list[dict] = []
        self.attached: list[tuple[dict, str, str]] = []
        self.attach_error: Exception | None = None
        self.problems: list[Problem] = []
        # Set to "campaign_sid" (Twilio) or "campaign_id" (Telnyx): status()
        # then creates the campaign once, writes its id into refs in place
        # and reports pending, like the real plug-ins.
        self.campaign_key: str | None = None
        self.campaigns_created = 0
        # Set both to hold the first status() call: it sets ``entered`` and
        # waits for ``release``.
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None
        # Seconds create_draft waits, so two filings overlap.
        self.draft_delay = 0.0
        self.status_calls = 0
        # How a person confirms: "code" (type a code into Hail) or "reply".
        self.confirm_kind = "code"
        self.confirm_problems: list[Problem] = []
        self.confirmed: list[tuple[dict, str]] = []
        self.detached: list[str] = []
        # True: detach_number says the number left its messaging service.
        self.detach_leaves_service = False

    async def requirements(self, country_code, number_type, subject_type):
        return Requirements(
            provider=self.name,
            country_code=country_code,
            number_type=number_type,
            subject_type=subject_type,
            required=True,
            fields=(
                FieldSpec(name="legal_name", label="Legal name"),
                FieldSpec(name="campaign_description", label="What you send"),
            ),
            address_required=True,
            subject_types=("person", "business"),
        )

    async def create_draft(self, *, messaging_service_sid=None, **kwargs):
        self.drafts.append({**kwargs, "messaging_service_sid": messaging_service_sid})
        if self.draft_delay:
            await asyncio.sleep(self.draft_delay)
        if self.problems:
            return DraftResult(refs={}, problems=self.problems)
        return DraftResult(
            refs={
                "brand_sid": f"BN{len(self.drafts)}",
                "subject_type": kwargs["requirements"].subject_type,
            }
        )

    async def check(self, refs, subject_type):
        return []

    async def submit(self, refs):
        self.submitted.append(refs)

    def confirmation(self, refs):
        if refs.get("subject_type") == "person" and not refs.get("identity_verified"):
            return self.confirm_kind
        return None

    async def confirm(self, refs, code):
        if self.confirm_kind != "code":
            raise NotImplementedError
        self.confirmed.append((dict(refs), code))
        return self.confirm_problems

    async def status(self, refs):
        self.status_calls += 1
        if self.release is not None and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
        if self.campaign_key and self.campaign_key not in refs:
            self.campaigns_created += 1
            refs[self.campaign_key] = "QE1"
            return ProviderStatus(state="pending")
        return self.remote_status

    async def purchase_handle(self, refs):
        return {}

    async def discard(self, refs):
        self.discarded.append(refs)

    async def attach_number(self, refs, *, provider_resource_id, e164):
        if self.attach_error:
            raise self.attach_error
        self.attached.append((refs, provider_resource_id, e164))

    async def detach_number(self, refs, *, provider_resource_id, e164):
        self.detached.append(provider_resource_id)
        return self.detach_leaves_service


# quote_id of a GB mobile offer from the "fake" carrier, for the test org.
# A number verification needs one; `carrier` sets it.
_QUOTE_ID: str | None = None


@pytest.fixture()
async def carrier(org_and_key, async_session):
    global _QUOTE_ID
    fake = FakeCarrier()
    app.dependency_overrides[get_verification_registry] = lambda: (
        lambda name, purpose="number": (
            fake if name == "fake" and purpose == "number" else None
        )
    )
    verifications_routes._last_polled.clear()
    _QUOTE_ID = await _raw_quote(async_session, org_and_key[0], "fake")
    yield fake
    _QUOTE_ID = None
    app.dependency_overrides.pop(get_verification_registry, None)
    app.dependency_overrides.pop(require_superadmin, None)


def _gb_mobile(**params) -> dict:
    """Query for the GB mobile number form, with the test quote."""
    return {
        "country_code": "GB",
        "number_type": "mobile",
        "quote_id": _QUOTE_ID,
        "subject_type": "person",
        **params,
    }


@pytest.fixture()
def sms_carrier():
    fake = FakeSmsCarrier()
    number_fake = FakeCarrier()

    def registry(name, purpose="number"):
        if name != "fake":
            return None
        return fake if purpose == "sms" else number_fake

    app.dependency_overrides[get_verification_registry] = lambda: registry
    verifications_routes._last_polled.clear()
    app.dependency_overrides[get_default_sms_provider_name] = lambda: "fake"
    yield fake
    app.dependency_overrides.pop(get_verification_registry, None)
    app.dependency_overrides.pop(get_default_sms_provider_name, None)


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
    (tmp_path / "twilio.json").write_text(json.dumps(data))
    (tmp_path / "telnyx.json").write_text(json.dumps({**data, "numbers": []}))
    (tmp_path / "sms-registration.json").write_text(
        json.dumps(
            {
                "version": 1,
                "license": "CC-BY-4.0",
                "providers": [
                    {
                        "provider": "fake",
                        "last_verified": "2026-10-13",
                        "source_url": "",
                        "person": {
                            "brand_usd": "4.50",
                            "campaign_vetting_usd": "15.00",
                            "campaign_monthly_usd": "2.00",
                        },
                        "business": {
                            "brand_usd": "46.00",
                            "campaign_vetting_usd": "15.00",
                            "campaign_monthly_usd": "10.00",
                        },
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_DIR", str(tmp_path))
    telephony_catalog._load.cache_clear()
    sms_registration_fees._load.cache_clear()
    yield
    telephony_catalog._load.cache_clear()
    sms_registration_fees._load.cache_clear()


def _auth(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}


def _submission(**overrides) -> dict:
    data = {
        "provider": "fake",
        "quote_id": _QUOTE_ID or "",
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
        params=_gb_mobile(provider="fake"),
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["required"] is True
    assert [f["name"] for f in body["fields"]] == ["first_name", "last_name"]
    assert body["documents"][0]["needs_input"] is True


async def test_requirements_ignore_the_provider_param(
    client, org_and_key, carrier
) -> None:
    """``provider`` is deprecated: still accepted, never used, never echoed."""
    _, _, key = org_and_key
    resp = await client.get(
        "/verifications/requirements",
        params=_gb_mobile(provider="nope"),
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    assert "provider" not in resp.json()


async def _row_provider(async_session, vid: str) -> str:
    """The carrier stored on a verification row: the API no longer shows it."""
    return (
        await async_session.execute(
            select(CarrierVerification.provider)
            .where(CarrierVerification.id == uuid.UUID(vid))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_requirements_rejects_unknown_number_type(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    resp = await client.get(
        "/verifications/requirements",
        params={
            "subject_type": "person",
            "country_code": "GB",
            "number_type": "satellite",
            "provider": "fake",
        },
        headers=_auth(key),
    )
    assert resp.status_code == 422


async def test_requires_auth(client) -> None:
    assert (await client.get("/verifications")).status_code in (401, 403)


# -- create ---------------------------------------------------------------


async def test_create_stores_state_refs_and_the_name_only(
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
    # The name is the one personal value kept; nothing else from the form.
    assert row.subject_name == "Ada Lovelace"
    stored = " ".join(str(v) for k, v in vars(row).items() if k != "subject_name")
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


async def test_carrier_text_is_neither_returned_nor_logged(
    client, org_and_key, carrier, async_session, caplog
) -> None:
    """The 422 keeps the field and Hail's wording. The log names the carrier,
    org and field, never the carrier's text (it can quote personal data)."""
    org_id, _, key = org_and_key
    carrier.problems = [
        Problem(
            field="proof_of_identity.first_name",
            message=VALUE_NOT_ACCEPTED,
            raw="CarrierCo: first name does not match document",
        )
    ]
    with caplog.at_level("WARNING", logger="hailhq.api.routes.verifications"):
        resp = await _create(client, key)
    assert resp.status_code == 422
    assert resp.json()["detail"] == [
        {
            "loc": ["body", "proof_of_identity", "first_name"],
            "msg": VALUE_NOT_ACCEPTED,
            "type": "verification_problem",
        }
    ]
    assert "CarrierCo" not in resp.text
    messages = [r.getMessage() for r in caplog.records]
    assert not any("CarrierCo" in m for m in messages)
    logged = [m for m in messages if "refused verification details" in m]
    assert len(logged) == 1
    assert str(org_id) in logged[0] and "fake" in logged[0]
    assert "proof_of_identity.first_name" in logged[0]


async def test_public_verification_has_no_provider_admin_does(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = (await _create(client, key)).json()["id"]
    got = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert "provider" not in got.json()
    listed = await client.get("/verifications", headers=_auth(key))
    assert all("provider" not in r for r in listed.json())
    await _set_state(async_session, vid, "awaiting_review", age_s=0)
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    resp = await client.get("/admin/verifications", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert [r["provider"] for r in resp.json()] == ["fake"]


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


def _named(first: str, last: str) -> dict:
    return {"fields": json.dumps({"first_name": first, "last_name": last})}


async def test_create_stores_and_returns_the_name_without_logging_it(
    client, org_and_key, carrier, async_session, caplog
) -> None:
    _, _, key = org_and_key
    with caplog.at_level("DEBUG"):
        resp = await _create(client, key, **_named("Zelda", "Quixote"))
        again = await _create(client, key, **_named("zelda", "QUIXOTE"))
        listed = await client.get("/verifications", headers=_auth(key))
    assert resp.status_code == 201, resp.text
    assert resp.json()["subject_name"] == "Zelda Quixote"
    row = await async_session.get(CarrierVerification, uuid.UUID(resp.json()["id"]))
    assert row.subject_name == "Zelda Quixote"
    assert listed.json()[0]["subject_name"] == "Zelda Quixote"
    # Same kind, same name (any case): 409, named back to the customer.
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == (
        "You already have a verification for zelda QUIXOTE as a person "
        "for this number type."
    )
    logged = " ".join(
        f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records
    ).lower()
    assert "zelda" not in logged and "quixote" not in logged


async def test_several_people_of_one_kind_may_be_live(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    first = await _create(client, key, **_named("Ada", "Lovelace"))
    second = await _create(client, key, **_named("Grace", "Hopper"))
    assert (first.status_code, second.status_code) == (201, 201), second.text
    names = {
        r["subject_name"]
        for r in (await client.get("/verifications", headers=_auth(key))).json()
    }
    assert names == {"Ada Lovelace", "Grace Hopper"}
    # The same name as a company is another kind: allowed.
    assert (
        await _create(client, key, subject_type="business", **_named("Ada", "Lovelace"))
    ).status_code == 201


async def test_a_second_nameless_verification_of_one_kind_is_409(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    first = await _create(client, key, **_named("", ""))
    assert first.status_code == 201, first.text
    assert first.json()["subject_name"] is None
    again = await _create(client, key, **_named("", ""))
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == (
        "You already have a verification as a person for this number type "
        "with no name on it."
    )
    assert len(carrier.drafts) == 1
    # A named one of the same kind, and a nameless company, are still fine.
    assert (await _create(client, key)).status_code == 201
    assert (
        await _create(client, key, subject_type="business", **_named("", ""))
    ).status_code == 201
    # Once the nameless one is rejected, a new nameless one may be filed.
    await _set_state(async_session, first.json()["id"], "rejected", age_s=0)
    assert (await _create(client, key, **_named("", ""))).status_code == 201


async def test_a_duplicate_is_refused_before_files_are_read_or_filed(
    client, org_and_key, carrier, monkeypatch
) -> None:
    """The 409 comes before the uploads are read and before any filing."""
    _, _, key = org_and_key
    assert (await _create(client, key)).status_code == 201
    assert len(carrier.drafts) == 1

    reads = 0

    async def counting_read(upload):
        nonlocal reads
        reads += 1
        raise AssertionError("uploads must not be read before the 409")

    read_file = verifications_routes._read_file
    monkeypatch.setattr(verifications_routes, "_read_file", counting_read)
    resp = await _create(client, key)
    assert resp.status_code == 409, resp.text
    assert reads == 0
    assert len(carrier.drafts) == 1  # no second filing
    assert carrier.discarded == []
    # Nameless duplicate: the same. (Not monkeypatch.undo(): that would also
    # undo the fixtures' patches, such as the fake carrier.)
    monkeypatch.setattr(verifications_routes, "_read_file", read_file)
    assert (await _create(client, key, **_named("", ""))).status_code == 201
    monkeypatch.setattr(verifications_routes, "_read_file", counting_read)
    resp = await _create(client, key, **_named("", ""))
    assert resp.status_code == 409, resp.text
    assert reads == 0
    assert len(carrier.drafts) == 2


async def test_the_name_on_a_document_counts(
    client, org_and_key, carrier, monkeypatch
) -> None:
    """A plug-in may read the name from a document's fields (not its file):
    two people named only on their documents are two named rows, not a
    nameless 409."""
    _, _, key = org_and_key

    def from_documents(requirements, fields, address, documents):
        return next(
            (d.fields["full_name"] for d in documents.values() if d.fields),
            None,
        )

    monkeypatch.setattr(carrier, "subject_name", from_documents)

    def named_on_document(name: str) -> dict:
        return {
            **_named("", ""),
            "documents": json.dumps(
                {
                    "proof_of_identity": {
                        "option": "passport",
                        "fields": {"full_name": name},
                    }
                }
            ),
        }

    first = await _create(client, key, **named_on_document("Ada Lovelace"))
    assert first.status_code == 201, first.text
    assert first.json()["subject_name"] == "Ada Lovelace"
    second = await _create(client, key, **named_on_document("Grace Hopper"))
    assert second.status_code == 201, second.text
    assert second.json()["subject_name"] == "Grace Hopper"


async def test_same_name_may_return_after_a_cancel(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = (await _create(client, key)).json()["id"]
    await _set_state(async_session, vid, "rejected", age_s=0)
    assert (await _create(client, key)).status_code == 201


async def test_the_indexes_refuse_a_duplicate_name_or_a_second_nameless_one(
    org_and_key, async_session
) -> None:
    """The database refuses what the route's check refuses, even in a race."""
    from sqlalchemy.exc import IntegrityError

    org_id, _, _ = org_and_key

    def row(name, **over):
        return CarrierVerification(
            organization_id=org_id,
            provider="fake",
            country_code="GB",
            number_type="mobile",
            subject_type=over.pop("subject_type", "person"),
            subject_name=name,
            state=over.pop("state", "approved"),
            provider_refs={},
            requirements_version="1",
        )

    async_session.add_all(
        [
            row("Ada Lovelace"),
            row("Grace Hopper"),
            row("Ada Lovelace", subject_type="business"),
            row("Ada Lovelace", state="cancelled"),
            row(None),
            row(None, subject_type="business"),
            row(None, state="rejected"),
        ]
    )
    await async_session.commit()
    async_session.add(row("ADA LOVELACE"))
    with pytest.raises(IntegrityError):
        await async_session.commit()
    await async_session.rollback()
    # A second live nameless row of one kind: refused too.
    async_session.add(row(None, state="submitted"))
    with pytest.raises(IntegrityError):
        await async_session.commit()
    await async_session.rollback()


async def test_approved_handles_lists_every_approved_row(
    org_and_key, carrier, async_session
) -> None:
    org_id, _, _ = org_and_key
    rows = [
        CarrierVerification(
            organization_id=org_id,
            provider="fake",
            country_code="GB",
            number_type="mobile",
            subject_type="person",
            subject_name=name,
            state="approved",
            provider_refs={"bundle_sid": sid},
            requirements_version="1",
        )
        for name, sid in (("Ada Lovelace", "B1"), ("Grace Hopper", "B2"))
    ]
    async_session.add_all(rows)
    await async_session.commit()
    handles = await verifications_routes.approved_handles(
        async_session,
        lambda name, purpose="number": carrier,
        org_id,
        "GB",
        "mobile",
        ["fake"],
    )
    got = {
        (a.row_id, a.subject_type, a.subject_name, a.verification_id)
        for a in handles["fake"]
    }
    assert got == {
        (rows[0].id, "person", "Ada Lovelace", "B1"),
        (rows[1].id, "person", "Grace Hopper", "B2"),
    }


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
    vid = await _unsent(client, key, carrier, async_session)
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200 and resp.json()["state"] == "cancelled"
    assert carrier.discarded == [{"bundle_sid": "B1"}]
    assert (
        await client.delete(f"/verifications/{vid}", headers=_auth(key))
    ).status_code == 409
    # a cancelled verification does not block a new one
    assert (await _create(client, key)).status_code == 201


async def test_cancel_is_refused_while_under_review(
    client, org_and_key, carrier, async_session
) -> None:
    """The carrier keeps a draft it is reviewing, so cancelling now would
    leave the customer's documents there with nothing pointing at them."""
    _, _, key = org_and_key
    vid = (await _create(client, key)).json()["id"]
    got = (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()
    assert got["state"] == "submitted"
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 409 and "under review" in resp.json()["detail"]
    assert carrier.discarded == []
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    assert row.state == "submitted" and row.provider_refs == {"bundle_sid": "B1"}


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
        params=_gb_mobile(provider="fake"),
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


async def _approved(
    async_session, org_id, subject_type, bundle, name=None, state="approved"
) -> uuid.UUID:
    """An approved GB mobile number verification with a Twilio-shaped ref."""
    row = CarrierVerification(
        organization_id=org_id,
        provider="twilio",
        country_code="GB",
        number_type="mobile",
        subject_type=subject_type,
        subject_name=name,
        state=state,
        provider_refs={"bundle_sid": bundle},
        requirements_version="v1",
    )
    async_session.add(row)
    await async_session.commit()
    return row.id


async def _quote(async_session, org_id, approvals, subject_types=None):
    """A ready GB mobile quote. ``approvals``: (row id, subject type,
    bundle[, name]) per approval the carrier accepted at quote time."""
    from datetime import datetime, timedelta, timezone

    from hailhq.core.carrier_offer import Approval
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
        subject_types=(
            subject_types
            if subject_types is not None
            else (["business", "person"] if approvals else [])
        ),
        approvals=[
            Approval(
                row_id=a[0],
                subject_type=a[1],
                verification_id=a[2],
                subject_name=a[3] if len(a) > 3 else None,
            )
            for a in approvals
        ],
    )
    quote = NumberOffer(
        organization_id=org_id,
        offer=offer.model_dump(mode="json"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    async_session.add(quote)
    await async_session.commit()
    return quote, offer


async def _buy(client, key, async_session, org_id, approvals, monkeypatch, **body):
    """POST /numbers for a fresh quote; returns (response, purchase mock)."""
    from unittest.mock import AsyncMock

    await _fund(async_session, org_id)
    purchase = AsyncMock(return_value="PN_gb_1")
    monkeypatch.setattr(
        "hailhq.core.providers.voice.twilio.purchase_ordered_number", purchase
    )
    subject_types = body.pop("subject_types", None)
    quote, offer = await _quote(async_session, org_id, approvals, subject_types)
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer], [])),
    )
    resp = await client.post(
        "/numbers",
        json={"country_code": "GB", "quote_id": str(quote.id), **body},
        headers=_auth(key),
    )
    return resp, purchase


async def test_purchase_passes_the_quoted_verification_to_the_carrier(
    client, org_and_key, async_session, monkeypatch
) -> None:
    """Hail's approved row decides the purchase handle; the carrier only
    confirmed it at quote time. One approved kind: subject_type may be left
    out, and the number remembers which verification it used."""
    from hailhq.core.models import PhoneNumber

    org_id, _, key = org_and_key
    row_id = await _approved(
        async_session, org_id, "person", "BU_person", "Ada Lovelace"
    )
    resp, purchase = await _buy(
        client,
        key,
        async_session,
        org_id,
        [(row_id, "person", "BU_person", "Ada Lovelace")],
        monkeypatch,
    )
    assert resp.status_code == 201, resp.text
    purchase.assert_awaited_once()
    assert purchase.await_args.args[2] == "BU_person"
    body = resp.json()
    assert body["subject_type"] == "person"
    assert body["verification_id"] == str(row_id)
    assert body["subject_name"] == "Ada Lovelace"
    number = await async_session.get(PhoneNumber, uuid.UUID(resp.json()["id"]))
    await async_session.refresh(number)
    stored = number.provisioning_metadata["offer"]
    assert stored["verification_row_id"] == str(row_id)
    assert stored["subject_name"] == "Ada Lovelace"
    listed = await client.get("/numbers", headers=_auth(key))
    item = listed.json()["items"][0]
    assert (item["subject_type"], item["verification_id"], item["subject_name"]) == (
        "person",
        str(row_id),
        "Ada Lovelace",
    )
    got = await client.get(f"/numbers/{body['id']}", headers=_auth(key))
    assert got.json()["subject_name"] == "Ada Lovelace"


_SEVERAL = "You have more than one verification for this number. Pass verification_id."


async def test_purchase_with_two_approved_kinds_needs_a_pick(
    client, org_and_key, async_session, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    person = await _approved(async_session, org_id, "person", "BU_person")
    business = await _approved(async_session, org_id, "business", "BU_business")
    approvals = [
        (person, "person", "BU_person"),
        (business, "business", "BU_business"),
    ]
    resp, purchase = await _buy(
        client, key, async_session, org_id, approvals, monkeypatch
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "verification_id"]
    assert resp.json()["detail"][0]["msg"] == _SEVERAL
    purchase.assert_not_awaited()

    resp, purchase = await _buy(
        client,
        key,
        async_session,
        org_id,
        approvals,
        monkeypatch,
        subject_type="business",
    )
    assert resp.status_code == 201, resp.text
    assert purchase.await_args.args[2] == "BU_business"
    assert resp.json()["subject_type"] == "business"
    assert resp.json()["verification_id"] == str(business)


async def test_purchase_picks_one_of_several_people_by_verification_id(
    client, org_and_key, async_session, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    ada = await _approved(async_session, org_id, "person", "BU_ada", "Ada Lovelace")
    grace = await _approved(async_session, org_id, "person", "BU_grace", "Grace Hopper")
    approvals = [
        (ada, "person", "BU_ada", "Ada Lovelace"),
        (grace, "person", "BU_grace", "Grace Hopper"),
    ]
    resp, purchase = await _buy(
        client, key, async_session, org_id, approvals, monkeypatch
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0] == {
        **resp.json()["detail"][0],
        "loc": ["body", "verification_id"],
        "msg": _SEVERAL,
    }
    # subject_type alone does not pick between two people.
    resp, purchase = await _buy(
        client,
        key,
        async_session,
        org_id,
        approvals,
        monkeypatch,
        subject_type="person",
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "verification_id"]
    resp, purchase = await _buy(
        client,
        key,
        async_session,
        org_id,
        approvals,
        monkeypatch,
        verification_id=str(grace),
        subject_type="person",
    )
    assert resp.status_code == 201, resp.text
    assert purchase.await_args.args[2] == "BU_grace"
    assert resp.json()["subject_name"] == "Grace Hopper"
    assert resp.json()["verification_id"] == str(grace)


async def test_purchase_refuses_a_verification_id_it_cannot_use(
    client, org_and_key, async_session, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    other_org, _, _ = await insert_org_and_key(async_session)
    ada = await _approved(async_session, org_id, "person", "BU_ada", "Ada")
    pending = await _approved(
        async_session, org_id, "person", "BU_p", "Pat", state="submitted"
    )
    # Approved, but the carrier did not accept it for this offer.
    stale = await _approved(async_session, org_id, "person", "BU_s", "Sam")
    theirs = await _approved(async_session, other_org, "person", "BU_t", "Tom")
    approvals = [(ada, "person", "BU_ada", "Ada")]
    cases = {
        str(uuid.uuid4()): "This verification is not one of yours for this number.",
        str(theirs): "This verification is not one of yours for this number.",
        str(pending): "This verification is not approved yet.",
        str(stale): "This verification cannot be used for this number.",
    }
    for vid, msg in cases.items():
        resp, purchase = await _buy(
            client,
            key,
            async_session,
            org_id,
            approvals,
            monkeypatch,
            verification_id=vid,
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"][0]
        assert detail["loc"] == ["body", "verification_id"]
        assert detail["msg"].startswith(msg), detail
        purchase.assert_not_awaited()
    # It must match subject_type when both are passed.
    resp, purchase = await _buy(
        client,
        key,
        async_session,
        org_id,
        approvals + [(uuid.uuid4(), "business", "BU_b")],
        monkeypatch,
        verification_id=str(ada),
        subject_type="business",
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0] == {
        **resp.json()["detail"][0],
        "loc": ["body", "verification_id"],
        "msg": "This verification is not as a company.",
    }


@pytest.mark.parametrize(
    ("asked", "msg"),
    [("person", "Verify as a person first"), ("business", "Verify as a company first")],
)
async def test_purchase_with_a_kind_not_approved_is_422(
    client, org_and_key, async_session, monkeypatch, asked, msg
) -> None:
    org_id, _, key = org_and_key
    other = "business" if asked == "person" else "person"
    row_id = await _approved(async_session, org_id, other, "BU_other")
    resp, purchase = await _buy(
        client,
        key,
        async_session,
        org_id,
        [(row_id, other, "BU_other")],
        monkeypatch,
        subject_type=asked,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "subject_type"]
    assert resp.json()["detail"][0]["msg"] == msg
    purchase.assert_not_awaited()


async def test_purchase_with_a_kind_the_number_does_not_take_is_422(
    client, org_and_key, async_session, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    row_id = await _approved(async_session, org_id, "business", "BU_business")
    resp, purchase = await _buy(
        client,
        key,
        async_session,
        org_id,
        [(row_id, "business", "BU_business")],
        monkeypatch,
        subject_types=["business"],
        subject_type="person",
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "subject_type"]
    purchase.assert_not_awaited()


async def test_purchase_without_verification_ignores_subject_type(
    client, org_and_key, async_session, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    resp, purchase = await _buy(
        client, key, async_session, org_id, [], monkeypatch, subject_type="person"
    )
    assert resp.status_code == 201, resp.text
    assert purchase.await_args.args[2] is None
    assert resp.json()["subject_type"] is None


async def test_purchase_refuses_an_approval_withdrawn_since_the_quote(
    client, org_and_key, async_session, monkeypatch
) -> None:
    """The carrier re-check must still accept the chosen approval."""
    from unittest.mock import AsyncMock

    org_id, _, key = org_and_key
    row_id = await _approved(async_session, org_id, "person", "BU_person")
    await _fund(async_session, org_id)
    purchase = AsyncMock(return_value="PN_gb_1")
    monkeypatch.setattr(
        "hailhq.core.providers.voice.twilio.purchase_ordered_number", purchase
    )
    quote, offer = await _quote(
        async_session, org_id, [(row_id, "person", "BU_person")]
    )
    monkeypatch.setattr(
        "hailhq.api.number_orders.discover_offers",
        AsyncMock(return_value=([offer.model_copy(update={"approvals": []})], [])),
    )
    resp = await client.post(
        "/numbers",
        json={"country_code": "GB", "quote_id": str(quote.id)},
        headers=_auth(key),
    )
    assert resp.status_code == 409, resp.text
    purchase.assert_not_awaited()


async def test_purchase_without_verification_passes_none(
    client, org_and_key, async_session, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    resp, purchase = await _buy(client, key, async_session, org_id, [], monkeypatch)
    assert resp.status_code == 201, resp.text
    assert purchase.await_args.args[2] is None


async def test_subject_type_is_required(client, org_and_key, carrier) -> None:
    """No default: the agent must ask the user "you or your company?"."""
    _, _, key = org_and_key
    resp = await client.get(
        "/verifications/requirements",
        params={k: v for k, v in _gb_mobile().items() if k != "subject_type"},
        headers=_auth(key),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["query", "subject_type"]
    for value in ("", "company"):
        resp = await client.post(
            "/verifications",
            data=_submission(subject_type=value),
            files=_passport(),
            headers=_auth(key),
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"][0]["loc"] == ["body", "subject_type"]
    assert carrier.drafts == []


async def test_one_live_verification_per_subject_type_and_name(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    assert (await _create(client, key)).status_code == 201
    resp = await _create(client, key, subject_type="business")
    assert resp.status_code == 201, resp.text
    assert resp.json()["subject_type"] == "business"
    resp = await _create(client, key)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == (
        "You already have a verification for Ada Lovelace as a person for this "
        "number type."
    )
    resp = await _create(client, key, subject_type="business")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == (
        "You already have a verification for Ada Lovelace as a company for this "
        "number type."
    )


async def test_number_verification_without_quote_id_is_422(
    client, org_and_key, carrier
) -> None:
    """Hail picks the carrier from the offer: no quote, no form, no filing."""
    _, _, key = org_and_key
    resp = await client.get(
        "/verifications/requirements",
        params={
            "subject_type": "person",
            "country_code": "GB",
            "number_type": "mobile",
            "provider": "fake",
        },
        headers=_auth(key),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["query", "quote_id"]
    resp = await client.post(
        "/verifications",
        data=_submission(quote_id=""),
        files=_passport(),
        headers=_auth(key),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "quote_id"]
    assert carrier.drafts == []


async def _raw_quote(async_session, org_id, carrier_name: str) -> str:
    from datetime import datetime, timedelta, timezone

    from hailhq.core.models import NumberOffer

    quote = NumberOffer(
        organization_id=org_id,
        offer={
            "provider": carrier_name,
            "e164": "+447700900001",
            "country_code": "GB",
            "number_type": "mobile",
        },
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    async_session.add(quote)
    await async_session.commit()
    return str(quote.id)


async def test_quote_id_picks_the_quoted_carrier(
    client, org_and_key, carrier, async_session
) -> None:
    """A client that never sees the carrier name verifies for an offer."""
    org_id, _, key = org_and_key
    quote_id = await _raw_quote(async_session, org_id, "fake")
    resp = await client.get(
        "/verifications/requirements",
        params={
            "subject_type": "person",
            "country_code": "GB",
            "number_type": "mobile",
            "quote_id": quote_id,
        },
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        "/verifications",
        data=_submission(provider="", quote_id=quote_id),
        files=_passport(),
        headers=_auth(key),
    )
    assert resp.status_code == 201, resp.text
    assert await _row_provider(async_session, resp.json()["id"]) == "fake"


async def test_quote_id_must_belong_to_the_org(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    other_org, _, _ = await insert_org_and_key(async_session)
    quote_id = await _raw_quote(async_session, other_org, "fake")
    for q in (quote_id, str(uuid.uuid4())):
        resp = await client.get(
            "/verifications/requirements",
            params={
                "subject_type": "person",
                "country_code": "GB",
                "number_type": "mobile",
                "quote_id": q,
            },
            headers=_auth(key),
        )
        assert resp.status_code == 404


async def test_quote_id_wins_over_a_provider_param(
    client, org_and_key, carrier, async_session
) -> None:
    """A stale ``provider`` from an old client is ignored, not a 422."""
    org_id, _, key = org_and_key
    quote_id = await _raw_quote(async_session, org_id, "fake")
    resp = await client.get(
        "/verifications/requirements",
        params={
            "subject_type": "person",
            "country_code": "GB",
            "number_type": "mobile",
            "quote_id": quote_id,
            "provider": "other",
        },
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text


async def test_quote_id_must_match_country_and_number_type(
    client, org_and_key, carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    quote_id = await _raw_quote(async_session, org_id, "fake")
    for params in (
        {"country_code": "FR", "number_type": "mobile"},
        {"country_code": "GB", "number_type": "local"},
    ):
        resp = await client.get(
            "/verifications/requirements",
            params={**params, "quote_id": quote_id},
            headers=_auth(key),
        )
        assert resp.status_code == 422, resp.text
    resp = await client.post(
        "/verifications",
        data=_submission(provider="", quote_id=quote_id, country_code="FR"),
        files=_passport(),
        headers=_auth(key),
    )
    assert resp.status_code == 422, resp.text


async def test_quote_id_is_rejected_for_sms(
    client, org_and_key, carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    quote_id = await _raw_quote(async_session, org_id, "fake")
    resp = await client.get(
        "/verifications/requirements",
        params={
            "subject_type": "person",
            "country_code": "US",
            "number_type": "local",
            "purpose": "sms",
            "quote_id": quote_id,
        },
        headers=_auth(key),
    )
    assert resp.status_code == 422, resp.text


async def test_quote_from_an_unconfigured_carrier_is_404(
    client, org_and_key, carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    quote_id = await _raw_quote(async_session, org_id, "none")
    resp = await client.get(
        "/verifications/requirements",
        params=_gb_mobile(quote_id=quote_id),
        headers=_auth(key),
    )
    assert resp.status_code == 404


async def test_create_keeps_waiting_when_the_carrier_is_down(
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


async def test_create_rejects_when_the_carrier_refuses_the_submit(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key

    async def refuse(refs):
        raise SubmitRefused(
            [Problem(field="", message=NOT_ACCEPTED, raw="Group is not fulfilled")]
        )

    carrier.submit = refuse
    resp = await _create(client, key)
    assert resp.status_code == 201, resp.text
    # Not left waiting: every retry would be refused the same way.
    assert resp.json()["state"] == "rejected"
    assert resp.json()["rejection_reason"] == NOT_ACCEPTED
    assert carrier.discarded == [{"bundle_sid": "B1"}]
    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "verification.reject")
        )
    ).scalar_one()
    assert audit.payload["carrier_reason"] == "Group is not fulfilled"


async def test_create_keeps_waiting_when_a_refused_draft_cannot_be_discarded(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key

    async def refuse(refs):
        raise SubmitRefused([Problem(field="", message=NOT_ACCEPTED)])

    async def boom(refs):
        raise RuntimeError("carrier down")

    carrier.submit, carrier.discard = refuse, boom
    resp = await _create(client, key)
    assert resp.status_code == 201, resp.text
    # Not stuck in 'submitting': the sweeper tries again.
    assert resp.json()["state"] == "awaiting_review"


async def test_superadmin_approve_reports_a_refused_submit(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )

    async def refuse(refs):
        raise SubmitRefused(
            [Problem(field="", message=NOT_ACCEPTED, raw="Group is not fulfilled")]
        )

    carrier.submit = refuse
    resp = await client.post(f"/admin/verifications/{vid}/approve", headers=_auth(key))
    assert resp.status_code == 409
    assert "Group is not fulfilled" in resp.json()["detail"]
    got = (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()
    assert got["state"] == "rejected"
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    # A rejected row never names an approving admin.
    assert row.approved_by is None


async def test_sweeper_submits_waiting_drafts_and_pulls_decisions(
    client, org_and_key, carrier, async_session
) -> None:
    from hailhq.api.routes.verifications import sweep_verifications

    _, _, key = org_and_key
    waiting = await _unsent(client, key, carrier, async_session)
    # Too fresh: creation may still be running; the sweeper leaves it alone.
    counts = await sweep_verifications(
        async_session,
        lambda name, purpose="number": carrier if name == "fake" else None,
    )
    assert counts == {"submitted": 0, "refreshed": 0} and carrier.submitted == []
    await _set_state(async_session, waiting, "awaiting_review", age_s=3 * 60)
    carrier.remote_status = ProviderStatus(state="approved")
    counts = await sweep_verifications(
        async_session,
        lambda name, purpose="number": carrier if name == "fake" else None,
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


async def test_sweeper_rejects_a_draft_the_carrier_refuses(
    client, org_and_key, carrier, async_session
) -> None:
    from hailhq.api.routes.verifications import sweep_verifications

    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    await _set_state(async_session, vid, "awaiting_review", age_s=3 * 60)
    carrier.check_problems = [
        Problem(field="", message="document unreadable"),
        Problem(
            field="proof_of_identity",
            message=VALUE_NOT_ACCEPTED,
            raw="CarrierCo: blurry scan",
        ),
    ]
    counts = await sweep_verifications(
        async_session,
        lambda name, purpose="number": carrier if name == "fake" else None,
    )
    assert counts["submitted"] == 0 and carrier.submitted == []
    got = (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()
    assert got["state"] == "rejected"
    # Hail's wording with the field; the carrier's text only in the audit row.
    assert got["rejection_reason"] == (
        f"document unreadable; proof_of_identity: {VALUE_NOT_ACCEPTED}"
    )
    assert carrier.discarded == [{"bundle_sid": "B1"}]
    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "verification.reject")
        )
    ).scalar_one()
    assert audit.actor_kind == "system" and audit.actor_user_id is None
    assert audit.payload["carrier_reason"] == "CarrierCo: blurry scan"
    # Not retried: a second pass leaves it alone.
    carrier.check_problems = []
    counts = await sweep_verifications(
        async_session,
        lambda name, purpose="number": carrier if name == "fake" else None,
    )
    assert counts["submitted"] == 0 and carrier.submitted == []
    # A rejected verification does not block a new one.
    assert (await _create(client, key)).status_code == 201


async def test_sweeper_keeps_a_refused_draft_when_the_discard_fails(
    client, org_and_key, carrier, async_session
) -> None:
    from hailhq.api.routes.verifications import sweep_verifications

    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    await _set_state(async_session, vid, "awaiting_review", age_s=3 * 60)
    carrier.check_problems = [Problem(field="", message="document unreadable")]

    async def boom(refs):
        raise RuntimeError("carrier down")

    carrier.discard = boom
    counts = await sweep_verifications(
        async_session,
        lambda name, purpose="number": carrier if name == "fake" else None,
    )
    assert counts == {"submitted": 0, "refreshed": 0}
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "awaiting_review" and row.provider_refs == {"bundle_sid": "B1"}
    assert (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "verification.reject")
        )
    ).scalar_one_or_none() is None


async def test_response_carries_purpose_number_by_default(
    client, org_and_key, carrier
) -> None:
    _, _, key = org_and_key
    resp = await _create(client, key)
    assert resp.status_code == 201, resp.text
    assert resp.json()["purpose"] == "number"


def _sms_submission(**overrides) -> dict:
    data = {
        "provider": "fake",
        "purpose": "sms",
        "country_code": "US",
        "number_type": "local",
        "subject_type": "business",
        "fields": json.dumps(
            {"legal_name": "Acme", "campaign_description": "Order updates"}
        ),
        "address": json.dumps(
            {
                "customer_name": "Acme",
                "street": "1 Main St",
                "city": "Austin",
                "region": "TX",
                "postal_code": "78701",
                "country_code": "US",
            }
        ),
    }
    data.update(overrides)
    return data


async def _post_sms(client, key, **overrides):
    # A (None, value) file entry makes httpx send multipart/form-data.
    return await client.post(
        "/verifications",
        data=_sms_submission(**overrides),
        files={"_": (None, "1")},
        headers=_auth(key),
    )


async def _set_balance(async_session, org_id, cents: int) -> None:
    """The org fixture starts with credit; move the balance to exactly ``cents``."""
    diff = cents - await _balance(async_session, org_id)
    if diff:
        async_session.add(
            AccountCredit(
                organization_id=org_id,
                kind="credit" if diff > 0 else "debit",
                channel="credit" if diff > 0 else "voice",
                amount_cents=diff,
                qty=1,
                ref=f"test-balance-{uuid.uuid4()}",
                source="test",
            )
        )
        await async_session.commit()


async def _balance(async_session, org_id) -> int:
    from sqlalchemy import func

    total = (
        await async_session.execute(
            select(func.coalesce(func.sum(AccountCredit.amount_cents), 0)).where(
                AccountCredit.organization_id == org_id
            )
        )
    ).scalar_one()
    return int(total)


@pytest.mark.usefixtures("us_number")
async def test_sms_registration_requires_purpose_sms_provider(
    client, org_and_key, carrier
) -> None:
    # Only the number plug-in is registered: purpose=sms is 404.
    _, _, key = org_and_key
    resp = await _post_sms(client, key)
    assert resp.status_code == 404, resp.text


@pytest.mark.usefixtures("us_number")
async def test_sms_registration_debits_filing_fee(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    resp = await _post_sms(client, key)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["purpose"] == "sms"
    assert body["state"] == "submitted"
    assert await _balance(async_session, org_id) == 10_000 - 6100
    debit = (
        await async_session.execute(
            select(AccountCredit).where(
                AccountCredit.ref == f"sms_registration:{body['id']}"
            )
        )
    ).scalar_one()
    assert debit.channel == "sms" and int(debit.amount_cents) == -6100


@pytest.mark.usefixtures("us_number")
async def test_sms_registration_402_when_balance_too_low(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 5_000)
    resp = await _post_sms(client, key)
    assert resp.status_code == 402, resp.text
    assert sms_carrier.drafts == []
    assert await _balance(async_session, org_id) == 5_000


@pytest.mark.usefixtures("us_number")
async def test_sms_registration_rejected_brand_keeps_fee(
    client, org_and_key, sms_carrier, async_session, caplog
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    vid = (await _post_sms(client, key)).json()["id"]
    sms_carrier.remote_status = ProviderStatus(
        state="rejected", reason=BRAND_REJECTED, raw_reason="EIN does not match"
    )
    verifications_routes._last_polled.clear()
    with caplog.at_level("WARNING", logger="hailhq.api.routes.verifications"):
        resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "rejected"
    # The customer reads Hail's wording, never the carrier's.
    assert resp.json()["rejection_reason"] == BRAND_REJECTED
    assert "EIN" not in resp.text
    messages = [r.getMessage() for r in caplog.records]
    assert not any("EIN" in m for m in messages)
    logged = [m for m in messages if "rejected verification" in m]
    assert len(logged) == 1 and vid in logged[0] and str(org_id) in logged[0]
    audit = (
        await async_session.execute(
            select(AuditLog).where(
                AuditLog.action == "verification.reject",
                AuditLog.resource_id == uuid.UUID(vid),
            )
        )
    ).scalar_one()
    assert audit.payload["carrier_reason"] == "EIN does not match"
    assert await _balance(async_session, org_id) == 10_000 - 6100


@pytest.mark.usefixtures("us_number")
async def test_sms_and_number_rows_coexist(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    assert (await _post_sms(client, key)).status_code == 201
    assert (await _post_sms(client, key)).status_code == 409
    resp = await client.get(
        "/verifications", params={"purpose": "sms"}, headers=_auth(key)
    )
    assert [r["purpose"] for r in resp.json()] == ["sms"]


@pytest.mark.usefixtures("us_number")
async def test_sms_requirements_endpoint(client, org_and_key, sms_carrier) -> None:
    _, _, key = org_and_key
    resp = await client.get(
        "/verifications/requirements",
        params={
            "country_code": "US",
            "number_type": "local",
            "purpose": "sms",
            "subject_type": "business",
            "provider": "fake",
        },
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["address_required"] is True


_FAKE_FEES = {
    "person": {"filing_cents": 1950, "monthly_cents": 200, "minimum_months": 1},
    "business": {"filing_cents": 6100, "monthly_cents": 1000, "minimum_months": 1},
}


async def test_sms_fees_endpoint(client, org_and_key, sms_carrier) -> None:
    # No US number yet: the fees of the default carrier.
    _, _, key = org_and_key
    resp = await client.get("/verifications/sms-fees", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.json() == _FAKE_FEES


async def test_sms_fees_endpoint_shows_a_carrier_minimum(
    client, org_and_key, sms_carrier, monkeypatch
) -> None:
    # The file's minimum_months reaches the API; "fake" stands in.
    row = sms_registration_fees._load()["fake"]
    monkeypatch.setitem(row["person"], "minimum_months", 3)
    _, _, key = org_and_key
    resp = await client.get("/verifications/sms-fees", headers=_auth(key))
    assert resp.json()["person"]["minimum_months"] == 3


async def test_sms_fees_follow_the_carrier_of_the_us_numbers(
    client, async_session, org_and_key, sms_carrier
) -> None:
    org_id, _, key = org_and_key
    # The default carrier has no fees: only the org's number can pick "fake".
    app.dependency_overrides[get_default_sms_provider_name] = lambda: "none"
    resp = await client.get("/verifications/sms-fees", headers=_auth(key))
    assert resp.status_code == 404, resp.text
    mine = await _us_number(async_session, org_id)
    for params in ({}, {"number_id": str(mine.id)}):
        resp = await client.get(
            "/verifications/sms-fees", params=params, headers=_auth(key)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == _FAKE_FEES


async def test_sms_fees_with_numbers_on_two_carriers_need_number_id(
    client, async_session, org_and_key, sms_carrier
) -> None:
    org_id, _, key = org_and_key
    mine = await _us_number(async_session, org_id, "+15125550100", resource="PN1")
    await _us_number(
        async_session, org_id, "+15125550101", provider="telnyx", resource="PN2"
    )
    resp = await client.get("/verifications/sms-fees", headers=_auth(key))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["query", "number_id"]
    resp = await client.get(
        "/verifications/sms-fees",
        params={"number_id": str(mine.id)},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == _FAKE_FEES


async def test_sms_fees_for_a_number_that_is_not_yours_is_404(
    client, async_session, org_and_key, sms_carrier
) -> None:
    org_id, _, key = org_and_key
    other_org, _, _ = await insert_org_and_key(async_session)
    theirs = await _us_number(async_session, other_org, e164="+15125550199")
    voice_only = await _us_number(
        async_session, org_id, e164="+15125550198", resource="PN2"
    )
    voice_only.capabilities = ["voice"]
    await async_session.commit()
    for number_id in (theirs.id, voice_only.id, uuid.uuid4()):
        resp = await client.get(
            "/verifications/sms-fees",
            params={"number_id": str(number_id)},
            headers=_auth(key),
        )
        assert resp.status_code == 404, resp.text


async def test_sms_registration_rejects_non_us(
    client, org_and_key, sms_carrier
) -> None:
    _, _, key = org_and_key
    resp = await _post_sms(client, key, country_code="GB")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "country_code"]


async def test_sms_registration_rejects_non_local(
    client, org_and_key, sms_carrier
) -> None:
    _, _, key = org_and_key
    resp = await _post_sms(client, key, number_type="mobile")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "country_code"]


@pytest.mark.usefixtures("us_number")
async def test_approved_sms_row_is_not_a_number_purchase_handle(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    vid = (await _post_sms(client, key)).json()["id"]
    row = (
        await async_session.execute(
            select(CarrierVerification).where(CarrierVerification.id == uuid.UUID(vid))
        )
    ).scalar_one()
    row.state = "approved"
    await async_session.commit()
    handles = await verifications_routes.approved_handles(
        async_session,
        lambda name, purpose="number": sms_carrier,
        org_id,
        "US",
        "local",
        ["fake"],
    )
    assert handles == {}


@pytest.fixture()
async def us_number(async_session, org_and_key):
    """An active US number on the fake carrier: an SMS registration is filed
    with the carrier of the org's US numbers (``provider`` is ignored)."""
    return await _us_number(
        async_session, org_and_key[0], "+15125550199", resource="PN-fixture"
    )


async def _us_number(
    async_session, org_id, e164="+15125550100", provider="fake", resource="PN1"
) -> PhoneNumber:
    n = PhoneNumber(
        organization_id=org_id,
        e164=e164,
        country_code="US",
        number_type="local",
        capabilities=["voice", "sms"],
        provider=provider,
        provider_resource_id=resource,
        provisioning_state="active",
        messaging_service_sid="MG1",
    )
    async_session.add(n)
    await async_session.commit()
    return n


async def test_approval_attaches_every_us_long_code(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    await _us_number(async_session, org_id, "+15125550100", resource="PN1")
    await _us_number(async_session, org_id, "+15125550101", resource="PN2")
    vid = (await _post_sms(client, key)).json()["id"]
    sms_carrier.remote_status = ProviderStatus(state="approved")
    verifications_routes._last_polled.clear()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "approved"
    assert sorted(r for _, r, _ in sms_carrier.attached) == ["PN1", "PN2"]
    on = (
        await async_session.execute(
            select(PhoneNumber.provider_resource_id).where(
                PhoneNumber.sms_registration_id == uuid.UUID(vid)
            )
        )
    ).scalars()
    assert sorted(on) == ["PN1", "PN2"]
    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "verification.sms_attached")
        )
    ).scalar_one()
    assert audit.payload == {"numbers": 2, "failed": 0}


async def test_person_registration_attaches_the_first_number_only(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    await _us_number(async_session, org_id, "+15125550100", resource="PN1")
    await _us_number(async_session, org_id, "+15125550101", resource="PN2")
    vid = (await _post_sms(client, key, subject_type="person")).json()["id"]
    sms_carrier.remote_status = ProviderStatus(state="approved")
    verifications_routes._last_polled.clear()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "approved"
    assert len(sms_carrier.attached) == 1
    # A number bought later does not join a person's registration.
    from hailhq.api import sms_registration

    later = await _us_number(async_session, org_id, "+15125550102", resource="PN3")
    await sms_registration.attach_if_registered(
        async_session,
        later,
        lambda n, p="number": sms_carrier if p == "sms" else None,
    )
    assert [r for _, r, _ in sms_carrier.attached] == ["PN1"]
    assert later.sms_registration_id is None


async def _approved_sms_row(client, key, async_session, sms_carrier, **overrides):
    """File an SMS registration and have the carrier approve it."""
    vid = (await _post_sms(client, key, **overrides)).json()["id"]
    sms_carrier.remote_status = ProviderStatus(state="approved")
    verifications_routes._last_polled.clear()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "approved"
    return vid


async def test_cancel_an_approved_sms_registration(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    number = await _us_number(async_session, org_id)
    vid = await _approved_sms_row(client, key, async_session, sms_carrier)
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "cancelled"
    assert sms_carrier.detached == ["PN1"]
    assert sms_carrier.discarded  # the campaign is deleted at the carrier
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.cancelled_at is not None
    await async_session.refresh(number)
    assert number.sms_registration_id is None
    assert number.messaging_service_sid == "MG1"  # a company's number stays
    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "verification.cancel")
        )
    ).scalar_one()
    assert audit.payload["state_before"] == "approved"


async def test_cancel_a_person_registration_frees_the_number_service(
    client, org_and_key, sms_carrier, async_session, caplog
) -> None:
    # A person's number leaves the registration's own service; the org's
    # service setup then runs for it (it fails here: "fake" cannot text).
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    number = await _us_number(async_session, org_id)
    vid = await _approved_sms_row(
        client, key, async_session, sms_carrier, subject_type="person"
    )
    sms_carrier.detach_leaves_service = True
    with caplog.at_level("WARNING", logger="hailhq.api.routes.verifications"):
        resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    await async_session.refresh(number)
    assert number.sms_registration_id is None
    assert number.messaging_service_sid is None
    assert any("enable-sms" in r.getMessage() for r in caplog.records)


async def test_an_approved_number_verification_cannot_be_cancelled(
    client, org_and_key, async_session
) -> None:
    org_id, _, key = org_and_key
    row = CarrierVerification(
        organization_id=org_id,
        provider="fake",
        country_code="GB",
        number_type="mobile",
        subject_type="business",
        state="approved",
        provider_refs={},
        requirements_version="1",
    )
    async_session.add(row)
    await async_session.commit()
    resp = await client.delete(f"/verifications/{row.id}", headers=_auth(key))
    assert resp.status_code == 409, resp.text


async def test_attach_failure_still_approves_and_logs_the_number(
    client, org_and_key, sms_carrier, async_session, caplog
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    number = await _us_number(async_session, org_id)
    vid = (await _post_sms(client, key)).json()["id"]
    sms_carrier.remote_status = ProviderStatus(state="approved")
    sms_carrier.attach_error = RuntimeError("carrier down")
    verifications_routes._last_polled.clear()
    with caplog.at_level("WARNING", logger="hailhq.api.sms_registration"):
        resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "approved"
    assert sms_carrier.attached == []
    assert any(str(number.id) in r.getMessage() for r in caplog.records)
    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "verification.sms_attached")
        )
    ).scalar_one()
    assert audit.payload == {"numbers": 0, "failed": 1}


async def test_one_bad_number_does_not_stop_the_others(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    await _us_number(async_session, org_id, "+15125550100", resource="PN1")
    await _us_number(async_session, org_id, "+15125550101", resource="PN2")
    vid = (await _post_sms(client, key)).json()["id"]
    sms_carrier.remote_status = ProviderStatus(state="approved")
    real_attach = sms_carrier.attach_number

    async def attach(refs, *, provider_resource_id, e164):
        if provider_resource_id == "PN1":
            raise RuntimeError("carrier refused")
        await real_attach(refs, provider_resource_id=provider_resource_id, e164=e164)

    sms_carrier.attach_number = attach
    verifications_routes._last_polled.clear()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "approved"
    assert [r for _, r, _ in sms_carrier.attached] == ["PN2"]


async def test_enable_sms_attaches_to_the_approved_registration(
    client, org_and_key, sms_carrier, async_session, sms_mock, monkeypatch
) -> None:
    # The repair path for a number whose attach failed at approval: the
    # number already has its messaging service.
    from hailhq.api.routes import numbers as numbers_routes

    org_id, _, key = org_and_key
    number = await _us_number(async_session, org_id, provider="twilio")
    async_session.add(
        CarrierVerification(
            organization_id=org_id,
            provider="twilio",
            purpose="sms",
            country_code="US",
            number_type="local",
            subject_type="business",
            state="approved",
            provider_refs={"campaign_sid": "QE1", "messaging_service_sid": "MG1"},
            requirements_version="1",
        )
    )
    await async_session.commit()
    monkeypatch.setattr(
        numbers_routes,
        "get_verification_provider",
        lambda name, purpose="number": sms_carrier if purpose == "sms" else None,
    )
    resp = await client.post(f"/numbers/{number.id}/enable-sms", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert [r for _, r, _ in sms_carrier.attached] == ["PN1"]
    await async_session.refresh(number)
    assert number.sms_registration_id is not None  # saved, so the gate sees it


@pytest.mark.usefixtures("us_number")
async def test_number_activated_later_is_attached(
    client, org_and_key, sms_carrier, async_session
) -> None:
    from hailhq.api import sms_registration

    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    vid = (await _post_sms(client, key)).json()["id"]
    # The number the registration was filed for is gone before approval.
    from sqlalchemy import update

    await async_session.execute(
        update(PhoneNumber)
        .where(PhoneNumber.e164 == "+15125550199")
        .values(provisioning_state="released")
    )
    await async_session.commit()
    sms_carrier.remote_status = ProviderStatus(state="approved")
    verifications_routes._last_polled.clear()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["state"] == "approved"
    # Nothing was attached at approval: no sms_attached audit row.
    assert (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "verification.sms_attached")
        )
    ).first() is None
    number = await _us_number(async_session, org_id, "+15125550177", resource="PN7")
    await sms_registration.attach_if_registered(
        async_session,
        number,
        lambda n, p="number": sms_carrier if p == "sms" else None,
    )
    assert sms_carrier.attached[-1][1] == "PN7"


async def _stored_refs(async_session, vid: str) -> dict:
    return (
        await async_session.execute(
            select(CarrierVerification.provider_refs).where(
                CarrierVerification.id == uuid.UUID(vid)
            )
        )
    ).scalar_one()


@pytest.mark.usefixtures("us_number")
@pytest.mark.parametrize("campaign_key", ["campaign_sid", "campaign_id"])
@pytest.mark.parametrize("via", ["get", "list"])
async def test_campaign_id_written_during_a_poll_is_saved(
    client, org_and_key, sms_carrier, async_session, campaign_key, via
) -> None:
    # The plug-ins create the campaign inside status() and write its id into
    # refs in place. The id must reach the database, or every later poll
    # creates (and pays for) another campaign.
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    vid = (await _post_sms(client, key)).json()["id"]
    sms_carrier.campaign_key = campaign_key
    sms_carrier.remote_status = ProviderStatus(state="pending")
    url = f"/verifications/{vid}" if via == "get" else "/verifications"
    for _ in range(2):
        verifications_routes._last_polled.clear()
        resp = await client.get(url, headers=_auth(key))
        assert resp.status_code == 200, resp.text
        assert (await _stored_refs(async_session, vid))[campaign_key] == "QE1"
    assert sms_carrier.campaigns_created == 1


async def test_telnyx_release_unlinks_the_number_from_the_campaign(
    client, org_and_key, async_session, monkeypatch
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from hailhq.api.routes import numbers as numbers_routes
    from hailhq.core.providers.verification import SmsRegistrationProvider

    org_id, _, key = org_and_key
    resource = str(uuid.uuid4())
    number = await _us_number(
        async_session, org_id, provider="telnyx", resource=resource
    )
    row = CarrierVerification(
        organization_id=org_id,
        provider="telnyx",
        purpose="sms",
        country_code="US",
        number_type="local",
        subject_type="business",
        state="approved",
        provider_refs={"campaign_id": "C1"},
        requirements_version="1",
    )
    async_session.add(row)
    await async_session.flush()
    number.sms_registration_id = row.id
    await async_session.commit()
    monkeypatch.setattr(settings, "telnyx_api_key", "test")
    release = AsyncMock()
    monkeypatch.setattr(
        "hailhq.core.providers.telnyx.TelnyxClient.release_number", release
    )
    plugin = MagicMock(spec=SmsRegistrationProvider)
    plugin.detach_number = AsyncMock(return_value=False)
    monkeypatch.setattr(
        numbers_routes,
        "get_verification_provider",
        lambda name, purpose="number": plugin if purpose == "sms" else None,
    )
    resp = await client.delete(f"/numbers/{number.id}", headers=_auth(key))
    assert resp.status_code == 204, resp.text
    plugin.detach_number.assert_awaited_once_with(
        {"campaign_id": "C1"}, provider_resource_id=resource, e164="+15125550100"
    )
    release.assert_awaited_once()
    await async_session.refresh(number)
    assert number.sms_registration_id is None


def _per_request_sessions(session_factory):
    """Give each request its own session, so two requests run in two
    transactions and the database locks are what order them."""
    from hailhq.core.db import get_session

    async def per_request_session():
        async with session_factory() as s:
            yield s

    saved = app.dependency_overrides[get_session]
    app.dependency_overrides[get_session] = per_request_session
    return lambda: app.dependency_overrides.__setitem__(get_session, saved)


@pytest.mark.usefixtures("us_number")
async def test_overlapping_polls_create_one_campaign(
    client, org_and_key, sms_carrier, async_session, session_factory
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    vid = (await _post_sms(client, key)).json()["id"]
    sms_carrier.campaign_key = "campaign_sid"
    sms_carrier.entered = asyncio.Event()
    sms_carrier.release = asyncio.Event()
    restore = _per_request_sessions(session_factory)
    try:
        first = asyncio.create_task(
            client.get(f"/verifications/{vid}", headers=_auth(key))
        )
        await asyncio.wait_for(sms_carrier.entered.wait(), 5)
        # The first poll holds the row: the second skips the carrier call.
        second = await asyncio.wait_for(
            client.get(f"/verifications/{vid}", headers=_auth(key)), 5
        )
        assert second.status_code == 200, second.text
        sms_carrier.release.set()
        assert (await first).status_code == 200
    finally:
        sms_carrier.release.set()
        restore()
    assert sms_carrier.campaigns_created == 1
    assert (await _stored_refs(async_session, vid))["campaign_sid"] == "QE1"


@pytest.mark.usefixtures("us_number")
async def test_overlapping_filings_register_one_brand(
    client, org_and_key, sms_carrier, async_session, session_factory
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 20_000)
    sms_carrier.draft_delay = 0.2
    restore = _per_request_sessions(session_factory)
    try:
        r1, r2 = await asyncio.gather(_post_sms(client, key), _post_sms(client, key))
    finally:
        restore()
    assert sorted([r1.status_code, r2.status_code]) == [201, 409]
    assert len(sms_carrier.drafts) == 1


async def test_sms_carrier_defaults_to_the_one_of_the_us_numbers(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    await _us_number(async_session, org_id)
    resp = await client.get(
        "/verifications/requirements",
        params={
            "subject_type": "person",
            "country_code": "US",
            "number_type": "local",
            "purpose": "sms",
        },
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    assert "provider" not in resp.json()
    resp = await _post_sms(client, key, provider="")
    assert resp.status_code == 201, resp.text
    assert await _row_provider(async_session, resp.json()["id"]) == "fake"


async def test_sms_without_us_numbers_needs_one_first(
    client, org_and_key, sms_carrier
) -> None:
    _, _, key = org_and_key
    resp = await _post_sms(client, key, provider="")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"][0]
    assert detail["loc"] == ["body", "number_id"]
    assert detail["msg"] == "Buy a US number first, then register it for texting."
    resp = await client.get(
        "/verifications/requirements",
        params={
            "subject_type": "person",
            "country_code": "US",
            "number_type": "local",
            "purpose": "sms",
        },
        headers=_auth(key),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["query", "number_id"]


async def test_sms_with_us_numbers_on_two_carriers_must_name_one(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _us_number(async_session, org_id, "+15125550100", resource="PN1")
    await _us_number(
        async_session, org_id, "+15125550101", provider="telnyx", resource="PN2"
    )
    resp = await _post_sms(client, key, provider="")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"][0]
    assert detail["loc"] == ["body", "number_id"]
    assert detail["msg"] == (
        "Your US numbers are with more than one carrier. Pass number_id: "
        "the id of the number to register."
    )
    assert sms_carrier.drafts == []


async def test_sms_number_id_picks_that_numbers_carrier(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    fake = await _us_number(async_session, org_id, "+15125550100", resource="PN1")
    await _us_number(
        async_session, org_id, "+15125550101", provider="telnyx", resource="PN2"
    )
    resp = await client.get(
        "/verifications/requirements",
        params={
            "subject_type": "person",
            "country_code": "US",
            "number_type": "local",
            "purpose": "sms",
            "number_id": str(fake.id),
        },
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    resp = await _post_sms(client, key, provider="", number_id=str(fake.id))
    assert resp.status_code == 201, resp.text
    assert await _row_provider(async_session, resp.json()["id"]) == "fake"


async def test_sms_number_id_must_be_an_org_us_number(
    client, org_and_key, sms_carrier, async_session
) -> None:
    _, _, key = org_and_key
    other_org, _, _ = await insert_org_and_key(async_session)
    theirs = await _us_number(async_session, other_org, resource="PN9")
    for number_id in (theirs.id, uuid.uuid4()):
        resp = await _post_sms(client, key, provider="", number_id=str(number_id))
        assert resp.status_code == 404, resp.text
    assert sms_carrier.drafts == []


async def test_sms_number_id_wins_over_a_provider_part(
    client, org_and_key, sms_carrier, async_session
) -> None:
    """A stale ``provider`` part from an old client is ignored, not a 422."""
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    mine = await _us_number(async_session, org_id)
    resp = await _post_sms(client, key, provider="telnyx", number_id=str(mine.id))
    assert resp.status_code == 201, resp.text
    assert await _row_provider(async_session, resp.json()["id"]) == "fake"


async def test_number_id_is_rejected_for_number_purpose(client, org_and_key) -> None:
    _, _, key = org_and_key
    resp = await client.get(
        "/verifications/requirements",
        params={
            "subject_type": "person",
            "country_code": "GB",
            "number_type": "local",
            "number_id": str(uuid.uuid4()),
        },
        headers=_auth(key),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["query", "number_id"]


async def test_shared_key_filing_writes_no_debit(
    client, sms_carrier, async_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "hail_api_key", "hail-test-shared-secret")
    await _us_number(async_session, SELF_HOSTED_ORG_ID)
    resp = await _post_sms(client, "hail-test-shared-secret")
    assert resp.status_code == 201, resp.text
    debit = (
        await async_session.execute(
            select(AccountCredit).where(
                AccountCredit.ref == f"sms_registration:{resp.json()['id']}"
            )
        )
    ).first()
    assert debit is None


# -- person confirmation (code the carrier texted) ------------------------


async def _person_sms(client, key, async_session, org_id) -> dict:
    await _set_balance(async_session, org_id, 10_000)
    await _us_number(async_session, org_id, "+15125550198", resource="PN-person")
    resp = await _post_sms(client, key, subject_type="person")
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _confirm(client, key, vid, code="123456"):
    return await client.post(
        f"/verifications/{vid}/confirm", json={"code": code}, headers=_auth(key)
    )


async def test_person_registration_asks_for_the_code(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    body = await _person_sms(client, key, async_session, org_id)
    assert body["state"] == "submitted" and body["confirmation"] == "code"
    got = await client.get(f"/verifications/{body['id']}", headers=_auth(key))
    assert got.json()["confirmation"] == "code"
    listed = await client.get("/verifications", headers=_auth(key))
    assert listed.json()[0]["confirmation"] == "code"


@pytest.mark.usefixtures("us_number")
async def test_company_registration_has_no_confirmation(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    body = (await _post_sms(client, key)).json()
    assert body["confirmation"] is None


async def test_confirm_sends_the_code_and_creates_the_campaign(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    vid = (await _person_sms(client, key, async_session, org_id))["id"]
    sms_carrier.campaign_key = "campaign_id"
    calls_before = sms_carrier.status_calls
    resp = await _confirm(client, key, vid, "482913")
    assert resp.status_code == 200, resp.text
    assert resp.json()["confirmation"] is None
    assert [c for _, c in sms_carrier.confirmed] == ["482913"]
    assert sms_carrier.status_calls == calls_before + 1
    refs = await _stored_refs(async_session, vid)
    assert refs["identity_verified"] is True and refs["campaign_id"] == "QE1"
    assert "482913" not in json.dumps(refs)
    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "verification.confirm")
        )
    ).scalar_one()
    assert str(audit.resource_id) == vid
    assert "482913" not in json.dumps(audit.payload)


async def test_confirm_wrong_code_is_422_on_code(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    vid = (await _person_sms(client, key, async_session, org_id))["id"]
    sms_carrier.confirm_problems = [
        Problem(field="code", message="That code is wrong or has expired.")
    ]
    resp = await _confirm(client, key, vid)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "code"]
    assert "identity_verified" not in await _stored_refs(async_session, vid)


@pytest.mark.usefixtures("us_number")
async def test_confirm_on_a_company_row_is_409(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    vid = (await _post_sms(client, key)).json()["id"]
    resp = await _confirm(client, key, vid)
    assert resp.status_code == 409, resp.text
    assert sms_carrier.confirmed == []


async def test_confirm_on_another_orgs_row_is_404(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    vid = (await _person_sms(client, key, async_session, org_id))["id"]
    _, _, other_key = await insert_org_and_key(async_session)
    resp = await _confirm(client, other_key, vid)
    assert resp.status_code == 404, resp.text
    assert sms_carrier.confirmed == []


async def test_confirm_on_a_reply_carrier_is_409(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    sms_carrier.confirm_kind = "reply"
    body = await _person_sms(client, key, async_session, org_id)
    assert body["confirmation"] == "reply"
    resp = await _confirm(client, key, body["id"])
    assert resp.status_code == 409, resp.text


async def test_confirm_code_length_is_checked(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    vid = (await _person_sms(client, key, async_session, org_id))["id"]
    assert (await _confirm(client, key, vid, "12")).status_code == 422
    assert (await _confirm(client, key, vid, "1" * 11)).status_code == 422
    assert sms_carrier.confirmed == []


async def test_confirm_is_capped_after_five_wrong_codes(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    vid = (await _person_sms(client, key, async_session, org_id))["id"]
    sms_carrier.confirm_problems = [
        Problem(field="code", message="That code is wrong or has expired.")
    ]
    for _ in range(5):
        assert (await _confirm(client, key, vid, "000000")).status_code == 422
    assert (await _stored_refs(async_session, vid))["confirm_attempts"] == 5
    resp = await _confirm(client, key, vid, "111111")
    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"] == "Too many wrong codes. Wait for a new code."
    assert len(sms_carrier.confirmed) == 5
    audits = (
        (
            await async_session.execute(
                select(AuditLog).where(AuditLog.action == "verification.confirm")
            )
        )
        .scalars()
        .all()
    )
    assert [a.payload for a in audits] == [
        {"ok": False, "attempts": n} for n in range(1, 6)
    ]
    assert "000000" not in json.dumps([a.payload for a in audits])


async def test_a_new_code_resets_the_attempt_cap(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    vid = (await _person_sms(client, key, async_session, org_id))["id"]
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    # What the carrier plug-in writes when it sends a new code.
    row.provider_refs = {**row.provider_refs, "confirm_attempts": 0}
    await async_session.commit()
    resp = await _confirm(client, key, vid)
    assert resp.status_code == 200, resp.text
    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "verification.confirm")
        )
    ).scalar_one()
    assert audit.payload == {"ok": True, "attempts": 1}
