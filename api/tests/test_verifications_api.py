"""Tests for /verifications: collect details and documents, build the carrier
record through a provider plug-in, superadmin approval, and purchase.

A fake carrier stands in for Twilio so nothing here depends on it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from hailhq.api import sms_registration, verification_decisions
from hailhq.api.funds import BILLING_URL
from hailhq.api.main import app
from hailhq.api.routes import verifications as verifications_routes
from hailhq.api.routes.verifications import (
    get_verification_registry,
)
from hailhq.api.superadmin import require_superadmin
from hailhq.core import sms_registration_fees, telephony_catalog
from hailhq.core.config import SELF_HOSTED_ORG_ID, settings
from hailhq.core.db import session_scope
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
    RegistrationHoldsAnotherNumber,
)
from hailhq.core.providers.verification.forms import display_name
from sqlalchemy import select, update

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
        # With problems: what the carrier made and bills anyway.
        self.billed_refs: dict = {}
        # Set to "campaign_sid" (Twilio) or "campaign_id" (Telnyx): status()
        # then creates the campaign once, writes its id into refs in place
        # and reports pending, like the real plug-ins.
        self.campaign_key: str | None = None
        self.campaigns_created = 0
        # status() calls that would have filed the campaign but were told not to.
        self.campaigns_skipped = 0
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
            return DraftResult(
                refs={}, problems=self.problems, billed_refs=self.billed_refs
            )
        return DraftResult(
            refs={
                "brand_sid": f"BN{len(self.drafts)}",
                "subject_type": kwargs["requirements"].subject_type,
            }
        )

    async def check(self, refs):
        return []

    async def submit(self, refs):
        self.submitted.append(refs)

    def confirmation(self, refs):
        if refs.get("subject_type") == "person" and not refs.get("identity_verified"):
            return self.confirm_kind
        return None

    @property
    def campaign_ref(self):
        return self.campaign_key

    async def confirm(self, refs, code):
        if self.confirm_kind != "code":
            raise NotImplementedError
        self.confirmed.append((dict(refs), code))
        return self.confirm_problems

    async def status(self, refs, *, may_file_campaign=True):
        self.status_calls += 1
        if self.release is not None and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
        if self.campaign_key and self.campaign_key not in refs:
            if not may_file_campaign:
                # Like the real plug-ins: everything but the paid filing.
                self.campaigns_skipped += 1
                return self.remote_status
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
    yield fake
    app.dependency_overrides.pop(get_verification_registry, None)


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


def _with_limits(carrier, monkeypatch, first_name_max: int, number_max: int) -> None:
    """The fake carrier's form with a max_length on a field and on a
    document type's field (the fake checks nothing itself, like DIDWW)."""
    base = carrier.requirements

    async def requirements(country_code, number_type, subject_type):
        req = await base(country_code, number_type, subject_type)
        return req.model_copy(
            update={
                "fields": (
                    FieldSpec(
                        name="first_name", label="First name", max_length=first_name_max
                    ),
                    FieldSpec(name="last_name", label="Last name"),
                ),
                "documents": (
                    DocumentSlot(
                        name="proof_of_identity",
                        label="Proof of identity",
                        options=(
                            DocumentOption(
                                key="passport",
                                label="Passport",
                                fields=(
                                    FieldSpec(
                                        name="number",
                                        label="Number",
                                        max_length=number_max,
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            }
        )

    monkeypatch.setattr(carrier, "requirements", requirements)


async def test_create_refuses_values_over_max_length_for_any_carrier(
    client, org_and_key, carrier, async_session, monkeypatch
) -> None:
    """The API checks every max_length itself, all at once, before the
    plug-in sees the form: nothing is filed."""
    _, _, key = org_and_key
    _with_limits(carrier, monkeypatch, first_name_max=3, number_max=4)
    resp = await _create(
        client,
        key,
        fields=json.dumps({"first_name": "Adaline", "last_name": "Lovelace"}),
        documents=json.dumps(
            {"proof_of_identity": {"option": "passport", "fields": {"number": "12345"}}}
        ),
    )
    assert resp.status_code == 422, resp.text
    assert [(d["loc"], d["msg"]) for d in resp.json()["detail"]] == [
        (["body", "first_name"], "Use at most 3 characters."),
        (["body", "proof_of_identity", "number"], "Use at most 4 characters."),
    ]
    assert carrier.drafts == []
    assert (await async_session.execute(select(CarrierVerification))).first() is None


async def test_create_counts_max_length_in_utf16_units(
    client, org_and_key, carrier, monkeypatch
) -> None:
    """2 emoji are 2 characters but 4 UTF-16 units: over a limit of 3."""
    _, _, key = org_and_key
    _with_limits(carrier, monkeypatch, first_name_max=3, number_max=10)
    emoji = "\U0001f600" * 2
    resp = await _create(
        client,
        key,
        fields=json.dumps({"first_name": emoji, "last_name": "Lovelace"}),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "first_name"]
    assert carrier.drafts == []
    resp = await _create(
        client, key, fields=json.dumps({"first_name": "\U0001f600", "last_name": "L"})
    )
    assert resp.status_code == 201, resp.text


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
    assert got["state"] == "submitted" and got["cancellable"] is False
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
    client, org_and_key, carrier, async_session, monkeypatch
) -> None:
    decided: list[str] = []
    monkeypatch.setattr(
        verification_decisions, "notify_verification_decided", decided.append
    )
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
    assert decided == [vid]
    # rejected does not block starting again
    app.dependency_overrides.pop(require_superadmin)
    assert (await _create(client, key)).status_code == 201


async def test_status_is_pulled_from_the_carrier_after_submit(
    client, org_and_key, carrier, monkeypatch
) -> None:
    decided: list[str] = []
    monkeypatch.setattr(
        verification_decisions, "notify_verification_decided", decided.append
    )
    _, _, key = org_and_key
    vid = (await _create(client, key)).json()["id"]
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    await client.post(f"/admin/verifications/{vid}/approve", headers=_auth(key))

    assert (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()[
        "state"
    ] == "submitted"
    assert decided == []
    carrier.remote_status = ProviderStatus(state="approved")
    got = (await client.get(f"/verifications/{vid}", headers=_auth(key))).json()
    assert got["state"] == "approved" and got["approved_at"]
    # The owners are emailed once: an approved row is not polled again.
    verifications_routes._last_polled.clear()
    await client.get("/verifications", headers=_auth(key))
    assert decided == [vid]


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
    assert counts == {"submitted": 0, "refreshed": 0, "forgotten": 0}
    assert carrier.submitted == []
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
    client, org_and_key, carrier, async_session, monkeypatch
) -> None:
    from hailhq.api.routes.verifications import sweep_verifications

    decided: list[str] = []
    monkeypatch.setattr(
        verification_decisions, "notify_verification_decided", decided.append
    )
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
    assert decided == [vid]
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
    assert counts == {"submitted": 0, "refreshed": 0, "forgotten": 0}
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "awaiting_review" and row.provider_refs == {"bundle_sid": "B1"}
    assert (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "verification.reject")
        )
    ).scalar_one_or_none() is None


async def test_a_check_that_fails_partway_keeps_only_the_ids_it_added(
    client, org_and_key, carrier, async_session
) -> None:
    from hailhq.api.routes.verifications import sweep_verifications

    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    await _set_state(async_session, vid, "awaiting_review", age_s=3 * 60)

    async def check(refs):
        refs["filed_id"] = "F1"
        refs.pop("bundle_sid")
        raise RuntimeError("carrier down")

    carrier.check = check
    await sweep_verifications(
        async_session,
        lambda name, purpose="number": carrier if name == "fake" else None,
    )
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.provider_refs == {"bundle_sid": "B1", "filed_id": "F1"}


async def test_a_discard_that_fails_partway_keeps_the_ids_it_added(
    client, org_and_key, carrier, async_session
) -> None:
    from hailhq.api.routes.verifications import sweep_verifications

    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    await _set_state(async_session, vid, "awaiting_review", age_s=3 * 60)
    carrier.check_problems = [Problem(field="", message="document unreadable")]

    async def discard(refs):
        refs["deleting_id"] = "D1"
        refs["bundle_sid"] = "changed"
        raise RuntimeError("carrier down")

    carrier.discard = discard
    await sweep_verifications(
        async_session,
        lambda name, purpose="number": carrier if name == "fake" else None,
    )
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "awaiting_review"
    assert row.provider_refs == {"bundle_sid": "B1", "deleting_id": "D1"}


@pytest.mark.parametrize(
    "fail", [RuntimeError("carrier down"), asyncio.CancelledError()]
)
async def test_ids_check_wrote_survive_a_failed_or_cut_discard(
    client, org_and_key, carrier, async_session, fail
) -> None:
    from hailhq.api.routes.verifications import sweep_verifications

    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    await _set_state(async_session, vid, "awaiting_review", age_s=3 * 60)

    async def check(refs):
        # A plug-in may file (and be billed) inside check(), in place.
        refs["filed_id"] = "F1"
        return [Problem(field="", message="document unreadable")]

    async def boom(refs):
        refs.pop("bundle_sid")  # half done
        raise fail

    carrier.check, carrier.discard = check, boom
    registry = lambda name, purpose="number": carrier if name == "fake" else None
    if isinstance(fail, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await sweep_verifications(async_session, registry)
        await async_session.rollback()
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    if not isinstance(fail, asyncio.CancelledError):
        await sweep_verifications(async_session, registry)
        # The row in memory matches the saved one, not discard()'s half.
        assert row.provider_refs == {"bundle_sid": "B1", "filed_id": "F1"}
    await async_session.refresh(row)
    assert row.state == "awaiting_review"
    assert row.provider_refs == {"bundle_sid": "B1", "filed_id": "F1"}


async def test_a_refused_submit_discards_the_refs_as_they_are_now(
    client, org_and_key, carrier, async_session, session_factory
) -> None:
    """submit() wrote nothing; another writer changed the refs meanwhile.
    The discard gets the new refs, and the old ones are not written back."""
    from hailhq.api.routes.verifications import sweep_verifications

    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    await _set_state(async_session, vid, "awaiting_review", age_s=3 * 60)

    async def refuse(refs):
        async with session_factory() as other:
            row = await other.get(CarrierVerification, uuid.UUID(vid))
            row.provider_refs = {**row.provider_refs, "campaign_id": "C9"}
            await other.commit()
        raise SubmitRefused([Problem(field="", message=NOT_ACCEPTED)])

    carrier.submit = refuse
    await sweep_verifications(
        async_session,
        lambda name, purpose="number": carrier if name == "fake" else None,
    )
    assert carrier.discarded == [{"bundle_sid": "B1", "campaign_id": "C9"}]


@pytest.mark.parametrize(
    "fail", [RuntimeError("carrier down"), asyncio.CancelledError()]
)
async def test_ids_submit_wrote_survive_a_failed_or_cut_discard(
    client, org_and_key, carrier, async_session, fail
) -> None:
    """A refused submit that billed something keeps its id when the discard
    fails, and when the process stops during the discard (a restart): the
    id is saved before the discard starts."""
    from hailhq.api.routes.verifications import sweep_verifications

    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    await _set_state(async_session, vid, "awaiting_review", age_s=3 * 60)

    async def refuse(refs):
        refs["filed_id"] = "F1"
        raise SubmitRefused([Problem(field="", message=NOT_ACCEPTED)])

    async def stop(refs):
        raise fail

    carrier.submit, carrier.discard = refuse, stop
    registry = lambda name, purpose="number": carrier if name == "fake" else None
    if isinstance(fail, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await sweep_verifications(async_session, registry)
        await async_session.rollback()
    else:
        await sweep_verifications(async_session, registry)
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.provider_refs == {"bundle_sid": "B1", "filed_id": "F1"}
    # A failed discard waits for the next try; a cut one is left
    # 'submitting', for _refresh to settle.
    cut_off = isinstance(fail, asyncio.CancelledError)
    assert row.state == ("submitting" if cut_off else "awaiting_review")


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


@pytest.mark.usefixtures("us_number")
async def test_an_sms_link_over_512_characters_is_refused(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    from hailhq.core.providers.verification.sms_form import sms_requirements

    async def requirements(country_code, number_type, subject_type):
        return sms_requirements(sms_carrier, subject_type)

    monkeypatch.setattr(sms_carrier, "requirements", requirements)
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    long = "https://acme.example/" + "a" * 600
    resp = await _post_sms(
        client, key, fields=json.dumps({"legal_name": "Acme", "terms_url": long})
    )
    assert resp.status_code == 422, resp.text
    assert [(d["loc"], d["msg"]) for d in resp.json()["detail"]] == [
        (["body", "terms_url"], "Use at most 512 characters.")
    ]
    assert sms_carrier.drafts == []


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
async def test_sms_registration_takes_no_money_and_pings_the_rater(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    pinged: list[str] = []
    monkeypatch.setattr(
        verifications_routes, "notify_sms_registration_changed", pinged.append
    )
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    resp = await _post_sms(client, key)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["purpose"] == "sms" and body["state"] == "submitted"
    # Prices live in the website: the API writes no ledger row.
    assert await _balance(async_session, org_id) == 10_000
    assert pinged == [body["id"]]


@pytest.mark.usefixtures("us_number")
async def test_a_brand_billed_then_refused_is_kept_as_rejected(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    pinged: list[str] = []
    monkeypatch.setattr(
        verifications_routes, "notify_sms_registration_changed", pinged.append
    )
    decided: list[str] = []
    monkeypatch.setattr(
        verification_decisions, "notify_verification_decided", decided.append
    )
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 20_000)
    sms_carrier.problems = [Problem(field="rep_phone", message="Use another one.")]
    sms_carrier.billed_refs = {"brand_id": "B7", "subject_type": "business"}
    resp = await _post_sms(client, key)
    assert resp.status_code == 422, resp.text
    row = (
        await async_session.execute(
            select(CarrierVerification).where(
                CarrierVerification.organization_id == org_id
            )
        )
    ).scalar_one()
    assert row.state == "rejected" and row.provider_refs["brand_id"] == "B7"
    assert row.provider_refs["refused_at_filing"] is True
    assert row.rejection_reason == "Use another one."
    assert pinged == [str(row.id)]
    assert decided == [str(row.id)]
    # A rejected row never blocks a new filing.
    sms_carrier.problems = []
    sms_carrier.billed_refs = {}
    assert (await _post_sms(client, key)).status_code == 201


async def _set_refs(async_session, vid: str, state: str, refs: dict) -> None:
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    row.state, row.provider_refs = state, refs
    await async_session.commit()


@pytest.mark.usefixtures("us_number")
async def test_a_dismissed_filing_hands_its_kept_brand_to_the_next_one(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 20_000)

    async def keep(refs):
        return {"kept_brand_id": "B9"}

    monkeypatch.setattr(sms_carrier, "discard_rejected", keep)
    vid = (await _post_sms(client, key)).json()["id"]
    await _set_refs(async_session, vid, "rejected", {"brand_reused": True})
    monkeypatch.setattr(sms_carrier, "keeps_brand", lambda refs: True, raising=False)
    before = await client.get("/verifications", headers=_auth(key))
    assert [v["identity_confirmed"] for v in before.json()] == [True]
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200 and resp.json()["state"] == "cancelled"
    async_session.expire_all()
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    # The rater still reads the mark after the wipe.
    assert row.provider_refs == {"brand_reused": True, "kept_brand_id": "B9"}
    listed = await client.get("/verifications", headers=_auth(key))
    assert [v["identity_confirmed"] for v in listed.json()] == [True]
    # A kept brand costs nothing: the filing needs the campaign review alone.
    review = sms_registration_fees.filing_fee_cents("fake", "business", brand_kept=True)
    await _set_balance(async_session, org_id, review)
    second = await _post_sms(client, key)
    assert second.status_code == 201
    await _set_balance(async_session, org_id, 20_000)
    assert sms_carrier.drafts[-1]["kept_brand_id"] == "B9"
    # The newer filing owns the brand now: a later one does not get it.
    await _set_refs(async_session, second.json()["id"], "rejected", {})
    assert (await _post_sms(client, key)).status_code == 201
    assert "kept_brand_id" not in sms_carrier.drafts[-1]


@pytest.mark.usefixtures("us_number")
async def test_a_reused_brand_is_not_an_unsettled_filing(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    floor = sms_registration_fees.filing_fee_cents("fake", "business")
    await _set_balance(async_session, org_id, floor + 1)
    vid = (await _post_sms(client, key)).json()["id"]
    await _set_refs(async_session, vid, "rejected", {"brand_reused": True})
    # The balance covers one filing: the reused one owes no submission.
    assert (await _post_sms(client, key)).status_code == 201


@pytest.mark.usefixtures("us_number")
async def test_a_lost_race_leaves_a_reused_brand_alone(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 20_000)
    winner = await _post_sms(client, key)
    assert winner.status_code == 201
    live = await async_session.get(CarrierVerification, uuid.UUID(winner.json()["id"]))
    live.state = "rejected"
    await async_session.commit()
    original = sms_carrier.create_draft

    async def racing(**kwargs):
        draft = await original(**kwargs)
        draft.refs["brand_reused"] = True
        # The other request commits while this one is at the carrier.
        live.state = "submitted"
        await async_session.commit()
        return draft

    monkeypatch.setattr(sms_carrier, "create_draft", racing)
    sms_carrier.discarded.clear()
    assert (await _post_sms(client, key)).status_code == 409
    assert sms_carrier.discarded == []


@pytest.mark.usefixtures("us_number")
async def test_cancelling_an_unsent_filing_keeps_no_brand(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 20_000)
    vid = (await _post_sms(client, key)).json()["id"]
    await _set_refs(async_session, vid, "awaiting_review", {"brand_sid": "BN1"})
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200
    assert sms_carrier.discarded == [{"brand_sid": "BN1"}]
    assert (await _post_sms(client, key)).status_code == 201
    assert "kept_brand_id" not in sms_carrier.drafts[-1]


@pytest.mark.usefixtures("us_number")
async def test_a_billed_refusal_with_a_campaign_carries_the_marker(
    client, org_and_key, sms_carrier, async_session
) -> None:
    """A carrier that files and bills a campaign at create, then refuses:
    the saved row says the campaign was filed, so its review is charged."""
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 20_000)
    sms_carrier.campaign_key = "campaign_xyz"
    sms_carrier.problems = [Problem(field="rep_phone", message="Use another one.")]
    sms_carrier.billed_refs = {"brand_id": "B7", "campaign_xyz": "C7"}
    resp = await _post_sms(client, key)
    assert resp.status_code == 422, resp.text
    row = (
        await async_session.execute(
            select(CarrierVerification).where(
                CarrierVerification.organization_id == org_id
            )
        )
    ).scalar_one()
    assert row.state == "rejected" and row.provider_refs["campaign_xyz"] == "C7"
    assert row.provider_refs["campaign_filed_at"]


@pytest.mark.usefixtures("us_number")
async def test_a_refusal_before_any_brand_keeps_nothing(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    sms_carrier.problems = [Problem(field="ein", message="Check the tax ID (EIN).")]
    resp = await _post_sms(client, key)
    assert resp.status_code == 422, resp.text
    rows = (
        (
            await async_session.execute(
                select(CarrierVerification).where(
                    CarrierVerification.organization_id == org_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def _kept_brand_filing(client, key, sms_carrier, async_session, monkeypatch):
    """Leave a cancelled filing with a kept brand, and return its row id."""

    async def keep(refs):
        return {"kept_brand_id": "B9"}

    monkeypatch.setattr(sms_carrier, "discard_rejected", keep)
    vid = (await _post_sms(client, key)).json()["id"]
    await _set_refs(async_session, vid, "rejected", {"brand_reused": True})
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200 and resp.json()["state"] == "cancelled"
    return vid


@pytest.mark.usefixtures("us_number")
@pytest.mark.parametrize("extra, funded", [(0, False), (None, True)])
async def test_a_kept_brand_filing_says_if_credit_covers_a_new_brand(
    client, org_and_key, sms_carrier, async_session, monkeypatch, extra, funded
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 20_000)
    await _kept_brand_filing(client, key, sms_carrier, async_session, monkeypatch)
    floor = sms_registration_fees.filing_fee_cents("fake", "business", brand_kept=True)
    full = sms_registration_fees.filing_fee_cents("fake", "business")
    await _set_balance(async_session, org_id, full if extra is None else floor + extra)
    assert (await _post_sms(client, key)).status_code == 201
    assert sms_carrier.drafts[-1]["kept_brand_id"] == "B9"
    assert sms_carrier.drafts[-1]["new_brand_funded"] is funded


@pytest.mark.usefixtures("us_number")
async def test_an_uncharged_filing_counts_against_a_new_brand(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 20_000)
    vid = await _kept_brand_filing(client, key, sms_carrier, async_session, monkeypatch)
    kept = await async_session.get(CarrierVerification, uuid.UUID(vid))
    # A person's filing the website has not charged yet. It leaves the
    # company's kept brand as the last company filing.
    async_session.add(
        CarrierVerification(
            organization_id=org_id,
            provider=kept.provider,
            purpose="sms",
            country_code=kept.country_code,
            number_type=kept.number_type,
            subject_type="person",
            state="rejected",
            provider_refs={},
            requirements_version=kept.requirements_version,
        )
    )
    await async_session.commit()
    floor = sms_registration_fees.filing_fee_cents("fake", "business", brand_kept=True)
    full = sms_registration_fees.filing_fee_cents("fake", "business")
    # Covers the reused price and the uncharged filing, but not a new brand too.
    await _set_balance(async_session, org_id, full + floor)
    first = await _post_sms(client, key)
    assert first.status_code == 201, first.text
    assert sms_carrier.drafts[-1]["new_brand_funded"] is False
    await _set_refs(
        async_session,
        first.json()["id"],
        "cancelled",
        {"brand_reused": True, "kept_brand_id": "B9"},
    )
    await _set_balance(async_session, org_id, full * 2)
    assert (await _post_sms(client, key)).status_code == 201
    assert sms_carrier.drafts[-1]["new_brand_funded"] is True


@pytest.mark.usefixtures("us_number")
async def test_a_new_brand_without_credit_returns_402_and_files_nothing(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 20_000)
    await _kept_brand_filing(client, key, sms_carrier, async_session, monkeypatch)

    async def stop(**kwargs):
        assert kwargs["new_brand_funded"] is False
        return DraftResult(refs={}, needs_new_brand_credit=True)

    monkeypatch.setattr(sms_carrier, "create_draft", stop)
    floor = sms_registration_fees.filing_fee_cents("fake", "business", brand_kept=True)
    await _set_balance(async_session, org_id, floor)
    resp = await _post_sms(client, key)
    assert resp.status_code == 402, resp.text
    assert resp.json()["detail"] == (
        "The details changed or cannot be reused, so a submission fee applies. "
        f"Add credit at {BILLING_URL} and file again."
    )
    live = await client.get("/verifications", headers=_auth(key))
    assert [v["state"] for v in live.json()] == ["cancelled"]


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
async def test_sms_registration_rejected_brand_is_audited(
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
    # The API takes no money; the website rater charges per step.
    assert await _balance(async_session, org_id) == 10_000


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


async def test_enable_sms_tries_a_refused_number_during_its_wait(
    client, org_and_key, sms_carrier, async_session, sms_mock, monkeypatch
) -> None:
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
    attach = sms_carrier.attach_number
    attempts = 0

    async def refused(refs, *, provider_resource_id, e164):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("carrier refused")

    monkeypatch.setattr(sms_carrier, "attach_number", refused)
    for expected in (1, 2):
        resp = await client.post(f"/numbers/{number.id}/enable-sms", headers=_auth(key))
        assert resp.status_code == 200, resp.text
        assert attempts == expected
    await async_session.refresh(number)
    assert number.sms_registration_id is None
    assert number.provisioning_metadata["sms_attach_refused"]["wait_hours"] == 2
    assert len(await _audits(async_session, "verification.sms_attach_refused")) == 1

    monkeypatch.setattr(sms_carrier, "attach_number", attach)
    resp = await client.post(f"/numbers/{number.id}/enable-sms", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    await async_session.refresh(number)
    assert number.sms_registration_id is not None
    assert "sms_attach_refused" not in number.provisioning_metadata


async def test_a_failed_reattach_of_an_attached_number_is_not_a_refusal(
    client, org_and_key, sms_carrier, async_session, sms_mock, monkeypatch
) -> None:
    from hailhq.api.routes import numbers as numbers_routes

    org_id, _, key = org_and_key
    number = await _us_number(async_session, org_id, provider="twilio")
    row = CarrierVerification(
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
    async_session.add(row)
    await async_session.flush()
    number.sms_registration_id = row.id
    await async_session.commit()
    monkeypatch.setattr(
        numbers_routes,
        "get_verification_provider",
        lambda name, purpose="number": sms_carrier if purpose == "sms" else None,
    )
    sms_carrier.attach_error = RuntimeError("carrier down")

    resp = await client.post(f"/numbers/{number.id}/enable-sms", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    await async_session.refresh(number)
    assert number.sms_registration_id == row.id
    assert "sms_attach_refused" not in number.provisioning_metadata
    assert await _audits(async_session, "verification.sms_attach_refused") == []


async def test_attach_reads_the_retry_state_under_the_lock(
    org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    # The request loaded the number, then the background pass refused it.
    org_id, _, _ = org_and_key
    number = await _us_number(async_session, org_id, provider="twilio")
    row = CarrierVerification(
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
    async_session.add(row)
    await async_session.commit()
    state = {
        "registration_id": str(row.id),
        "wait_hours": 1,
        "retry_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "reason_hash": _reason_hash("carrier refused"),
    }
    async with session_scope() as other:
        await other.execute(
            update(PhoneNumber)
            .where(PhoneNumber.id == number.id)
            .values(provisioning_metadata={"sms_attach_refused": state, "kept": 1})
        )
        await other.commit()
    sms_carrier.attach_error = RuntimeError("carrier refused")

    await sms_registration.attach_if_registered(
        async_session, number, lambda name, purpose="number": sms_carrier
    )
    await async_session.commit()

    await async_session.refresh(number)
    assert number.provisioning_metadata["kept"] == 1
    assert number.provisioning_metadata["sms_attach_refused"]["wait_hours"] == 2
    assert await _audits(async_session, "verification.sms_attach_refused") == []


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
    client, org_and_key, sms_carrier, async_session, campaign_key, via, monkeypatch
) -> None:
    # The plug-ins create the campaign inside status() and write its id into
    # refs in place. The id must reach the database, or every later poll
    # creates (and pays for) another campaign.
    decided: list[str] = []
    monkeypatch.setattr(
        verification_decisions, "notify_verification_decided", decided.append
    )
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    vid = (await _post_sms(client, key)).json()["id"]
    sms_carrier.campaign_key = campaign_key
    sms_carrier.remote_status = ProviderStatus(state="pending")
    url = f"/verifications/{vid}" if via == "get" else "/verifications"
    stamps = []
    for _ in range(2):
        verifications_routes._last_polled.clear()
        resp = await client.get(url, headers=_auth(key))
        assert resp.status_code == 200, resp.text
        refs = await _stored_refs(async_session, vid)
        assert refs[campaign_key] == "QE1"
        stamps.append(refs["campaign_filed_at"])
    assert sms_carrier.campaigns_created == 1
    # One key for billing whatever the carrier's own key, set once.
    assert stamps[0] and stamps[0] == stamps[1]
    # A new campaign id is not a decision: nobody is emailed.
    assert decided == []


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


# -- credit for the campaign step ------------------------------------------


def _campaign_fee() -> int:
    return sms_registration_fees.campaign_fee_cents("fake", "business")


async def _charge(async_session, org_id, vid, step: str, cents: int) -> None:
    """The website rater's debit for one step of a registration."""
    async_session.add(
        AccountCredit(
            organization_id=org_id,
            kind="debit",
            channel="sms",
            amount_cents=-cents,
            qty=1,
            ref=f"sms_registration:{vid}:{step}",
            source=f"sms_registration_{step}",
        )
    )
    await async_session.commit()


async def _charge_submission(async_session, org_id, vid) -> None:
    full = sms_registration_fees.filing_fee_cents("fake", "business")
    await _charge(async_session, org_id, vid, "submission", full - _campaign_fee())


async def _before_campaign(
    client, key, async_session, org_id, sms_carrier, charged=True
) -> str:
    """A submitted US texting registration whose campaign is not filed yet:
    the next poll files it (a billed step). ``charged``: the rater has
    charged its submission."""
    await _set_balance(async_session, org_id, 10_000)
    resp = await _post_sms(client, key)
    assert resp.status_code == 201, resp.text
    vid = resp.json()["id"]
    if charged:
        await _charge_submission(async_session, org_id, vid)
    sms_carrier.campaign_key = "campaign_id"
    return vid


async def _stored_state(async_session, vid: str) -> str:
    return (
        await async_session.execute(
            select(CarrierVerification.state).where(
                CarrierVerification.id == uuid.UUID(vid)
            )
        )
    ).scalar_one()


@pytest.mark.usefixtures("us_number")
@pytest.mark.parametrize("via", ["get", "list", "sweep"])
async def test_campaign_step_waits_without_credit(
    client, org_and_key, sms_carrier, async_session, via
) -> None:
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    await _set_balance(async_session, org_id, _campaign_fee() - 1)
    refs = await _stored_refs(async_session, vid)
    verifications_routes._last_polled.clear()
    if via == "sweep":
        await verifications_routes.sweep_verifications(
            async_session, _sms_registry(sms_carrier)
        )
    else:
        url = f"/verifications/{vid}" if via == "get" else "/verifications"
        resp = await client.get(url, headers=_auth(key))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if via == "list":
            body = next(v for v in body if v["id"] == vid)
        assert body["state"] == "submitted"
        assert body["waiting_for_credit"] is True
    # The poll ran, without the campaign call: nothing filed, refs kept.
    assert sms_carrier.status_calls == 1
    assert sms_carrier.campaigns_skipped == 1
    assert sms_carrier.campaigns_created == 0
    assert await _stored_refs(async_session, vid) == refs
    assert await _stored_state(async_session, vid) == "submitted"
    # The next poll after a top-up files the campaign.
    await _set_balance(async_session, org_id, _campaign_fee())
    verifications_routes._last_polled.clear()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert sms_carrier.campaigns_created == 1
    assert resp.json()["waiting_for_credit"] is False


@pytest.mark.usefixtures("us_number")
@pytest.mark.parametrize("via", ["get", "list"])
async def test_campaign_step_runs_when_funded(
    client, org_and_key, sms_carrier, async_session, via
) -> None:
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    await _set_balance(async_session, org_id, _campaign_fee())
    verifications_routes._last_polled.clear()
    url = f"/verifications/{vid}" if via == "get" else "/verifications"
    resp = await client.get(url, headers=_auth(key))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    if via == "list":
        body = next(v for v in body if v["id"] == vid)
    assert sms_carrier.campaigns_created == 1
    assert (await _stored_refs(async_session, vid))["campaign_id"] == "QE1"
    assert body["waiting_for_credit"] is False
    # Once the campaign is filed there is no step left to pay for.
    await _set_balance(async_session, org_id, 0)
    verifications_routes._last_polled.clear()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is False
    assert sms_carrier.status_calls == 2


@pytest.mark.usefixtures("us_number")
async def test_an_uncharged_submission_counts_against_the_campaign(
    client, org_and_key, sms_carrier, async_session
) -> None:
    # The rater has not charged the filing yet: the balance still holds
    # the brand's cost, so it cannot pay for the campaign too.
    org_id, _, key = org_and_key
    vid = await _before_campaign(
        client, key, async_session, org_id, sms_carrier, charged=False
    )
    await _set_balance(async_session, org_id, _campaign_fee())
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is True
    assert sms_carrier.campaigns_created == 0
    await _charge_submission(async_session, org_id, vid)
    await _set_balance(async_session, org_id, _campaign_fee())
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is False
    assert sms_carrier.campaigns_created == 1


async def test_campaign_step_skips_the_credit_check_for_the_shared_key(
    client, sms_carrier, async_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "hail_api_key", "hail-test-shared-secret")
    await _us_number(async_session, SELF_HOSTED_ORG_ID)
    resp = await _post_sms(client, "hail-test-shared-secret")
    assert resp.status_code == 201, resp.text
    vid = resp.json()["id"]
    sms_carrier.campaign_key = "campaign_id"
    # The self-hosted org has no credit and is not billed.
    assert await _balance(async_session, SELF_HOSTED_ORG_ID) < _campaign_fee()
    resp = await client.get(
        f"/verifications/{vid}", headers=_auth("hail-test-shared-secret")
    )
    assert resp.status_code == 200, resp.text
    assert sms_carrier.campaigns_created == 1
    assert resp.json()["waiting_for_credit"] is False


@pytest.mark.usefixtures("us_number")
async def test_waiting_row_still_gets_a_brand_rejection(
    client, org_and_key, sms_carrier, async_session
) -> None:
    # Only the paid campaign filing waits for credit: the poll still runs
    # and applies the carrier's brand rejection.
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    await _set_balance(async_session, org_id, _campaign_fee() - 1)
    sms_carrier.remote_status = ProviderStatus(
        state="rejected", reason=BRAND_REJECTED, raw_reason="brand refused"
    )
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "rejected"
    assert resp.json()["rejection_reason"] == BRAND_REJECTED
    assert resp.json()["waiting_for_credit"] is False
    assert sms_carrier.campaigns_created == 0


@pytest.mark.usefixtures("us_number")
async def test_retry_submit_waits_without_credit(
    client, org_and_key, sms_carrier, async_session
) -> None:
    # The submit is free, so it goes through; the campaign waits.
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    await _charge_submission(async_session, org_id, vid)
    sms_carrier.campaign_key = "campaign_id"
    await _set_balance(async_session, org_id, _campaign_fee() - 1)
    counts = await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    assert counts["submitted"] == 1
    assert await _stored_state(async_session, vid) == "submitted"
    assert sms_carrier.campaigns_created == 0
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is True
    # The next poll after a top-up files the campaign.
    await _set_balance(async_session, org_id, _campaign_fee())
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is False
    assert sms_carrier.campaigns_created == 1


@pytest.mark.usefixtures("us_number")
async def test_admin_approve_waits_without_credit(
    client, org_and_key, sms_carrier, async_session
) -> None:
    # The approve submits; the response says the campaign waits for credit.
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    await _charge_submission(async_session, org_id, vid)
    sms_carrier.campaign_key = "campaign_id"
    await _set_balance(async_session, org_id, _campaign_fee() - 1)
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    try:
        resp = await client.post(
            f"/admin/verifications/{vid}/approve", headers=_auth(key)
        )
    finally:
        app.dependency_overrides.pop(require_superadmin, None)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "submitted"
    assert body["waiting_for_credit"] is True
    assert sms_carrier.campaigns_created == 0


async def test_one_credit_files_one_campaign(
    client, org_and_key, sms_carrier, async_session
) -> None:
    # Two registrations of one org, each before its campaign, and credit
    # for one campaign fee: one campaign is filed, the other waits, also on
    # a later poll before the rater has charged the first.
    org_id, _, key = org_and_key
    rows = [
        CarrierVerification(
            organization_id=org_id,
            provider="fake",
            purpose="sms",
            country_code="US",
            number_type=number_type,
            subject_type="business",
            state="submitted",
            provider_refs={"brand_sid": f"BN-{number_type}"},
            requirements_version="1",
        )
        for number_type in ("local", "toll_free")
    ]
    async_session.add_all(rows)
    await async_session.commit()
    for row in rows:
        await _charge_submission(async_session, org_id, row.id)
    sms_carrier.campaign_key = "campaign_id"
    await _set_balance(async_session, org_id, _campaign_fee())
    verifications_routes._last_polled.clear()
    resp = await client.get("/verifications?purpose=sms", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert sms_carrier.campaigns_created == 1
    by_id = {v["id"]: v for v in resp.json()}
    filed = [
        r for r in rows if "campaign_id" in await _stored_refs(async_session, str(r.id))
    ]
    assert len(filed) == 1
    other = next(r for r in rows if r is not filed[0])
    assert by_id[str(filed[0].id)]["waiting_for_credit"] is False
    assert by_id[str(other.id)]["waiting_for_credit"] is True
    # The first campaign is not charged yet: its fee still counts.
    resp = await client.get(f"/verifications/{other.id}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is True
    assert sms_carrier.campaigns_created == 1
    # Once the rater charges it and the org tops up, the other is filed.
    await _charge(async_session, org_id, filed[0].id, "review", _campaign_fee())
    await _set_balance(async_session, org_id, _campaign_fee())
    resp = await client.get(f"/verifications/{other.id}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is False
    assert sms_carrier.campaigns_created == 2


async def test_the_oldest_registration_gets_the_credit_first(
    client, org_and_key, sms_carrier, async_session
) -> None:
    # A poll of the newer registration alone leaves the credit to the older
    # one, as the list does.
    org_id, _, key = org_and_key
    now = datetime.now(timezone.utc)
    older, newer = (
        CarrierVerification(
            organization_id=org_id,
            provider="fake",
            purpose="sms",
            country_code="US",
            number_type=number_type,
            subject_type="business",
            state="submitted",
            provider_refs={"brand_sid": f"BN-{number_type}"},
            requirements_version="1",
            created_at=now - timedelta(days=age),
        )
        for number_type, age in (("local", 3), ("toll_free", 2))
    )
    async_session.add_all([older, newer])
    await async_session.commit()
    sms_carrier.campaign_key = "campaign_id"
    await _set_balance(async_session, org_id, _campaign_fee())
    resp = await client.get(f"/verifications/{newer.id}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is True
    assert sms_carrier.campaigns_created == 0
    resp = await client.get(f"/verifications/{older.id}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is False
    assert sms_carrier.campaigns_created == 1


@pytest.mark.usefixtures("us_number")
@pytest.mark.parametrize(
    "filed_at",
    [
        "not a time",
        "2026-11-07 pending",
        "2026-13-45T00:00:00",
        "2026-11-07T25:00:00",
        "2026-11-07T10:00:00+99:00",
    ],
)
async def test_a_campaign_time_that_is_not_a_time_does_not_fail_the_check(
    client, org_and_key, sms_carrier, async_session, filed_at
) -> None:
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    async_session.add(
        CarrierVerification(
            organization_id=org_id,
            provider="fake",
            purpose="sms",
            country_code="US",
            number_type="toll_free",
            subject_type="business",
            state="cancelled",
            # No submission owed, so only the bad time is in play.
            provider_refs={"campaign_filed_at": filed_at, "brand_reused": True},
            requirements_version="1",
        )
    )
    await async_session.commit()
    await _set_balance(async_session, org_id, _campaign_fee())
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert sms_carrier.campaigns_created == 1


@pytest.mark.usefixtures("us_number")
@pytest.mark.parametrize(
    "filed_at", ["2026-11-07", "2026-11-07T10:00+00:00", "2026-11-07T10:00:00Z"]
)
async def test_an_uncharged_campaign_in_any_iso_form_counts(
    client, org_and_key, sms_carrier, async_session, filed_at
) -> None:
    # Another registration's campaign, filed today and not charged yet,
    # still holds the credit, however its time is written.
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    async_session.add(
        CarrierVerification(
            organization_id=org_id,
            provider="fake",
            purpose="sms",
            country_code="US",
            number_type="toll_free",
            subject_type="business",
            state="cancelled",
            provider_refs={
                "campaign_filed_at": datetime.now(timezone.utc).date().isoformat()
                + filed_at[10:],
                "brand_reused": True,
            },
            requirements_version="1",
        )
    )
    await async_session.commit()
    await _set_balance(async_session, org_id, _campaign_fee())
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is True
    assert sms_carrier.campaigns_created == 0


async def test_a_campaign_in_flight_keeps_its_credit_when_the_order_flips(
    client, org_and_key, sms_carrier, async_session, session_factory
) -> None:
    # Credit for one fee. The newer row B is funded and its campaign call
    # is in flight (the org lock is released by then). Meanwhile the older
    # row A becomes ready (its hold is lifted), so by age A would come first.
    # B's reservation keeps the credit: A files nothing, one campaign in all.
    org_id, _, key = org_and_key
    now = datetime.now(timezone.utc)
    older, newer = (
        CarrierVerification(
            organization_id=org_id,
            provider="fake",
            purpose="sms",
            country_code="US",
            number_type=number_type,
            subject_type="business",
            state="submitted",
            provider_refs={
                "brand_sid": f"BN-{number_type}",
                "brand_reused": True,
                **extra,
            },
            requirements_version="1",
            created_at=now - timedelta(days=age),
        )
        for number_type, age, extra in (
            ("local", 3, {"needs_information": True}),
            ("toll_free", 2, {}),
        )
    )
    async_session.add_all([older, newer])
    await async_session.commit()
    sms_carrier.campaign_key = "campaign_id"
    sms_carrier.entered = asyncio.Event()
    sms_carrier.release = asyncio.Event()
    await _set_balance(async_session, org_id, _campaign_fee())
    restore = _per_request_sessions(session_factory)
    try:
        first = asyncio.create_task(
            client.get(f"/verifications/{newer.id}", headers=_auth(key))
        )
        await asyncio.wait_for(sms_carrier.entered.wait(), 5)
        refs = dict(older.provider_refs)
        refs.pop("needs_information")
        await async_session.execute(
            update(CarrierVerification)
            .where(CarrierVerification.id == older.id)
            .values(provider_refs=refs)
        )
        await async_session.commit()
        second = await asyncio.wait_for(
            client.get(f"/verifications/{older.id}", headers=_auth(key)), 5
        )
        assert second.status_code == 200, second.text
        assert second.json()["waiting_for_credit"] is True
        sms_carrier.release.set()
        assert (await first).status_code == 200
    finally:
        sms_carrier.release.set()
        restore()
    assert sms_carrier.campaigns_created == 1
    assert "campaign_id" in await _stored_refs(async_session, str(newer.id))
    assert "campaign_id" not in await _stored_refs(async_session, str(older.id))


@pytest.mark.usefixtures("us_number")
@pytest.mark.parametrize("via", ["get", "list"])
async def test_a_row_that_needs_the_check_only_after_the_reread_files_nothing(
    client, org_and_key, sms_carrier, async_session, monkeypatch, via
) -> None:
    # Read with a campaign stamp (no check needed), then re-read under the
    # row lock without it: it got no credit check, so no campaign (fail
    # closed), although the balance is 0.
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    refs = await _stored_refs(async_session, vid)
    stamped = {**refs, "campaign_filed_at": datetime.now(timezone.utc).isoformat()}
    await async_session.execute(
        update(CarrierVerification)
        .where(CarrierVerification.id == uuid.UUID(vid))
        .values(provider_refs=stamped)
    )
    await async_session.commit()
    await _set_balance(async_session, org_id, 0)
    real = verifications_routes._lock_for_poll

    async def lock_after_a_change(db, row):
        await db.execute(
            update(CarrierVerification)
            .where(CarrierVerification.id == row.id)
            .values(provider_refs=refs)
        )
        return await real(db, row)

    monkeypatch.setattr(verifications_routes, "_lock_for_poll", lock_after_a_change)
    verifications_routes._last_polled.clear()
    url = f"/verifications/{vid}" if via == "get" else "/verifications"
    resp = await client.get(url, headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert sms_carrier.status_calls == 1
    assert sms_carrier.campaigns_created == 0
    assert "campaign_id" not in await _stored_refs(async_session, vid)


async def test_a_poll_that_files_nothing_gives_up_its_claim(
    client, org_and_key, sms_carrier, async_session
) -> None:
    # Credit for one fee. The newer row X is polled while funded, but its
    # brand is not ready (no campaign filed): the poll clears its
    # reservation, so the older row Y, ready later, gets the credit.
    org_id, _, key = org_and_key
    now = datetime.now(timezone.utc)
    older, newer = (
        CarrierVerification(
            organization_id=org_id,
            provider="fake",
            purpose="sms",
            country_code="US",
            number_type=number_type,
            subject_type="business",
            state="submitted",
            provider_refs={
                "brand_sid": f"BN-{number_type}",
                "brand_reused": True,
                **extra,
            },
            requirements_version="1",
            created_at=now - timedelta(days=age),
        )
        for number_type, age, extra in (
            ("local", 3, {"needs_information": True}),
            ("toll_free", 2, {}),
        )
    )
    async_session.add_all([older, newer])
    await async_session.commit()
    await _set_balance(async_session, org_id, _campaign_fee())
    # No campaign_key: the fake files no campaign (its brand is pending).
    resp = await client.get(f"/verifications/{newer.id}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is False
    assert sms_carrier.status_calls == 1
    assert "campaign_reserved_at" not in await _stored_refs(
        async_session, str(newer.id)
    )
    refs = dict(older.provider_refs)
    refs.pop("needs_information")
    await async_session.execute(
        update(CarrierVerification)
        .where(CarrierVerification.id == older.id)
        .values(provider_refs=refs)
    )
    await async_session.commit()
    resp = await client.get(f"/verifications/{older.id}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is False
    resp = await client.get(f"/verifications/{newer.id}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is True


@pytest.mark.usefixtures("us_number")
async def test_a_failed_campaign_call_keeps_its_claim(
    client, org_and_key, sms_carrier, async_session, pinged
) -> None:
    # The carrier call raises after it may have filed the campaign: the
    # reservation stays (the next poll may find that campaign), and the
    # billing service is not told about a change that is not billed.
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    await _set_balance(async_session, org_id, _campaign_fee())

    async def status(refs, *, may_file_campaign=True):
        assert may_file_campaign
        raise RuntimeError("timed out")

    sms_carrier.status = status
    pinged.clear()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert "campaign_reserved_at" in await _stored_refs(async_session, vid)
    assert pinged == []


@pytest.mark.usefixtures("us_number")
async def test_a_kept_claim_is_not_renewed_and_runs_out(
    client, org_and_key, sms_carrier, async_session
) -> None:
    # A claim kept after a failed call keeps its first time on later polls,
    # so it runs out; a successful poll then drops it.
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    await _set_balance(async_session, org_id, _campaign_fee())
    real = sms_carrier.status

    async def failing(refs, *, may_file_campaign=True):
        raise RuntimeError("timed out")

    sms_carrier.status = failing
    await client.get(f"/verifications/{vid}", headers=_auth(key))
    first = (await _stored_refs(async_session, vid))["campaign_reserved_at"]
    await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert (await _stored_refs(async_session, vid))["campaign_reserved_at"] == first
    # The kept claim is another poll's: the next poll files nothing (the
    # carrier's answer shows no campaign) and drops it; the one after files.
    sms_carrier.status = real
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    refs = await _stored_refs(async_session, vid)
    assert "campaign_reserved_at" not in refs and "campaign_id" not in refs
    await client.get(f"/verifications/{vid}", headers=_auth(key))
    refs = await _stored_refs(async_session, vid)
    assert "campaign_reserved_at" not in refs and refs["campaign_id"] == "QE1"


async def _reserved(async_session, vid: str):
    return (await _stored_refs(async_session, vid)).get("campaign_reserved_at")


@pytest.mark.usefixtures("us_number")
async def test_a_poll_that_loses_the_row_drops_its_claim(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    # The poll reserved the credit, then did not get the row (it was free
    # again by the drop): no call, so the claim goes.
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    await _set_balance(async_session, org_id, _campaign_fee())
    seen = []

    async def lost(db, row):
        seen.append(await _reserved(db, str(row.id)))
        return False

    monkeypatch.setattr(verifications_routes, "_lock_for_poll", lost)
    await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert seen and seen[0] is not None  # it was reserved
    assert await _reserved(async_session, vid) is None
    assert sms_carrier.status_calls == 0


@pytest.mark.usefixtures("us_number")
async def test_a_poll_cannot_drop_another_polls_claim(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    # Between this poll's reservation and its drop, another poll kept its
    # own claim on the row (a failed call). This poll drops only its own.
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    await _set_balance(async_session, org_id, _campaign_fee())
    other = "2026-01-01T00:00:00+00:00"

    async def lost(db, row):
        refs = await _stored_refs(db, str(row.id))
        await db.execute(
            update(CarrierVerification)
            .where(CarrierVerification.id == row.id)
            .values(provider_refs={**refs, "campaign_reserved_at": other})
        )
        return False

    monkeypatch.setattr(verifications_routes, "_lock_for_poll", lost)
    await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert await _reserved(async_session, vid) == other


@pytest.mark.usefixtures("us_number")
async def test_a_row_no_longer_pollable_drops_any_claim(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    # Re-read under its lock, the row is on hold: there is no campaign to
    # claim credit for, so any reservation goes, its own or one kept.
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    await _set_balance(async_session, org_id, _campaign_fee())
    real = verifications_routes._lock_for_poll

    async def put_on_hold(db, row):
        refs = await _stored_refs(db, str(row.id))
        await db.execute(
            update(CarrierVerification)
            .where(CarrierVerification.id == row.id)
            .values(
                provider_refs={
                    **refs,
                    "needs_information": True,
                    "campaign_reserved_at": "2026-01-01T00:00:00+00:00",
                }
            )
        )
        return await real(db, row)

    monkeypatch.setattr(verifications_routes, "_lock_for_poll", put_on_hold)
    await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert await _reserved(async_session, vid) is None
    assert sms_carrier.status_calls == 0


@pytest.mark.usefixtures("us_number")
async def test_a_poll_that_loses_the_row_keeps_a_kept_claim(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    # The row carries a claim another poll kept after a failed call. This
    # poll files nothing on it, and losing the row it drops nothing.
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    await _set_balance(async_session, org_id, _campaign_fee())
    kept = datetime.now(timezone.utc).isoformat()
    refs = await _stored_refs(async_session, vid)
    await async_session.execute(
        update(CarrierVerification)
        .where(CarrierVerification.id == uuid.UUID(vid))
        .values(provider_refs={**refs, "campaign_reserved_at": kept})
    )
    await async_session.commit()

    async def lost(db, row):
        return False

    monkeypatch.setattr(verifications_routes, "_lock_for_poll", lost)
    await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert await _reserved(async_session, vid) == kept


@pytest.mark.usefixtures("us_number")
async def test_a_claim_older_than_its_time_holds_nothing(
    client, org_and_key, sms_carrier, async_session
) -> None:
    # A claim left past _CAMPAIGN_RESERVED_FOR (a process that died) does
    # not keep the credit from the row that is next.
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    await _set_balance(async_session, org_id, _campaign_fee())
    old = (
        datetime.now(timezone.utc)
        - verifications_routes._CAMPAIGN_RESERVED_FOR
        - timedelta(minutes=1)
    ).isoformat()
    async_session.add(
        CarrierVerification(
            organization_id=org_id,
            provider="fake",
            purpose="sms",
            country_code="US",
            number_type="toll_free",
            subject_type="business",
            state="submitted",
            provider_refs={
                "brand_sid": "BN-old",
                "brand_reused": True,
                "campaign_reserved_at": old,
            },
            requirements_version="1",
        )
    )
    await async_session.commit()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.json()["waiting_for_credit"] is False
    assert sms_carrier.campaigns_created == 1


@pytest.mark.usefixtures("us_number")
async def test_a_filed_campaign_poll_takes_no_org_lock(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    # Once the campaign is filed there is no credit to check: its polls do
    # not compete with billing writes for the organization's lock.
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    await _set_balance(async_session, org_id, _campaign_fee())
    await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert sms_carrier.campaigns_created == 1
    locks = []

    async def no_lock(db, org):
        locks.append(org)
        return False

    monkeypatch.setattr(verifications_routes, "try_org_lock", no_lock)
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert locks == []
    assert sms_carrier.status_calls == 2
    assert sms_carrier.campaigns_created == 1
    assert resp.json()["waiting_for_credit"] is False


@pytest.mark.usefixtures("us_number")
async def test_a_poll_before_the_campaign_files_nothing_when_the_org_lock_is_busy(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    # The other side of the test above: a row before its campaign asks for
    # the lock; when it is busy, the poll runs and files nothing.
    org_id, _, key = org_and_key
    vid = await _before_campaign(client, key, async_session, org_id, sms_carrier)
    await _set_balance(async_session, org_id, _campaign_fee())
    locks = []

    async def busy(db, org):
        locks.append(org)
        return False

    monkeypatch.setattr(verifications_routes, "try_org_lock", busy)
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert locks == [org_id]
    assert sms_carrier.status_calls == 1
    assert sms_carrier.campaigns_skipped == 1  # status() ran with the flag off
    assert sms_carrier.campaigns_created == 0
    assert resp.json()["waiting_for_credit"] is False  # funded, just busy
    assert await _reserved(async_session, vid) is None
    # Lock free again: the same funded row files.
    monkeypatch.undo()
    resp = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert sms_carrier.campaigns_created == 1


async def test_overlapping_polls_email_the_owners_once(
    client, org_and_key, carrier, session_factory, monkeypatch
) -> None:
    decided: list[str] = []
    monkeypatch.setattr(
        verification_decisions, "notify_verification_decided", decided.append
    )
    _, _, key = org_and_key
    created = (await _create(client, key)).json()  # sent to the carrier at once
    vid = created["id"]
    assert created["state"] == "submitted"
    carrier.remote_status = ProviderStatus(state="approved")
    entered, release = asyncio.Event(), asyncio.Event()
    original = carrier.status

    async def held(refs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await original(refs)

    carrier.status = held
    restore = _per_request_sessions(session_factory)
    try:
        first = asyncio.create_task(
            client.get(f"/verifications/{vid}", headers=_auth(key))
        )
        await asyncio.wait_for(entered.wait(), 5)
        # The first poll holds the row: the second skips the carrier call.
        second = await asyncio.wait_for(
            client.get(f"/verifications/{vid}", headers=_auth(key)), 5
        )
        assert second.status_code == 200, second.text
        release.set()
        assert (await first).json()["state"] == "approved"
    finally:
        release.set()
        restore()
    assert decided == [vid]


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
    assert body["step"] == "confirm_identity"
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
    assert resp.json()["step"] == "carrier_review"
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


@pytest.mark.usefixtures("us_number")
async def test_uncharged_recent_filings_count_against_the_balance(
    client, org_and_key, sms_carrier, async_session
) -> None:
    # The website charges each filing a moment later. Until it does, a new
    # filing needs credit for the earlier ones too.
    org_id, _, key = org_and_key
    floor = sms_registration_fees.filing_fee_cents("fake", "business")
    await _set_balance(async_session, org_id, floor + 1)
    sms_carrier.problems = [Problem(field="rep_phone", message="Use another one.")]
    sms_carrier.billed_refs = {"brand_id": "B7", "subject_type": "business"}
    assert (await _post_sms(client, key)).status_code == 422
    assert (await _post_sms(client, key)).status_code == 402
    # Once the website charged the first one, the account may file again.
    row = (
        await async_session.execute(
            select(CarrierVerification).where(
                CarrierVerification.organization_id == org_id
            )
        )
    ).scalar_one()
    async_session.add(
        AccountCredit(
            organization_id=org_id,
            kind="debit",
            channel="sms",
            amount_cents=-1,
            qty=1,
            ref=f"sms_registration:{row.id}:submission",
            source="sms_registration_submission",
        )
    )
    await async_session.commit()
    assert (await _post_sms(client, key)).status_code == 422
    # A filing made before fees were charged per step is settled by its
    # one-time debit (no ':submission' suffix).
    second = (
        await async_session.execute(
            select(CarrierVerification)
            .where(
                CarrierVerification.organization_id == org_id,
                CarrierVerification.id != row.id,
            )
            .order_by(CarrierVerification.created_at.desc())
        )
    ).scalar_one()
    assert (await _post_sms(client, key)).status_code == 402
    async_session.add(
        AccountCredit(
            organization_id=org_id,
            kind="debit",
            channel="sms",
            amount_cents=-1,
            qty=1,
            ref=f"sms_registration:{second.id}",
            source="sms_registration",
        )
    )
    await async_session.commit()
    await _set_balance(async_session, org_id, floor)
    assert (await _post_sms(client, key)).status_code == 422


# -- fee rater pings ------------------------------------------------------


def _sms_registry(fake):
    return lambda name, purpose="number": (
        fake if name == "fake" and purpose == "sms" else None
    )


@pytest.fixture()
def pinged(monkeypatch) -> list[str]:
    got: list[str] = []
    monkeypatch.setattr(
        verifications_routes, "notify_sms_registration_changed", got.append
    )
    return got


async def _waiting_sms(client, key, async_session, org_id) -> str:
    """A US texting registration put back to 'awaiting_review', as when the
    carrier was down at creation."""
    await _set_balance(async_session, org_id, 10_000)
    vid = (await _post_sms(client, key)).json()["id"]
    await _set_state(async_session, vid, "awaiting_review", age_s=3 * 60)
    return vid


@pytest.mark.usefixtures("us_number")
async def test_the_sweeper_retry_pings_the_rater(
    client, org_and_key, sms_carrier, async_session, pinged
) -> None:
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    pinged.clear()
    counts = await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    assert counts["submitted"] == 1
    assert pinged == [vid]


@pytest.mark.usefixtures("us_number")
async def test_a_superadmin_approve_pings_the_rater(
    client, org_and_key, sms_carrier, async_session, pinged
) -> None:
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    pinged.clear()
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    try:
        resp = await client.post(
            f"/admin/verifications/{vid}/approve", headers=_auth(key)
        )
    finally:
        app.dependency_overrides.pop(require_superadmin, None)
    assert resp.status_code == 200, resp.text
    assert pinged == [vid]


@pytest.mark.usefixtures("us_number")
async def test_a_superadmin_reject_and_a_cancel_ping_the_rater(
    client, org_and_key, sms_carrier, async_session, pinged
) -> None:
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    pinged.clear()
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    try:
        resp = await client.post(
            f"/admin/verifications/{vid}/reject",
            json={"reason": "no"},
            headers=_auth(key),
        )
    finally:
        app.dependency_overrides.pop(require_superadmin, None)
    assert resp.status_code == 200, resp.text
    assert pinged == [vid]
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert pinged == [vid, vid]


@pytest.mark.usefixtures("us_number")
async def test_a_refused_retry_pings_the_rater_once(
    client, org_and_key, sms_carrier, async_session, pinged
) -> None:
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    pinged.clear()

    async def refuse(refs):
        raise SubmitRefused([Problem(field="", message=NOT_ACCEPTED)])

    sms_carrier.submit = refuse
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "rejected"
    assert pinged == [vid]


@pytest.mark.usefixtures("us_number")
async def test_a_retry_that_finds_the_carrier_down_does_not_ping(
    client, org_and_key, sms_carrier, async_session, pinged
) -> None:
    """Nothing changed: the row is 'awaiting_review' with the same refs."""
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    pinged.clear()

    async def down(refs):
        raise RuntimeError("carrier down")

    sms_carrier.submit = down
    counts = await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    assert counts["submitted"] == 0
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "awaiting_review"
    assert pinged == []


@pytest.mark.usefixtures("us_number")
async def test_a_new_filing_pings_even_when_the_carrier_is_down(
    client, org_and_key, sms_carrier, async_session, pinged
) -> None:
    """A new row is a billed submission, whatever its submit did."""
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)

    async def down(refs):
        raise RuntimeError("carrier down")

    sms_carrier.submit = down
    resp = await _post_sms(client, key)
    assert resp.status_code == 201, resp.text
    assert resp.json()["state"] == "awaiting_review"
    assert pinged == [resp.json()["id"]]


@pytest.fixture()
def rated(monkeypatch, session_factory) -> list[tuple[str, dict]]:
    """Each awaited rater call, with the refs the rater would read then
    (a separate session: only what is committed)."""
    got: list[tuple[str, dict]] = []

    async def rate(verification_id: str) -> None:
        async with session_factory() as other:
            row = await other.get(CarrierVerification, uuid.UUID(verification_id))
            got.append((verification_id, dict(row.provider_refs)))

    monkeypatch.setattr(verifications_routes, "rate_sms_registration", rate)
    return got


async def _with_campaign(async_session, vid: str, state: str) -> None:
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    row.state = state
    row.provider_refs = {**row.provider_refs, "campaign_id": "C1"}
    await async_session.commit()


@pytest.mark.usefixtures("us_number")
@pytest.mark.parametrize("state", ["rejected", "approved"])
async def test_a_cancel_rates_the_campaign_before_its_id_is_wiped(
    client, org_and_key, sms_carrier, async_session, pinged, rated, state
) -> None:
    """The rater charges the carrier review by the campaign id in the refs.
    A cancel empties them: the rater reads them first."""
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    sms_carrier.campaign_key = "campaign_id"
    vid = (await _post_sms(client, key)).json()["id"]
    await _with_campaign(async_session, vid, state)
    pinged.clear()
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert len(rated) == 1
    assert rated[0][0] == vid and rated[0][1]["campaign_id"] == "C1"
    # Then the wipe, and the usual ping for the new state.
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "cancelled" and not any(
        key != "application" for key in row.provider_refs
    )
    assert pinged == [vid]


@pytest.mark.usefixtures("us_number")
async def test_a_superadmin_reject_rates_the_campaign_before_its_id_is_wiped(
    client, org_and_key, sms_carrier, async_session, rated
) -> None:
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    sms_carrier.campaign_key = "campaign_id"
    await _with_campaign(async_session, vid, "awaiting_review")
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    try:
        resp = await client.post(
            f"/admin/verifications/{vid}/reject",
            json={"reason": "no"},
            headers=_auth(key),
        )
    finally:
        app.dependency_overrides.pop(require_superadmin, None)
    assert resp.status_code == 200, resp.text
    assert rated == [(vid, rated[0][1])] and rated[0][1]["campaign_id"] == "C1"


@pytest.mark.usefixtures("us_number")
async def test_a_refused_retry_rates_the_campaign_before_its_id_is_wiped(
    client, org_and_key, sms_carrier, async_session, rated
) -> None:
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    sms_carrier.campaign_key = "campaign_id"
    await _with_campaign(async_session, vid, "awaiting_review")

    async def refuse(refs):
        return [Problem(field="", message=NOT_ACCEPTED)]

    sms_carrier.check = refuse
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "rejected"
    assert [(v, r["campaign_id"]) for v, r in rated] == [(vid, "C1")]


@pytest.mark.usefixtures("us_number")
async def test_a_refusal_leaves_a_row_cancelled_while_it_was_rated(
    client, org_and_key, sms_carrier, async_session, monkeypatch, session_factory
) -> None:
    """Rating commits, which ends the row lock. A cancel in that gap wins:
    the refusal neither discards nor rejects the row."""
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    sms_carrier.campaign_key = "campaign_id"
    await _with_campaign(async_session, vid, "awaiting_review")

    async def cancel_meanwhile(verification_id: str) -> None:
        async with session_factory() as other:
            row = await other.get(CarrierVerification, uuid.UUID(verification_id))
            row.state, row.provider_refs = "cancelled", {}
            await other.commit()

    async def refuse(refs):
        return [Problem(field="", message=NOT_ACCEPTED)]

    discarded: list[dict] = []

    async def discard(refs):
        discarded.append(refs)

    monkeypatch.setattr(verifications_routes, "rate_sms_registration", cancel_meanwhile)
    sms_carrier.check, sms_carrier.discard = refuse, discard
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "cancelled" and not any(
        key != "application" for key in row.provider_refs
    )
    assert discarded == []


@pytest.mark.usefixtures("us_number")
async def test_a_refusal_leaves_a_row_that_came_back_while_it_was_rated(
    client, org_and_key, sms_carrier, async_session, monkeypatch, session_factory
) -> None:
    """A row that left and came back to the same state in that gap (an
    approve that found the carrier down, say) is not rejected for the old
    check."""
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    sms_carrier.campaign_key = "campaign_id"
    await _with_campaign(async_session, vid, "awaiting_review")

    from datetime import datetime, timezone

    async def round_trip(verification_id: str) -> None:
        async with session_factory() as other:
            row = await other.get(CarrierVerification, uuid.UUID(verification_id))
            row.provider_refs = {**row.provider_refs, "campaign_id": "C2"}
            row.updated_at = datetime.now(timezone.utc)
            await other.commit()

    async def refuse(refs):
        return [Problem(field="", message=NOT_ACCEPTED)]

    discarded: list[dict] = []

    async def discard(refs):
        discarded.append(refs)

    monkeypatch.setattr(verifications_routes, "rate_sms_registration", round_trip)
    sms_carrier.check, sms_carrier.discard = refuse, discard
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "awaiting_review"
    assert row.provider_refs["campaign_id"] == "C2"
    assert discarded == []


@pytest.mark.usefixtures("us_number")
async def test_a_refused_submit_keeps_a_filed_campaign_when_the_discard_fails(
    client, org_and_key, sms_carrier, async_session, rated
) -> None:
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    sms_carrier.campaign_key = "campaign_id"
    await _with_campaign(async_session, vid, "awaiting_review")

    async def refuse(refs):
        raise SubmitRefused([Problem(field="", message=NOT_ACCEPTED)])

    async def boom(refs):
        raise RuntimeError("carrier down")

    sms_carrier.submit, sms_carrier.discard = refuse, boom
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "awaiting_review"
    assert row.provider_refs["campaign_id"] == "C1"
    assert [(v, r["campaign_id"]) for v, r in rated] == [(vid, "C1")]


@pytest.mark.usefixtures("us_number")
async def test_admin_approve_shows_a_row_cancelled_while_it_was_refused(
    client,
    org_and_key,
    sms_carrier,
    async_session,
    monkeypatch,
    session_factory,
    pinged,
) -> None:
    """Not a 409 'refused': the row was cancelled, not rejected."""
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    sms_carrier.campaign_key = "campaign_id"
    await _with_campaign(async_session, vid, "awaiting_review")

    async def cancel_meanwhile(verification_id: str) -> None:
        async with session_factory() as other:
            row = await other.get(CarrierVerification, uuid.UUID(verification_id))
            row.state, row.provider_refs = "cancelled", {}
            await other.commit()

    async def refuse(refs):
        raise SubmitRefused([Problem(field="", message=NOT_ACCEPTED)])

    monkeypatch.setattr(verifications_routes, "rate_sms_registration", cancel_meanwhile)
    sms_carrier.submit = refuse
    pinged.clear()
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    try:
        resp = await client.post(
            f"/admin/verifications/{vid}/approve", headers=_auth(key)
        )
    finally:
        app.dependency_overrides.pop(require_superadmin, None)
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "cancelled"
    assert pinged == [vid]  # submit() wrote new refs


@pytest.mark.usefixtures("us_number")
async def test_admin_approve_shows_the_refusal_when_the_discard_fails(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)

    async def refuse(refs):
        raise SubmitRefused([Problem(field="", message=NOT_ACCEPTED, raw="nope")])

    async def boom(refs):
        raise RuntimeError("carrier down")

    sms_carrier.submit, sms_carrier.discard = refuse, boom
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    try:
        resp = await client.post(
            f"/admin/verifications/{vid}/approve", headers=_auth(key)
        )
    finally:
        app.dependency_overrides.pop(require_superadmin, None)
    assert resp.status_code == 409, resp.text
    assert "nope" in resp.json()["detail"]
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "awaiting_review"


@pytest.mark.usefixtures("us_number")
@pytest.mark.parametrize("refused", [True, False])
async def test_admin_approve_keeps_the_ids_a_failed_check_wrote(
    client, org_and_key, sms_carrier, async_session, refused
) -> None:
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)

    async def check(refs):
        refs["filed_id"] = "F1"
        if not refused:
            raise RuntimeError("carrier down")
        return [Problem(field="", message=NOT_ACCEPTED)]

    sms_carrier.check = check
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    try:
        resp = await client.post(
            f"/admin/verifications/{vid}/approve", headers=_auth(key)
        )
    finally:
        app.dependency_overrides.pop(require_superadmin, None)
    assert resp.status_code == (409 if refused else 502), resp.text
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "awaiting_review" and row.provider_refs["filed_id"] == "F1"


async def test_a_sweeper_check_that_fails_keeps_the_ids_it_wrote(
    client, org_and_key, carrier, async_session
) -> None:
    from hailhq.api.routes.verifications import sweep_verifications

    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)
    await _set_state(async_session, vid, "awaiting_review", age_s=3 * 60)

    async def check(refs):
        refs["filed_id"] = "F1"
        raise RuntimeError("carrier down")

    carrier.check = check
    await sweep_verifications(
        async_session,
        lambda name, purpose="number": carrier if name == "fake" else None,
    )
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "awaiting_review"
    assert row.provider_refs == {"bundle_sid": "B1", "filed_id": "F1"}


@pytest.mark.usefixtures("us_number")
async def test_a_refused_retry_pings_the_rater_when_its_discard_fails(
    client, org_and_key, sms_carrier, async_session, pinged
) -> None:
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)

    async def check(refs):
        refs["filed_id"] = "F1"
        return [Problem(field="", message=NOT_ACCEPTED)]

    async def boom(refs):
        raise RuntimeError("carrier down")

    sms_carrier.check, sms_carrier.discard = check, boom
    pinged.clear()
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    assert pinged == [vid]


@pytest.mark.usefixtures("us_number")
async def test_admin_approve_shows_the_refusal_of_a_row_waiting_again(
    client, org_and_key, sms_carrier, async_session, monkeypatch, session_factory
) -> None:
    """The row went back to waiting while the refusal was rated: the same
    draft would be refused again, so the admin sees why (not 502)."""
    from datetime import datetime, timezone

    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    sms_carrier.campaign_key = "campaign_id"
    await _with_campaign(async_session, vid, "awaiting_review")

    async def back_to_waiting(verification_id: str) -> None:
        async with session_factory() as other:
            row = await other.get(CarrierVerification, uuid.UUID(verification_id))
            row.state = "awaiting_review"
            row.updated_at = datetime.now(timezone.utc)
            await other.commit()

    async def refuse(refs):
        raise SubmitRefused([Problem(field="", message=NOT_ACCEPTED, raw="nope")])

    monkeypatch.setattr(verifications_routes, "rate_sms_registration", back_to_waiting)
    sms_carrier.submit = refuse
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    try:
        resp = await client.post(
            f"/admin/verifications/{vid}/approve", headers=_auth(key)
        )
    finally:
        app.dependency_overrides.pop(require_superadmin, None)
    assert resp.status_code == 409, resp.text
    assert "nope" in resp.json()["detail"]


@pytest.mark.usefixtures("us_number")
async def test_a_superadmin_reject_leaves_a_row_that_changed_while_it_was_rated(
    client, org_and_key, sms_carrier, async_session, monkeypatch, session_factory
) -> None:
    """Rating commits, which ends the row lock. A row the background pass
    moved on in that gap is not discarded or rejected: the admin gets 409."""
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    sms_carrier.campaign_key = "campaign_id"
    await _with_campaign(async_session, vid, "awaiting_review")

    async def submit_meanwhile(verification_id: str) -> None:
        async with session_factory() as other:
            row = await other.get(CarrierVerification, uuid.UUID(verification_id))
            row.state = "submitting"
            await other.commit()

    discarded: list[dict] = []

    async def discard(refs):
        discarded.append(refs)

    monkeypatch.setattr(verifications_routes, "rate_sms_registration", submit_meanwhile)
    sms_carrier.discard = discard
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    try:
        resp = await client.post(
            f"/admin/verifications/{vid}/reject",
            json={"reason": "no"},
            headers=_auth(key),
        )
    finally:
        app.dependency_overrides.pop(require_superadmin, None)
    assert resp.status_code == 409, resp.text
    assert discarded == []
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "submitting" and row.provider_refs["campaign_id"] == "C1"


@pytest.mark.usefixtures("us_number")
async def test_a_stamp_error_after_a_submit_does_not_undo_it(
    client, org_and_key, sms_carrier, async_session, monkeypatch
) -> None:
    """The submit went through: a plug-in error while stamping the refs
    after it must not put the row back to be submitted again."""
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)

    armed: list[bool] = []
    submit, campaign_filed = sms_carrier.submit, sms_carrier.campaign_filed

    async def submit_then_arm(refs):
        await submit(refs)
        armed.append(True)

    def broken_once(refs):
        if armed:
            armed.clear()
            raise KeyError("odd refs")
        return campaign_filed(refs)

    monkeypatch.setattr(sms_carrier, "submit", submit_then_arm)
    monkeypatch.setattr(sms_carrier, "campaign_filed", broken_once)
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "submitted"


@pytest.mark.usefixtures("us_number")
async def test_a_cancel_without_a_campaign_does_not_wait_for_the_rater(
    client, org_and_key, sms_carrier, async_session, pinged, rated
) -> None:
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    sms_carrier.campaign_key = "campaign_id"
    pinged.clear()
    resp = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert rated == []
    assert pinged == [vid]


# -- Telnyx outage after the brand ----------------------------------------

_TELNYX_BUSINESS = {
    "sender_country": "US",
    "has_tax_id": "YES",
    "program_name": "Acme",
    "opt_in_message": "Acme: Enrolled for appointment updates, once per appointment. Msg and data rates may apply. Reply HELP for help, STOP to stop.",
    "message_frequency": "once per appointment",
    "disclosure_purposes_confirmed": "YES",
    "text_types": "CUSTOMER_CARE,ACCOUNT_NOTIFICATION",
    "sample_1_type": "CUSTOMER_CARE",
    "sample_2_type": "ACCOUNT_NOTIFICATION",
    "consent_methods": "WEBSITE",
    "consent_disclosure": "Get Acme appointment reminders, once per appointment. Message and data rates may apply. Reply STOP to opt out or HELP for help. https://acme.org/privacy https://acme.org/terms",
    "consent_records": "We privately retain the checkbox selection, timestamp and disclosure version.",
    "consent_optional": "YES",
    "marketing_consent": "NO",
    "evidence_redacted": "YES",
    "website_url": "https://acme.org/book",
    "website_evidence_url": "https://acme.org/evidence/booking.png",
    "website_action": "Select the initially unchecked SMS reminder checkbox, then submit.",
    "policies_confirmed": "YES",
    "has_phone": "NO",
    "age_gated": "NO",
    "direct_lending": "NO",
    "restricted_content": "NONE",
    "registration_country": "US",
    "legal_name": "Acme Inc",
    "business_type": "PRIVATE_PROFIT",
    "ein": "12-3456789",
    "industry": "RETAIL",
    "website": "https://acme.org",
    "rep_name": "Ada Lovelace",
    "rep_email": "ada@acme.example",
    "rep_phone": "+15125550100",
    "rep_title": "CEO",
    "campaign_description": "Order updates and delivery notices for customers who ordered on our site.",
    "sample_1": "Acme: your order 123 shipped. Reply STOP to opt out.",
    "sample_2": "Acme: your order 123 was delivered. Reply STOP to opt out.",
    "opt_in_description": "Customers tick a box at checkout on acme.example.",
    "has_links": "NO",
    "privacy_policy_url": "https://acme.org/privacy",
    "terms_url": "https://acme.org/terms",
}
_TELNYX_PERSON = {
    "sender_country": "US",
    "has_tax_id": "NO",
    "program_name": "Ada",
    "opt_in_message": "Ada: Enrolled for appointment updates, once per appointment. Msg and data rates may apply. Reply HELP for help, STOP to stop.",
    "message_frequency": "once per appointment",
    "disclosure_purposes_confirmed": "YES",
    "text_types": "CUSTOMER_CARE,ACCOUNT_NOTIFICATION",
    "sample_1_type": "CUSTOMER_CARE",
    "sample_2_type": "ACCOUNT_NOTIFICATION",
    "consent_methods": "WEBSITE",
    "consent_disclosure": "Get Ada appointment reminders, once per appointment. Message and data rates may apply. Reply STOP to opt out or HELP for help. https://ada.org/privacy https://ada.org/terms",
    "consent_records": "We privately retain the checkbox selection, timestamp and disclosure version.",
    "consent_optional": "YES",
    "marketing_consent": "NO",
    "evidence_redacted": "YES",
    "website_url": "https://acme.org/book",
    "website_evidence_url": "https://acme.org/evidence/booking.png",
    "website_action": "Select the initially unchecked SMS reminder checkbox, then submit.",
    "policies_confirmed": "YES",
    "has_phone": "NO",
    "age_gated": "NO",
    "direct_lending": "NO",
    "restricted_content": "NONE",
    "registration_country": "US",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "mobile_phone_number": "+15125550100",
    "campaign_description": "Appointment reminders for my own clients who asked for them.",
    "sample_1": "Ada: your session is tomorrow at 10. Reply STOP to opt out.",
    "sample_2": "Ada: thanks for booking. Reply STOP to opt out.",
    "opt_in_description": "Clients give their number on my booking form.",
    "has_links": "NO",
    "privacy_policy_url": "https://ada.org/privacy",
    "terms_url": "https://ada.org/terms",
}


@pytest.fixture()
def telnyx_outage(monkeypatch):
    """The real Telnyx plug-in behind the org's 'fake' US numbers. The
    brand registers; the PIN and vetting calls answer 503 until the test
    sets ``state['down']`` to False."""
    import httpx
    from hailhq.core.providers.verification import telnyx_sms

    monkeypatch.setattr(telnyx_sms, "_BRAND_READ_GAP", 0)
    state = {"down": True, "calls": []}

    def handler(req: httpx.Request):
        state["calls"].append((req.method, req.url.path))
        if req.method == "POST" and req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(
                200, json={"status": "OK", "identityStatus": "SELF_DECLARED"}
            )
        if req.method == "GET" and req.url.path.endswith("/externalVetting"):
            return httpx.Response(200, json=[])
        if req.url.path.endswith(("/smsOtp", "/externalVetting")):
            if state["down"]:
                return httpx.Response(503)
            return httpx.Response(200, json={"referenceId": "R1"})
        return httpx.Response(404)

    provider = telnyx_sms.TelnyxSmsRegistrationProvider(
        api_key="k",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.telnyx.com/v2",
        ),
    )
    app.dependency_overrides[get_verification_registry] = lambda: _sms_registry(
        provider
    )
    verifications_routes._last_polled.clear()
    yield state
    app.dependency_overrides.pop(get_verification_registry, None)


@pytest.mark.usefixtures("us_number")
@pytest.mark.parametrize(
    ("subject_type", "fields", "step"),
    [
        ("business", _TELNYX_BUSINESS, "/v2/10dlc/brand/B1/externalVetting"),
        ("person", _TELNYX_PERSON, "/v2/10dlc/brand/B1/smsOtp"),
    ],
)
async def test_a_telnyx_outage_after_the_brand_keeps_the_filing(
    client,
    org_and_key,
    async_session,
    telnyx_outage,
    pinged,
    subject_type,
    fields,
    step,
) -> None:
    """A retry would file and bill a new brand: the brand is kept as a
    normal filing, and the next read sends the PIN or asks for vetting."""
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    resp = await _post_sms(
        client, key, subject_type=subject_type, fields=json.dumps(fields)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "submitted" and body["confirmation"] is None
    assert pinged == [body["id"]]
    rows = (
        (
            await async_session.execute(
                select(CarrierVerification).where(
                    CarrierVerification.organization_id == org_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [r.state for r in rows] == ["submitted"]
    assert rows[0].provider_refs["brand_id"] == "B1"
    assert not any(m == "DELETE" for m, _ in telnyx_outage["calls"])
    # Telnyx is back: the next read finishes the step.
    telnyx_outage["down"] = False
    got = await client.get(f"/verifications/{body['id']}", headers=_auth(key))
    assert got.status_code == 200, got.text
    assert ("POST", step) in telnyx_outage["calls"]
    assert pinged == [body["id"], body["id"]]
    if subject_type == "person":
        assert got.json()["confirmation"] == "code"


def test_campaign_filed_at_is_stamped_once_from_the_carrier_key() -> None:
    carrier = FakeSmsCarrier()
    carrier.campaign_key = "campaign_xyz"
    refs: dict = {"brand_id": "B1"}
    verifications_routes._stamp_campaign_filed(carrier, refs)
    assert "campaign_filed_at" not in refs
    # Only a non-empty string is a campaign, as the billing service reads it.
    for value in (7, True, ""):
        odd = {"campaign_xyz": value}
        verifications_routes._stamp_campaign_filed(carrier, odd)
        assert "campaign_filed_at" not in odd
        assert not verifications_routes._campaign_filed(carrier, odd)
    refs["campaign_xyz"] = "C1"
    verifications_routes._stamp_campaign_filed(carrier, refs)
    first = refs["campaign_filed_at"]
    verifications_routes._stamp_campaign_filed(carrier, refs)
    assert first and refs["campaign_filed_at"] == first
    # A number plug-in or no plug-in never stamps.
    bare: dict = {"campaign_xyz": "C1"}
    verifications_routes._stamp_campaign_filed(None, bare)
    verifications_routes._stamp_campaign_filed(FakeCarrier(), bare)
    assert "campaign_filed_at" not in bare


@pytest.mark.usefixtures("us_number")
async def test_a_campaign_filed_by_a_refused_submit_is_rated_before_the_wipe(
    client, org_and_key, sms_carrier, async_session, rated
) -> None:
    """A carrier that files its campaign inside submit() and then refuses:
    the campaign and its marker reach the rater before the refs are wiped."""
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    sms_carrier.campaign_key = "campaign_xyz"

    async def file_then_refuse(refs):
        refs["campaign_xyz"] = "C9"
        raise SubmitRefused([Problem(field="", message=NOT_ACCEPTED)])

    sms_carrier.submit = file_then_refuse
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    assert len(rated) == 1 and rated[0][0] == vid
    assert rated[0][1]["campaign_xyz"] == "C9"
    assert rated[0][1]["campaign_filed_at"]


@pytest.mark.usefixtures("us_number")
async def test_a_campaign_filed_by_a_failed_submit_is_kept(
    client, org_and_key, sms_carrier, async_session, session_factory
) -> None:
    org_id, _, key = org_and_key
    vid = await _waiting_sms(client, key, async_session, org_id)
    sms_carrier.campaign_key = "campaign_xyz"

    async def file_then_fail(refs):
        refs["campaign_xyz"] = "C9"
        raise RuntimeError("carrier down after filing")

    sms_carrier.submit = file_then_fail
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    async with session_factory() as other:
        row = await other.get(CarrierVerification, uuid.UUID(vid))
    assert row.state == "awaiting_review"
    assert row.provider_refs["campaign_xyz"] == "C9"
    assert row.provider_refs["campaign_filed_at"]


def test_a_failed_call_keeps_ids_it_appended_or_nested() -> None:
    before = {
        "bundle_sid": "B1",
        "docs": ["D1"],
        "brand": {"id": "R1"},
        "campaign_id": None,
        "vetting_requested": False,
    }
    after = {
        "docs": ["D1", "D2"],
        "brand": {"id": "R9", "vetting_id": "V1"},
        "filed_id": "F1",
        "campaign_id": "C1",
        "vetting_requested": True,
    }
    assert verifications_routes._added_only(before, after) == {
        "bundle_sid": "B1",
        "docs": ["D1", "D2"],
        "brand": {"id": "R1", "vetting_id": "V1"},
        "filed_id": "F1",
        "campaign_id": "C1",
        "vetting_requested": True,
    }
    # A zero is a value, not an empty one: a change to it goes back.
    assert verifications_routes._added_only({"step": 0}, {"step": 3}) == {"step": 0}
    # A list that lost or reordered items goes back.
    assert verifications_routes._added_only(
        {"docs": ["D1", "D2"]}, {"docs": ["D2"]}
    ) == {"docs": ["D1", "D2"]}


@pytest.mark.parametrize(
    "country,e164",
    [
        ("VI", "+13405550100"),
        ("PR", "+17875550100"),
        ("GU", "+16715550100"),
        ("MP", "+16705550100"),
        ("AS", "+16845550100"),
    ],
)
async def test_territory_only_org_can_register_and_attach_numbers(
    client, org_and_key, sms_carrier, async_session, country, e164
):
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    number = await _us_number(async_session, org_id, e164, resource="PN-territory")
    number.country_code = country
    await async_session.commit()
    requirements = await client.get(
        "/verifications/requirements",
        params={
            "subject_type": "business",
            "country_code": "US",
            "number_type": "local",
            "purpose": "sms",
            "number_id": str(number.id),
        },
        headers=_auth(key),
    )
    assert requirements.status_code == 200, requirements.text
    filed = await _post_sms(client, key, provider="", number_id=str(number.id))
    assert filed.status_code == 201, filed.text
    vid = filed.json()["id"]
    assert filed.json()["country_code"] == "US"
    sms_carrier.remote_status = ProviderStatus(state="approved")
    verifications_routes._last_polled.clear()
    approved = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert approved.json()["state"] == "approved", approved.text
    await async_session.refresh(number)
    assert str(number.sms_registration_id) == vid
    assert [resource for _, resource, _ in sms_carrier.attached] == ["PN-territory"]


@pytest.mark.parametrize(
    "subject_type,expected_attached", [("business", True), ("person", False)]
)
async def test_late_territory_number_respects_campaign_number_limit(
    client,
    org_and_key,
    sms_carrier,
    async_session,
    us_number,
    subject_type,
    expected_attached,
):
    from hailhq.api import sms_registration

    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    vid = await _approved_sms_row(
        client, key, async_session, sms_carrier, subject_type=subject_type
    )
    later = await _us_number(
        async_session, org_id, "+13405550101", resource="PN-later-territory"
    )
    later.country_code = "VI"
    await async_session.commit()
    await sms_registration.attach_if_registered(
        async_session,
        later,
        lambda name, purpose="number": sms_carrier if purpose == "sms" else None,
    )
    assert (later.sms_registration_id is not None) is expected_attached
    if expected_attached:
        assert str(later.sms_registration_id) == vid


@pytest.mark.parametrize(
    "subject_type,keep_primary,expected",
    [("business", True, True), ("person", True, False), ("person", False, True)],
)
async def test_background_repairs_existing_territory_campaign_links(
    client,
    org_and_key,
    sms_carrier,
    async_session,
    us_number,
    subject_type,
    keep_primary,
    expected,
):
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    vid = await _approved_sms_row(
        client, key, async_session, sms_carrier, subject_type=subject_type
    )
    if not keep_primary:
        # As release_org_number does: off the registration, then released.
        # A released number still on it would keep the person's registration.
        us_number.sms_registration_id = None
        us_number.provisioning_state = "released"
    territory = await _us_number(
        async_session, org_id, "+13405550101", resource="PN-existing-territory"
    )
    territory.country_code = "VI"
    await async_session.commit()
    sms_carrier.attached.clear()

    registry = _sms_registry(sms_carrier)

    await verifications_routes.sweep_verifications(async_session, registry)
    await async_session.refresh(territory)
    assert (territory.sms_registration_id is not None) is expected
    if expected:
        assert str(territory.sms_registration_id) == vid
        assert [r for _, r, _ in sms_carrier.attached] == ["PN-existing-territory"]
    calls = len(sms_carrier.attached)
    await verifications_routes.sweep_verifications(async_session, registry)
    assert len(sms_carrier.attached) == calls


async def _end_the_wait(async_session, number: PhoneNumber) -> None:
    """Move a refused number's retry time into the past."""
    refusal = number.provisioning_metadata["sms_attach_refused"]
    number.provisioning_metadata = {
        **number.provisioning_metadata,
        "sms_attach_refused": {
            **refusal,
            "retry_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        },
    }
    await async_session.commit()


def _reason_hash(reason: str) -> str:
    key = sms_registration._reason_key(reason)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


async def _audits(async_session, action: str) -> list[AuditLog]:
    await sms_registration.drain_audits()
    result = await async_session.execute(
        select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.occurred_at)
    )
    return list(result.scalars().all())


async def _refused_territory_number(
    client, key, org_id, sms_carrier, async_session, monkeypatch
):
    """A territory number on an approved registration, with every attach
    refused. Returns the number, the registration id, and the attempts."""
    await _set_balance(async_session, org_id, 10_000)
    vid = await _approved_sms_row(client, key, async_session, sms_carrier)
    number = await _us_number(
        async_session, org_id, "+13405550101", resource="PN-refused-territory"
    )
    number.country_code = "VI"
    await async_session.commit()
    attempts: list[str] = []

    async def refused(refs, *, provider_resource_id, e164):
        attempts.append(provider_resource_id)
        raise RuntimeError("carrier refused")

    monkeypatch.setattr(sms_carrier, "attach_number", refused)
    return number, vid, attempts


async def test_background_retries_a_refused_territory_campaign_link(
    client, org_and_key, sms_carrier, async_session, us_number, monkeypatch
):
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    vid = await _approved_sms_row(client, key, async_session, sms_carrier)
    territory = await _us_number(
        async_session, org_id, "+13405550101", resource="PN-retry-territory"
    )
    territory.country_code = "VI"
    await async_session.commit()
    attach = sms_carrier.attach_number
    attempts = 0

    async def flaky(refs, *, provider_resource_id, e164):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("carrier unavailable")
        await attach(refs, provider_resource_id=provider_resource_id, e164=e164)

    registry = _sms_registry(sms_carrier)

    monkeypatch.setattr(sms_carrier, "attach_number", flaky)
    await verifications_routes.sweep_verifications(async_session, registry)
    await async_session.refresh(territory)
    assert territory.sms_registration_id is None
    await _end_the_wait(async_session, territory)
    await verifications_routes.sweep_verifications(async_session, registry)
    await async_session.refresh(territory)
    assert str(territory.sms_registration_id) == vid
    await verifications_routes.sweep_verifications(async_session, registry)
    assert attempts == 2


@pytest.mark.usefixtures("us_number")
async def test_background_audits_a_refused_number_once(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    org_id, _, key = org_and_key
    attach = sms_carrier.attach_number
    number, vid, attempts = await _refused_territory_number(
        client, key, org_id, sms_carrier, async_session, monkeypatch
    )
    registry = _sms_registry(sms_carrier)
    attached_before = len(await _audits(async_session, "verification.sms_attached"))

    for _ in range(3):
        await verifications_routes.sweep_verifications(async_session, registry)
        await _end_the_wait(async_session, number)
    assert len(attempts) == 3

    refused = await _audits(async_session, "verification.sms_attach_refused")
    assert len(refused) == 1
    assert refused[0].organization_id == org_id
    assert refused[0].resource_type == "phone_number"
    assert refused[0].resource_id == number.id
    assert refused[0].actor_kind == "system"
    assert refused[0].payload == {
        "e164": "+13405550101",
        "verification_id": vid,
        "carrier_reason": "carrier refused",
    }
    attached = await _audits(async_session, "verification.sms_attached")
    assert len(attached) == attached_before

    monkeypatch.setattr(sms_carrier, "attach_number", attach)
    await verifications_routes.sweep_verifications(async_session, registry)
    await async_session.refresh(number)
    assert str(number.sms_registration_id) == vid
    assert "sms_attach_refused" not in number.provisioning_metadata
    attached = await _audits(async_session, "verification.sms_attached")
    assert len(attached) == attached_before + 1
    assert attached[-1].payload == {"numbers": 1, "failed": 0}

    await verifications_routes.sweep_verifications(async_session, registry)
    assert len(await _audits(async_session, "verification.sms_attached")) == (
        attached_before + 1
    )
    assert len(await _audits(async_session, "verification.sms_attach_refused")) == 1


@pytest.mark.usefixtures("us_number")
async def test_background_leaves_a_refused_number_alone_until_its_wait_ends(
    client, org_and_key, sms_carrier, async_session, monkeypatch, caplog
):
    org_id, _, key = org_and_key
    number, _, attempts = await _refused_territory_number(
        client, key, org_id, sms_carrier, async_session, monkeypatch
    )
    registry = _sms_registry(sms_carrier)

    await verifications_routes.sweep_verifications(async_session, registry)
    assert len(attempts) == 1

    caplog.clear()
    with caplog.at_level("WARNING", logger="hailhq.api.sms_registration"):
        await verifications_routes.sweep_verifications(async_session, registry)
    assert len(attempts) == 1
    assert not caplog.records

    await _end_the_wait(async_session, number)
    await verifications_routes.sweep_verifications(async_session, registry)
    assert len(attempts) == 2
    assert number.provisioning_metadata["sms_attach_refused"]["wait_hours"] == 2


@pytest.mark.usefixtures("us_number")
async def test_a_refused_number_waits_longer_each_time_up_to_a_day(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    org_id, _, key = org_and_key
    number, _, _ = await _refused_territory_number(
        client, key, org_id, sms_carrier, async_session, monkeypatch
    )
    registry = _sms_registry(sms_carrier)

    waits = []
    for _ in range(7):
        await verifications_routes.sweep_verifications(async_session, registry)
        refusal = number.provisioning_metadata["sms_attach_refused"]
        waits.append(refusal["wait_hours"])
        retry_at = datetime.fromisoformat(refusal["retry_at"])
        assert retry_at > datetime.now(timezone.utc) + timedelta(hours=waits[-1] - 1)
        await _end_the_wait(async_session, number)
    assert waits == [1, 2, 4, 8, 16, 24, 24]


@pytest.mark.usefixtures("us_number")
async def test_a_wait_from_another_registration_does_not_count(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    # A registration was cancelled and a new one approved: the refused number
    # was never on the old one, so nothing cleared its state.
    org_id, _, key = org_and_key
    number, vid, attempts = await _refused_territory_number(
        client, key, org_id, sms_carrier, async_session, monkeypatch
    )
    number.provisioning_metadata = {
        "sms_attach_refused": {
            "registration_id": str(uuid.uuid4()),
            "wait_hours": 24,
            "retry_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        }
    }
    await async_session.commit()

    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    assert len(attempts) == 1
    refusal = number.provisioning_metadata["sms_attach_refused"]
    assert refusal["registration_id"] == vid
    assert refusal["wait_hours"] == 1
    assert len(await _audits(async_session, "verification.sms_attach_refused")) == 1


async def test_a_refused_attach_at_approval_is_audited_once(
    client, org_and_key, sms_carrier, async_session
):
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    number = await _us_number(async_session, org_id)
    vid = (await _post_sms(client, key)).json()["id"]
    sms_carrier.remote_status = ProviderStatus(state="approved")
    sms_carrier.attach_error = RuntimeError("carrier refused")
    verifications_routes._last_polled.clear()
    await client.get(f"/verifications/{vid}", headers=_auth(key))

    await async_session.refresh(number)
    assert number.provisioning_metadata["sms_attach_refused"]["registration_id"] == vid
    await _end_the_wait(async_session, number)
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    assert number.provisioning_metadata["sms_attach_refused"]["wait_hours"] == 2
    assert len(await _audits(async_session, "verification.sms_attach_refused")) == 1


async def _person_twilio_row(async_session, org_id, monkeypatch, refused: set[str]):
    """An approved person registration on Twilio, with enable-sms wired to
    the fake carrier. The carrier refuses the resource ids in ``refused``;
    returns the resource ids it accepted."""
    from hailhq.api.routes import numbers as numbers_routes

    row = CarrierVerification(
        organization_id=org_id,
        provider="twilio",
        purpose="sms",
        country_code="US",
        number_type="local",
        subject_type="person",
        state="approved",
        provider_refs={"campaign_sid": "QE1", "messaging_service_sid": "MG1"},
        requirements_version="1",
    )
    async_session.add(row)
    await async_session.commit()
    carrier = FakeSmsCarrier()
    monkeypatch.setattr(
        numbers_routes,
        "get_verification_provider",
        lambda name, purpose="number": carrier if purpose == "sms" else None,
    )
    tried: list[str] = []

    async def attach(refs, *, provider_resource_id, e164):
        tried.append(provider_resource_id)
        if provider_resource_id in refused:
            raise RuntimeError("carrier refused")

    monkeypatch.setattr(carrier, "attach_number", attach)
    return row, tried


async def test_enable_sms_tries_a_persons_next_number_while_the_oldest_is_refused(
    client, org_and_key, async_session, sms_mock, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    oldest = await _us_number(
        async_session, org_id, "+15125550100", provider="twilio", resource="PNA"
    )
    second = await _us_number(
        async_session, org_id, "+15125550101", provider="twilio", resource="PNB"
    )
    third = await _us_number(
        async_session, org_id, "+15125550102", provider="twilio", resource="PNC"
    )
    row, tried = await _person_twilio_row(
        async_session, org_id, monkeypatch, refused={"PNA"}
    )

    resp = await client.post(f"/numbers/{oldest.id}/enable-sms", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"/numbers/{second.id}/enable-sms", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert tried == ["PNA", "PNB"]
    await async_session.refresh(second)
    assert second.sms_registration_id == row.id
    assert "sms_attach_refused" not in second.provisioning_metadata

    # The registration now holds its one number: no other number joins it.
    for number in (oldest, third):
        resp = await client.post(f"/numbers/{number.id}/enable-sms", headers=_auth(key))
        assert resp.status_code == 200, resp.text
    assert tried == ["PNA", "PNB"]
    on = (
        await async_session.execute(
            select(PhoneNumber.provider_resource_id).where(
                PhoneNumber.sms_registration_id == row.id
            )
        )
    ).scalars()
    assert list(on) == ["PNB"]


async def test_enable_sms_does_not_skip_a_persons_untried_oldest_number(
    client, org_and_key, async_session, sms_mock, monkeypatch
) -> None:
    org_id, _, key = org_and_key
    await _us_number(
        async_session, org_id, "+15125550100", provider="twilio", resource="PNA"
    )
    second = await _us_number(
        async_session, org_id, "+15125550101", provider="twilio", resource="PNB"
    )
    _, tried = await _person_twilio_row(
        async_session, org_id, monkeypatch, refused=set()
    )
    resp = await client.post(f"/numbers/{second.id}/enable-sms", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert tried == []
    await async_session.refresh(second)
    assert second.sms_registration_id is None


async def test_a_persons_next_number_reads_the_others_under_the_lock(
    org_and_key, async_session, monkeypatch
) -> None:
    # This session loaded the refused oldest number; then another request
    # attached it. The next number must not join as a second one.
    org_id, _, _ = org_and_key
    oldest = await _us_number(
        async_session, org_id, "+15125550100", provider="twilio", resource="PNA"
    )
    second = await _us_number(
        async_session, org_id, "+15125550101", provider="twilio", resource="PNB"
    )
    row, _ = await _person_twilio_row(async_session, org_id, monkeypatch, refused=set())
    oldest.provisioning_metadata = {
        "sms_attach_refused": {
            "registration_id": str(row.id),
            "wait_hours": 1,
            "retry_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "reason_hash": _reason_hash("carrier refused"),
        }
    }
    await async_session.commit()
    await async_session.refresh(oldest)
    assert oldest.sms_registration_id is None
    async with session_scope() as other:
        await other.execute(
            update(PhoneNumber)
            .where(PhoneNumber.id == oldest.id)
            .values(sms_registration_id=row.id, provisioning_metadata={})
        )
        await other.commit()

    carrier = FakeSmsCarrier()
    await sms_registration.attach_if_registered(
        async_session, second, lambda name, purpose="number": carrier
    )
    await async_session.commit()
    assert carrier.attached == []
    await async_session.refresh(second)
    assert second.sms_registration_id is None


async def _person_refused_at_approval(client, key, org_id, async_session, sms_carrier):
    """A person registration approved while the carrier refused its oldest
    number. Returns the registration id and the two numbers."""
    await _set_balance(async_session, org_id, 10_000)
    oldest = await _us_number(async_session, org_id, "+15125550100", resource="PNA")
    second = await _us_number(async_session, org_id, "+15125550101", resource="PNB")
    real_attach = sms_carrier.attach_number
    sms_carrier.refused = {"PNA"}

    async def attach(refs, *, provider_resource_id, e164):
        if provider_resource_id in sms_carrier.refused:
            raise RuntimeError("carrier refused")
        await real_attach(refs, provider_resource_id=provider_resource_id, e164=e164)

    sms_carrier.attach_number = attach
    vid = await _approved_sms_row(
        client, key, async_session, sms_carrier, subject_type="person"
    )
    return vid, oldest, second


async def _numbers_on(async_session, vid) -> list[str]:
    return list(
        (
            await async_session.execute(
                select(PhoneNumber.provider_resource_id)
                .where(PhoneNumber.sms_registration_id == uuid.UUID(vid))
                .order_by(PhoneNumber.provider_resource_id)
            )
        ).scalars()
    )


async def test_background_moves_a_persons_registration_to_the_next_number(
    client, org_and_key, sms_carrier, async_session
):
    org_id, _, key = org_and_key
    vid, oldest, _ = await _person_refused_at_approval(
        client, key, org_id, async_session, sms_carrier
    )
    assert sms_carrier.attached == []
    attached = await _audits(async_session, "verification.sms_attached")
    assert attached[-1].payload == {"numbers": 0, "failed": 1}

    registry = _sms_registry(sms_carrier)
    await verifications_routes.sweep_verifications(async_session, registry)
    assert [r for _, r, _ in sms_carrier.attached] == ["PNB"]
    assert await _numbers_on(async_session, vid) == ["PNB"]

    # The oldest number's wait ends and the carrier takes it now: the
    # registration still holds one number.
    sms_carrier.refused = set()
    await _end_the_wait(async_session, oldest)
    await verifications_routes.sweep_verifications(async_session, registry)
    assert [r for _, r, _ in sms_carrier.attached] == ["PNB"]
    assert await _numbers_on(async_session, vid) == ["PNB"]


async def test_a_persons_registration_takes_one_number_when_the_carrier_accepts_again(
    client, org_and_key, sms_carrier, async_session
):
    # Both numbers are candidates (the oldest was refused, its wait ended):
    # the pass stops at the first that attaches.
    org_id, _, key = org_and_key
    vid, oldest, _ = await _person_refused_at_approval(
        client, key, org_id, async_session, sms_carrier
    )
    sms_carrier.refused = set()
    await _end_the_wait(async_session, oldest)
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    assert [r for _, r, _ in sms_carrier.attached] == ["PNA"]
    assert await _numbers_on(async_session, vid) == ["PNA"]


def _holds(e164: str) -> RegistrationHoldsAnotherNumber:
    return RegistrationHoldsAnotherNumber(
        f"a person's registration holds one number: the carrier has {e164}", e164
    )


async def test_enable_sms_records_the_persons_number_the_carrier_holds(
    client, org_and_key, async_session, sms_mock, monkeypatch
) -> None:
    # The oldest number's link timed out but went through; Hail marked it
    # refused. Trying the next number finds it on the registration.
    from hailhq.api.routes import numbers as numbers_routes

    org_id, _, key = org_and_key
    oldest = await _us_number(
        async_session, org_id, "+15125550100", provider="twilio", resource="PNA"
    )
    second = await _us_number(
        async_session, org_id, "+15125550101", provider="twilio", resource="PNB"
    )
    row, _ = await _person_twilio_row(async_session, org_id, monkeypatch, refused=set())
    oldest.provisioning_metadata = {
        "sms_attach_refused": {
            "registration_id": str(row.id),
            "wait_hours": 1,
            "retry_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "reason_hash": _reason_hash("timed out"),
        }
    }
    await async_session.commit()
    carrier = numbers_routes.get_verification_provider("twilio", "sms")
    tried: list[str] = []

    async def attach(refs, *, provider_resource_id, e164):
        tried.append(provider_resource_id)
        raise _holds("+15125550100")

    monkeypatch.setattr(carrier, "attach_number", attach)
    resp = await client.post(f"/numbers/{second.id}/enable-sms", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert tried == ["PNB"]
    for number in (oldest, second):
        await async_session.refresh(number)
    assert oldest.sms_registration_id == row.id
    assert oldest.messaging_service_sid == "MG1"
    assert "sms_attach_refused" not in oldest.provisioning_metadata
    # The number tried is neither refused nor audited.
    assert second.sms_registration_id is None
    assert "sms_attach_refused" not in second.provisioning_metadata
    assert await _audits(async_session, "verification.sms_attach_refused") == []
    attached = await _audits(async_session, "verification.sms_attached")
    # It names both numbers: support reads why the number tried did not join.
    assert [a.payload for a in attached] == [
        {
            "numbers": 1,
            "failed": 0,
            "adopted": "+15125550100",
            "tried": "+15125550101",
        }
    ]
    assert attached[0].resource_id == row.id


async def test_background_records_the_persons_number_the_carrier_holds(
    client, org_and_key, sms_carrier, async_session
):
    org_id, _, key = org_and_key
    vid, oldest, second = await _person_refused_at_approval(
        client, key, org_id, async_session, sms_carrier
    )
    refused_rows = len(await _audits(async_session, "verification.sms_attach_refused"))

    async def attach(refs, *, provider_resource_id, e164):
        raise _holds("+15125550100")

    sms_carrier.attach_number = attach
    # The oldest number is in its wait: the pass tries the second one.
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    assert await _numbers_on(async_session, vid) == ["PNA"]
    for number in (oldest, second):
        await async_session.refresh(number)
    assert "sms_attach_refused" not in oldest.provisioning_metadata
    assert "sms_attach_refused" not in second.provisioning_metadata
    assert (
        len(await _audits(async_session, "verification.sms_attach_refused"))
        == refused_rows
    )
    attached = await _audits(async_session, "verification.sms_attached")
    assert attached[-1].payload == {"numbers": 1, "failed": 0}


async def test_a_number_the_carrier_holds_outside_the_org_still_refuses(
    client, org_and_key, sms_carrier, async_session
):
    org_id, _, key = org_and_key
    vid, _, second = await _person_refused_at_approval(
        client, key, org_id, async_session, sms_carrier
    )
    # Another organization's number, and one of this org's on another
    # carrier: neither is this registration's number.
    other_org, _, _ = await insert_org_and_key(async_session, org_slug="other")
    await _us_number(async_session, other_org, "+15125550199", resource="PNX")
    await _us_number(
        async_session, org_id, "+15125550198", provider="twilio", resource="PNY"
    )
    for held in ("+15125550199", "+15125550198"):

        async def attach(refs, *, provider_resource_id, e164, held=held):
            raise _holds(held)

        sms_carrier.attach_number = attach
        await verifications_routes.sweep_verifications(
            async_session, _sms_registry(sms_carrier)
        )
        await async_session.refresh(second)
        await _end_the_wait(async_session, second)
    assert await _numbers_on(async_session, vid) == []
    refusal = second.provisioning_metadata["sms_attach_refused"]
    assert refusal["wait_hours"] == 2
    rows = [
        a
        for a in await _audits(async_session, "verification.sms_attach_refused")
        if a.resource_id == second.id
    ]
    # The two reasons differ only by the number the carrier holds: one row.
    assert [r.payload["carrier_reason"] for r in rows] == [
        str(_holds("+15125550199")),
    ]


async def test_a_re_attach_does_not_add_the_number_the_carrier_holds(
    client, org_and_key, async_session, sms_mock, monkeypatch
) -> None:
    # The person's number is on the registration in Hail; the carrier has
    # another of the org's numbers. The registration keeps one number.
    org_id, _, key = org_and_key
    on = await _us_number(
        async_session, org_id, "+15125550100", provider="twilio", resource="PNA"
    )
    other = await _us_number(
        async_session, org_id, "+15125550101", provider="twilio", resource="PNB"
    )
    row, tried = await _person_twilio_row(
        async_session, org_id, monkeypatch, refused=set()
    )
    on.sms_registration_id = row.id
    await async_session.commit()
    from hailhq.api.routes import numbers as numbers_routes

    carrier = numbers_routes.get_verification_provider("twilio", "sms")

    async def attach(refs, *, provider_resource_id, e164):
        tried.append(provider_resource_id)
        raise _holds("+15125550101")

    monkeypatch.setattr(carrier, "attach_number", attach)
    resp = await client.post(f"/numbers/{on.id}/enable-sms", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert tried == ["PNA"]
    assert await _numbers_on(async_session, str(row.id)) == ["PNA"]
    await async_session.refresh(other)
    assert other.sms_registration_id is None
    assert await _audits(async_session, "verification.sms_attached") == []


async def test_a_released_number_still_on_a_persons_registration_keeps_it(
    client, org_and_key, async_session, sms_mock, monkeypatch
) -> None:
    # A release raced the approval: the released number still carries the
    # registration. It counts as the person's number, so no second joins.
    org_id, _, key = org_and_key
    released = await _us_number(
        async_session, org_id, "+15125550100", provider="twilio", resource="PNA"
    )
    second = await _us_number(
        async_session, org_id, "+15125550101", provider="twilio", resource="PNB"
    )
    row, tried = await _person_twilio_row(
        async_session, org_id, monkeypatch, refused=set()
    )
    released.sms_registration_id = row.id
    released.provisioning_state = "released"
    await async_session.commit()

    resp = await client.post(f"/numbers/{second.id}/enable-sms", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert tried == []
    assert await _numbers_on(async_session, str(row.id)) == ["PNA"]
    await async_session.refresh(second)
    assert second.sms_registration_id is None


async def test_recording_the_held_number_clears_the_tried_numbers_wait(
    client, org_and_key, async_session, sms_mock, monkeypatch
) -> None:
    # Both numbers were refused for this registration. enable-sms on the
    # second finds the oldest at the carrier: the second keeps no wait.
    from hailhq.api.routes import numbers as numbers_routes

    org_id, _, key = org_and_key
    oldest = await _us_number(
        async_session, org_id, "+15125550100", provider="twilio", resource="PNA"
    )
    second = await _us_number(
        async_session, org_id, "+15125550101", provider="twilio", resource="PNB"
    )
    row, _ = await _person_twilio_row(async_session, org_id, monkeypatch, refused=set())
    for number in (oldest, second):
        number.provisioning_metadata = {
            "sms_attach_refused": {
                "registration_id": str(row.id),
                "wait_hours": 2,
                "retry_at": (
                    datetime.now(timezone.utc) + timedelta(hours=2)
                ).isoformat(),
                "reason_hash": _reason_hash("timed out"),
            }
        }
    await async_session.commit()
    carrier = numbers_routes.get_verification_provider("twilio", "sms")

    async def attach(refs, *, provider_resource_id, e164):
        raise _holds("+15125550100")

    monkeypatch.setattr(carrier, "attach_number", attach)
    resp = await client.post(f"/numbers/{second.id}/enable-sms", headers=_auth(key))
    assert resp.status_code == 200, resp.text
    assert await _numbers_on(async_session, str(row.id)) == ["PNA"]
    await async_session.refresh(second)
    assert "sms_attach_refused" not in second.provisioning_metadata


async def test_the_held_number_is_locked_and_checked_again(
    client, org_and_key, async_session, session_factory, sms_mock, monkeypatch
) -> None:
    # Another transaction releases the held number while Hail records it.
    # Hail waits for that row and then sees it released: nothing joins.
    from hailhq.api.routes import numbers as numbers_routes

    org_id, _, key = org_and_key
    held = await _us_number(
        async_session, org_id, "+15125550100", provider="twilio", resource="PNA"
    )
    second = await _us_number(
        async_session, org_id, "+15125550101", provider="twilio", resource="PNB"
    )
    row, _ = await _person_twilio_row(async_session, org_id, monkeypatch, refused=set())
    held.provisioning_metadata = {
        "sms_attach_refused": {
            "registration_id": str(row.id),
            "wait_hours": 1,
            "retry_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "reason_hash": _reason_hash("timed out"),
        }
    }
    await async_session.commit()
    carrier = numbers_routes.get_verification_provider("twilio", "sms")

    async def attach(refs, *, provider_resource_id, e164):
        raise _holds("+15125550100")

    monkeypatch.setattr(carrier, "attach_number", attach)
    async with session_factory() as other:
        await other.execute(
            update(PhoneNumber)
            .where(PhoneNumber.id == held.id)
            .values(provisioning_state="released")
        )
        request = asyncio.create_task(
            client.post(f"/numbers/{second.id}/enable-sms", headers=_auth(key))
        )
        await asyncio.sleep(0.5)
        await other.commit()
    resp = await request
    assert resp.status_code == 200, resp.text
    assert await _numbers_on(async_session, str(row.id)) == []
    await async_session.refresh(second)
    assert second.provisioning_metadata["sms_attach_refused"]["registration_id"] == (
        str(row.id)
    )


async def test_a_business_registration_still_takes_every_number(
    client, org_and_key, sms_carrier, async_session
):
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    oldest = await _us_number(async_session, org_id, "+15125550100", resource="PNA")
    await _us_number(async_session, org_id, "+15125550101", resource="PNB")
    real_attach = sms_carrier.attach_number
    refused = {"PNA"}

    async def attach(refs, *, provider_resource_id, e164):
        if provider_resource_id in refused:
            raise RuntimeError("carrier refused")
        await real_attach(refs, provider_resource_id=provider_resource_id, e164=e164)

    sms_carrier.attach_number = attach
    vid = await _approved_sms_row(client, key, async_session, sms_carrier)
    assert await _numbers_on(async_session, vid) == ["PNB"]
    attached = await _audits(async_session, "verification.sms_attached")
    assert attached[-1].payload == {"numbers": 1, "failed": 1}

    registry = _sms_registry(sms_carrier)
    await verifications_routes.sweep_verifications(async_session, registry)
    assert [r for _, r, _ in sms_carrier.attached] == ["PNB"]  # PNA waits

    refused.clear()
    await _end_the_wait(async_session, oldest)
    await _us_number(async_session, org_id, "+15125550102", resource="PNC")
    await verifications_routes.sweep_verifications(async_session, registry)
    assert await _numbers_on(async_session, vid) == ["PNA", "PNB", "PNC"]
    attached = await _audits(async_session, "verification.sms_attached")
    assert attached[-1].payload == {"numbers": 2, "failed": 0}


@pytest.mark.usefixtures("us_number")
async def test_a_changed_carrier_reason_gets_a_new_audit_row(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    # An outage wrote the first row; the real refusal must not hide behind it.
    org_id, _, key = org_and_key
    number, vid, attempts = await _refused_territory_number(
        client, key, org_id, sms_carrier, async_session, monkeypatch
    )
    reasons = iter(["carrier down", "carrier down", "number not eligible"] * 2)

    async def refused(refs, *, provider_resource_id, e164):
        attempts.append(provider_resource_id)
        raise RuntimeError(next(reasons))

    monkeypatch.setattr(sms_carrier, "attach_number", refused)
    registry = _sms_registry(sms_carrier)

    for _ in range(4):
        await verifications_routes.sweep_verifications(async_session, registry)
        await _end_the_wait(async_session, number)
    assert len(attempts) == 4

    rows = await _audits(async_session, "verification.sms_attach_refused")
    assert [r.payload["carrier_reason"] for r in rows] == [
        "carrier down",
        "number not eligible",
        "carrier down",
    ]
    assert all(r.payload["verification_id"] == vid for r in rows)
    assert number.provisioning_metadata["sms_attach_refused"]["wait_hours"] == 8


@pytest.mark.usefixtures("us_number")
async def test_the_same_carrier_reason_gets_no_new_audit_row(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    org_id, _, key = org_and_key
    number, _, attempts = await _refused_territory_number(
        client, key, org_id, sms_carrier, async_session, monkeypatch
    )
    registry = _sms_registry(sms_carrier)
    for _ in range(3):
        await verifications_routes.sweep_verifications(async_session, registry)
        await _end_the_wait(async_session, number)
    assert len(attempts) == 3
    assert len(await _audits(async_session, "verification.sms_attach_refused")) == 1
    refusal = number.provisioning_metadata["sms_attach_refused"]
    assert refusal["reason_hash"] == _reason_hash("carrier refused")
    assert "carrier refused" not in json.dumps(refusal)


@pytest.mark.usefixtures("us_number")
async def test_retry_state_without_a_reason_hash_audits_once_more(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    # State written before the hash was kept: the row it wrote may hold
    # another reason, so the next refusal is audited.
    org_id, _, key = org_and_key
    number, vid, _ = await _refused_territory_number(
        client, key, org_id, sms_carrier, async_session, monkeypatch
    )
    number.provisioning_metadata = {
        "sms_attach_refused": {
            "registration_id": vid,
            "wait_hours": 1,
            "retry_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        }
    }
    await async_session.commit()
    registry = _sms_registry(sms_carrier)
    await verifications_routes.sweep_verifications(async_session, registry)
    await _end_the_wait(async_session, number)
    await verifications_routes.sweep_verifications(async_session, registry)
    assert len(await _audits(async_session, "verification.sms_attach_refused")) == 1
    assert number.provisioning_metadata["sms_attach_refused"]["wait_hours"] == 4


async def test_a_superadmin_reject_whose_discard_fails_answers_502(
    client, org_and_key, carrier, async_session
) -> None:
    _, _, key = org_and_key
    vid = await _unsent(client, key, carrier, async_session)

    async def boom(refs):
        refs["filed_id"] = "F1"  # half done
        raise RuntimeError("carrier down")

    carrier.discard = boom
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    try:
        resp = await client.post(
            f"/admin/verifications/{vid}/reject",
            json={"reason": "no"},
            headers=_auth(key),
        )
    finally:
        app.dependency_overrides.pop(require_superadmin, None)
    assert resp.status_code == 502, resp.text
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "awaiting_review"
    assert row.provider_refs == {"bundle_sid": "B1", "filed_id": "F1"}
    assert (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "verification.reject")
        )
    ).scalar_one_or_none() is None


async def test_a_status_poll_that_fails_partway_keeps_only_the_ids_it_added(
    client, org_and_key, carrier, async_session
) -> None:
    from hailhq.api.routes.verifications import sweep_verifications

    _, _, key = org_and_key
    vid = (await _create(client, key)).json()["id"]

    async def status(refs):
        refs["campaign_id"] = "C1"
        refs.pop("bundle_sid")
        raise RuntimeError("carrier down")

    carrier.status = status
    verifications_routes._last_polled.clear()
    await sweep_verifications(
        async_session,
        lambda name, purpose="number": carrier if name == "fake" else None,
    )
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "submitted"
    assert row.provider_refs == {"bundle_sid": "B1", "campaign_id": "C1"}


@pytest.mark.usefixtures("us_number")
async def test_a_failed_sms_status_poll_still_stamps_the_campaign_it_filed(
    client, org_and_key, sms_carrier, async_session
) -> None:
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 20_000)
    sms_carrier.campaign_key = "campaign_id"
    resp = await _post_sms(client, key)
    assert resp.status_code == 201, resp.text
    vid = resp.json()["id"]

    async def status(refs, *, may_file_campaign=True):
        assert may_file_campaign
        refs["campaign_id"] = "C1"
        raise RuntimeError("carrier down")

    sms_carrier.status = status
    verifications_routes._last_polled.clear()
    await verifications_routes.sweep_verifications(
        async_session, _sms_registry(sms_carrier)
    )
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.provider_refs["campaign_id"] == "C1"
    assert row.provider_refs["campaign_filed_at"]


@pytest.mark.usefixtures("us_number")
async def test_saved_sms_application_is_private_and_survives_repair(
    client, org_and_key, sms_carrier, async_session
):
    _, _, key = org_and_key
    response = await _post_sms(
        client,
        key,
        fields=json.dumps(
            {
                "legal_name": "Acme",
                "campaign_description": "Appointment reminders",
                "unrecognized_secret": "must not be stored",
            }
        ),
    )
    assert response.status_code == 201, response.text
    vid = response.json()["id"]
    assert "fields" not in response.json() and "application" not in response.text
    path = f"/verifications/{vid}/application"
    saved = await client.get(path, headers=_auth(key))
    assert saved.status_code == 200
    assert saved.headers["cache-control"] == "no-store"
    assert saved.json()["fields"] == {
        "legal_name": "Acme",
        "campaign_description": "Appointment reminders",
    }
    assert saved.json()["address"]["country_code"] == "US"
    _, _, other_key = await insert_org_and_key(async_session)
    assert (await client.get(path, headers=_auth(other_key))).status_code == 404
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    row.state = "rejected"
    await async_session.commit()
    cancelled = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert cancelled.status_code == 200, cancelled.text
    repaired = await client.get(path, headers=_auth(key))
    assert repaired.json() == saved.json()
    # Reading a saved application performs no submission or provider poll.
    assert len(sms_carrier.drafts) == 1 and sms_carrier.status_calls == 0


@pytest.mark.usefixtures("us_number")
async def test_legacy_application_has_actionable_missing_answers(
    client, org_and_key, sms_carrier, async_session
):
    _, _, key = org_and_key
    response = await _post_sms(client, key)
    vid = response.json()["id"]
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    row.provider_refs = {"brand_sid": "BN1"}
    await async_session.commit()
    saved = await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    assert saved.status_code == 404
    assert "Complete the current registration form" in saved.json()["detail"]


@pytest.mark.usefixtures("us_number")
async def test_expired_answers_are_not_rebuilt_from_the_carrier(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    from datetime import datetime, timedelta, timezone

    _, _, key = org_and_key
    asked = []

    async def identity(refs, subject_type):
        asked.append(refs)
        return {"fields": {}, "address": {}}

    monkeypatch.setattr(sms_carrier, "application_identity", identity)
    vid = (await _post_sms(client, key)).json()["id"]
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    row.state = "rejected"
    row.updated_at = datetime.now(timezone.utc) - timedelta(days=31)
    row.provider_refs = {"brand_sid": "BN1"}
    await async_session.commit()
    saved = await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    assert saved.status_code == 404 and asked == []
    # Stored answers past the limit are gone before the background pass runs.
    row.provider_refs = {"brand_sid": "BN1", "application": {"fields": {}}}
    await async_session.commit()
    saved = await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    assert saved.status_code == 404 and asked == []
    # Dismissing it later does not make the expired answers readable again.
    cancelled = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert cancelled.status_code == 200, cancelled.text
    await async_session.refresh(row)
    assert "application" not in row.provider_refs
    saved = await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    assert saved.status_code == 404


@pytest.mark.usefixtures("us_number")
async def test_rejecting_a_long_waiting_application_keeps_its_answers(
    client, org_and_key, sms_carrier, async_session
):
    from datetime import datetime, timedelta, timezone

    _, _, key = org_and_key
    vid = (await _post_sms(client, key)).json()["id"]
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    row.state = "awaiting_review"
    row.updated_at = datetime.now(timezone.utc) - timedelta(days=31)
    row.provider_refs = {**(row.provider_refs or {}), "application": {"fields": {}}}
    await async_session.commit()
    app.dependency_overrides[require_superadmin] = lambda: SimpleNamespace(
        user_id=uuid.uuid4(), api_key_id=None, superadmin=True
    )
    resp = await client.post(
        f"/admin/verifications/{vid}/reject",
        json={"reason": "Website unreachable"},
        headers=_auth(key),
    )
    app.dependency_overrides.pop(require_superadmin)
    assert resp.status_code == 200, resp.text
    await async_session.refresh(row)
    # The 30 days start at the rejection, not at the last change before it.
    assert "application" in row.provider_refs


@pytest.mark.usefixtures("us_number")
async def test_old_ended_applications_lose_their_saved_answers(
    client, org_and_key, sms_carrier, async_session
):
    from datetime import datetime, timedelta, timezone

    from hailhq.api.routes.verifications import forget_old_applications

    _, _, key = org_and_key
    now = datetime.now(timezone.utc)
    rows = {}
    for name, state, age in (
        ("old_cancelled", "cancelled", 31),
        ("old_rejected", "rejected", 31),
        ("new_cancelled", "cancelled", 29),
        ("old_submitted", "submitted", 31),
    ):
        vid = (await _post_sms(client, key)).json()["id"]
        row = await async_session.get(CarrierVerification, uuid.UUID(vid))
        row.state = state
        row.cancelled_at = now - timedelta(days=age) if state == "cancelled" else None
        row.updated_at = now - timedelta(days=age)
        row.provider_refs = {"brand_reused": True, "application": {"fields": {}}}
        rows[name] = row
        await async_session.commit()
    assert await forget_old_applications(async_session, now) == 2
    for name, row in rows.items():
        await async_session.refresh(row)
        kept = "application" in row.provider_refs
        assert kept is (name in ("new_cancelled", "old_submitted")), name
        # Only the answers go: the billing mark stays.
        assert row.provider_refs["brand_reused"] is True


@pytest.mark.usefixtures("us_number")
async def test_campaign_acceptance_is_distinct_from_runtime_readiness(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    _, _, key = org_and_key
    response = await _post_sms(client, key)
    vid = response.json()["id"]
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    row.provider_refs = {
        **row.provider_refs,
        "campaign_accepted": True,
        "runtime_ready": False,
    }
    await async_session.commit()
    monkeypatch.setattr(sms_carrier, "step", lambda refs: "runtime_setup")
    response = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert response.status_code == 200
    assert response.json()["state"] == "submitted"
    assert response.json()["step"] == "runtime_setup"
    assert response.json()["campaign_accepted"] is True
    assert response.json()["runtime_ready"] is False
    # The console shows Cancel from this flag, not its own copy of the rule.
    assert response.json()["cancellable"] is True


@pytest.mark.usefixtures("us_number")
async def test_sms_preview_is_exact_and_has_no_carrier_writes(
    client, org_and_key, sms_carrier, monkeypatch
):
    from dataclasses import asdict

    from hailhq.core.providers.verification.sms_form import (
        campaign_text,
        sms_requirements,
    )

    async def requirements(country_code, number_type, subject_type):
        return sms_requirements(sms_carrier, subject_type)

    monkeypatch.setattr(sms_carrier, "requirements", requirements)
    _, _, key = org_and_key
    response = await client.post(
        "/verifications/sms/preview",
        headers=_auth(key),
        json={
            "subject_type": "business",
            "fields": _TELNYX_BUSINESS,
            "address": json.loads(_sms_submission()["address"]),
        },
    )
    assert response.status_code == 200, response.text
    expected = json.loads(
        json.dumps(asdict(campaign_text(_TELNYX_BUSINESS, "business")))
    )
    assert response.json() == expected
    assert not sms_carrier.drafts and not sms_carrier.submitted
    invalid = await client.post(
        "/verifications/sms/preview",
        headers=_auth(key),
        json={
            "subject_type": "business",
            "fields": {**_TELNYX_BUSINESS, "consent_methods": ""},
            "address": json.loads(_sms_submission()["address"]),
        },
    )
    assert invalid.status_code == 422
    assert any(
        item["loc"] == ["body", "consent_methods"] for item in invalid.json()["detail"]
    )
    assert not sms_carrier.drafts


@pytest.mark.usefixtures("us_number")
async def test_person_identity_mobile_cannot_be_a_hail_number(
    client, org_and_key, sms_carrier, async_session
):
    org_id, _, key = org_and_key
    number = (
        (
            await async_session.execute(
                select(PhoneNumber).where(PhoneNumber.organization_id == org_id)
            )
        )
        .scalars()
        .first()
    )
    # Normalize formatting before comparing with allocated numbers.
    response = await _post_sms(
        client,
        key,
        subject_type="person",
        fields=json.dumps(
            {
                "legal_name": "Ada",
                "mobile_phone_number": number.e164[:2] + " " + number.e164[2:],
            }
        ),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "mobile_phone_number"]
    assert not sms_carrier.drafts


async def _needs_information_application(
    client, key, sms_carrier, async_session, monkeypatch, subject="business", saved=True
):
    from hailhq.core.providers.verification.sms_form import sms_requirements

    fields = dict(_TELNYX_PERSON if subject == "person" else _TELNYX_BUSINESS)
    address = json.loads(_sms_submission()["address"])

    async def requirements(country_code, number_type, subject_type):
        return sms_requirements(sms_carrier, subject_type)

    async def recover(refs):
        return False

    async def identity(refs, subject_type):
        return {"fields": fields, "address": address}

    monkeypatch.setattr(sms_carrier, "requirements", requirements)
    monkeypatch.setattr(sms_carrier, "recover_campaign", recover)
    monkeypatch.setattr(sms_carrier, "application_identity", identity)
    sms_carrier.campaign_key = "campaign_id"
    response = await _post_sms(
        client, key, subject_type=subject, fields=json.dumps(fields)
    )
    assert response.status_code == 201, response.text
    vid = response.json()["id"]
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    refs = {
        **row.provider_refs,
        "needs_information": True,
        "identity_verified": True,
        "campaign": {},
    }
    if not saved:
        refs.pop("application", None)
    row.provider_refs = refs
    await async_session.commit()
    return vid, {"fields": fields, "address": address}


@pytest.mark.usefixtures("us_number")
async def test_amendment_says_when_the_campaign_waits_for_credit(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    # The repaired row is next to file its campaign: the amend reply, and
    # its identical retry, say whether the credit covers it.
    org_id, _, key = org_and_key
    await _set_balance(async_session, org_id, 10_000)
    vid, answers = await _needs_information_application(
        client, key, sms_carrier, async_session, monkeypatch
    )
    await _charge_submission(async_session, org_id, vid)
    await _set_balance(async_session, org_id, _campaign_fee() - 1)
    path = f"/verifications/{vid}/application"
    for _ in range(2):
        resp = await client.patch(path, headers=_auth(key), json=answers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["waiting_for_credit"] is True
    assert sms_carrier.campaigns_created == 0


@pytest.mark.usefixtures("us_number")
@pytest.mark.parametrize("subject", ["person", "business"])
@pytest.mark.parametrize("saved", [True, False])
async def test_amendment_keeps_same_row_brand_and_original_charge(
    client, org_and_key, sms_carrier, async_session, monkeypatch, subject, saved
):
    org_id, _, key = org_and_key
    vid, answers = await _needs_information_application(
        client, key, sms_carrier, async_session, monkeypatch, subject, saved
    )
    initial = await _balance(async_session, org_id)
    # Reading/list polling an incomplete application does not file anything.
    waiting = await client.get(f"/verifications/{vid}", headers=_auth(key))
    assert (
        waiting.json()["state"] == "submitted"
        and waiting.json()["step"] == "needs_information"
    )
    assert sms_carrier.status_calls == 0
    first = await client.patch(
        f"/verifications/{vid}/application", headers=_auth(key), json=answers
    )
    assert first.status_code == 200, first.text
    assert first.json()["id"] == vid and first.json()["state"] == "submitted"
    assert first.json()["step"] == "filing"
    assert sms_carrier.status_calls == 0 and len(sms_carrier.drafts) == 1
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert (
        row.provider_refs["brand_sid"] == "BN1"
        and "needs_information" not in row.provider_refs
    )
    assert await _balance(async_session, org_id) == initial
    # The later normal poll creates a campaign once; identical PATCH stays safe.
    await client.get(f"/verifications/{vid}", headers=_auth(key))
    repeat = await client.patch(
        f"/verifications/{vid}/application", headers=_auth(key), json=answers
    )
    assert repeat.status_code == 200 and repeat.json()["id"] == vid
    assert sms_carrier.campaigns_created == 1 and len(sms_carrier.drafts) == 1
    # One audit row for the change, none for the identical retry, no answers.
    audits = (
        (
            await async_session.execute(
                select(AuditLog).where(AuditLog.action == "verification.amend")
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1 and str(audits[0].resource_id) == vid
    assert set(audits[0].payload) == {"requirements_version"}


@pytest.mark.usefixtures("us_number")
async def test_same_answers_lift_a_new_hold_after_an_amendment(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    _, _, key = org_and_key
    vid, answers = await _needs_information_application(
        client, key, sms_carrier, async_session, monkeypatch
    )
    first = await client.patch(
        f"/verifications/{vid}/application", headers=_auth(key), json=answers
    )
    assert first.status_code == 200, first.text
    # The carrier refuses the amended campaign (say, a policy page was down).
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    row.provider_refs = {
        **row.provider_refs,
        "needs_information": True,
        "campaign_needs_repair": True,
    }
    row.rejection_reason = "Review your privacy policy."
    await async_session.commit()
    again = await client.patch(
        f"/verifications/{vid}/application", headers=_auth(key), json=answers
    )
    assert again.status_code == 200 and again.json()["step"] == "filing"
    await async_session.refresh(row)
    assert "needs_information" not in row.provider_refs
    assert row.rejection_reason is None


@pytest.mark.usefixtures("us_number")
async def test_amendment_concurrent_retries_are_idempotent(
    client, org_and_key, sms_carrier, async_session, monkeypatch, session_factory
):
    _, _, key = org_and_key
    vid, answers = await _needs_information_application(
        client, key, sms_carrier, async_session, monkeypatch
    )
    restore = _per_request_sessions(session_factory)
    try:
        responses = await asyncio.gather(
            *(
                client.patch(
                    f"/verifications/{vid}/application",
                    headers=_auth(key),
                    json=answers,
                )
                for _ in range(2)
            )
        )
    finally:
        restore()
    assert [response.status_code for response in responses] == [200, 200]
    assert len(sms_carrier.drafts) == 1 and sms_carrier.status_calls == 0


@pytest.mark.usefixtures("us_number")
@pytest.mark.parametrize(
    "change",
    [
        {"legal_name": "Different Company"},
        {"ein": "99-9999999"},
        {"rep_email": "different@acme.org"},
    ],
)
async def test_amendment_cannot_replace_original_sender(
    client, org_and_key, sms_carrier, async_session, monkeypatch, change
):
    _, _, key = org_and_key
    vid, answers = await _needs_information_application(
        client, key, sms_carrier, async_session, monkeypatch
    )
    answers["fields"] = {**answers["fields"], **change}
    response = await client.patch(
        f"/verifications/{vid}/application", headers=_auth(key), json=answers
    )
    assert response.status_code == 409, response.text
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.state == "submitted" and row.provider_refs["needs_information"]
    assert row.provider_refs["brand_sid"] == "BN1" and len(sms_carrier.drafts) == 1


@pytest.mark.usefixtures("us_number")
async def test_amendment_unverifiable_identity_requires_support_and_retains_brand(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    _, _, key = org_and_key
    vid, answers = await _needs_information_application(
        client, key, sms_carrier, async_session, monkeypatch, saved=False
    )

    async def missing(refs, subject):
        return None

    monkeypatch.setattr(sms_carrier, "application_identity", missing)
    response = await client.patch(
        f"/verifications/{vid}/application", headers=_auth(key), json=answers
    )
    assert response.status_code == 409 and "support" in response.json()["detail"]
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert row.provider_refs["brand_sid"] == "BN1" and row.state == "submitted"


@pytest.mark.usefixtures("us_number")
async def test_amendment_is_org_scoped_and_validates_before_any_filing(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    _, _, key = org_and_key
    vid, answers = await _needs_information_application(
        client, key, sms_carrier, async_session, monkeypatch
    )
    _, _, other_key = await insert_org_and_key(async_session)
    response = await client.patch(
        f"/verifications/{vid}/application", headers=_auth(other_key), json=answers
    )
    assert response.status_code == 404
    bad = {**answers, "fields": {**answers["fields"], "consent_methods": ""}}
    response = await client.patch(
        f"/verifications/{vid}/application", headers=_auth(key), json=bad
    )
    assert response.status_code == 422 and len(sms_carrier.drafts) == 1
    assert sms_carrier.status_calls == 0


@pytest.mark.usefixtures("us_number")
async def test_amendment_recovers_lost_campaign_before_changing_answers(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    _, _, key = org_and_key
    vid, answers = await _needs_information_application(
        client, key, sms_carrier, async_session, monkeypatch
    )

    async def found(refs):
        refs["campaign_id"] = "Cexisting"
        refs.pop("needs_information", None)
        return True

    monkeypatch.setattr(sms_carrier, "recover_campaign", found)
    response = await client.patch(
        f"/verifications/{vid}/application", headers=_auth(key), json=answers
    )
    assert response.status_code == 409 and "recovered" in response.json()["detail"]
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    await async_session.refresh(row)
    assert (
        row.provider_refs["campaign_id"] == "Cexisting"
        and row.provider_refs["campaign"] == {}
    )
    assert row.provider_refs["brand_sid"] == "BN1" and len(sms_carrier.drafts) == 1


@pytest.mark.usefixtures("us_number")
async def test_saved_legacy_identity_recovery_does_not_submit(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    _, _, key = org_and_key
    vid, answers = await _needs_information_application(
        client, key, sms_carrier, async_session, monkeypatch, saved=False
    )
    response = await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    assert response.status_code == 200, response.text
    assert response.json()["fields"]["ein"] == answers["fields"]["ein"]
    assert (
        response.headers["cache-control"] == "no-store" and len(sms_carrier.drafts) == 1
    )


@pytest.mark.usefixtures("us_number")
async def test_accepted_runtime_setup_can_cancel_and_rates_before_discard(
    client, org_and_key, sms_carrier, async_session, rated
):
    _, _, key = org_and_key
    sms_carrier.campaign_key = "campaign_id"
    vid = (await _post_sms(client, key)).json()["id"]
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    row.provider_refs = {
        **row.provider_refs,
        "campaign_id": "C1",
        "campaign_accepted": True,
        "runtime_ready": False,
    }
    await async_session.commit()
    response = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "cancelled"
    assert rated[0][1]["campaign_id"] == "C1" and sms_carrier.discarded


@pytest.mark.usefixtures("us_number")
async def test_runtime_cancellation_rechecks_after_billing_and_detaches_racing_approval(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    org_id, _, key = org_and_key
    sms_carrier.campaign_key = "campaign_id"
    vid = (await _post_sms(client, key)).json()["id"]
    row = await async_session.get(CarrierVerification, uuid.UUID(vid))
    row.provider_refs = {
        **row.provider_refs,
        "campaign_id": "C1",
        "campaign_accepted": True,
        "runtime_ready": False,
    }
    await async_session.commit()

    async def approve_while_rated(_):
        await async_session.refresh(row)
        row.state = "approved"
        number = (
            (
                await async_session.execute(
                    select(PhoneNumber).where(PhoneNumber.organization_id == org_id)
                )
            )
            .scalars()
            .first()
        )
        number.sms_registration_id = row.id
        await async_session.commit()

    monkeypatch.setattr(
        verifications_routes, "rate_sms_registration", approve_while_rated
    )
    response = await client.delete(f"/verifications/{vid}", headers=_auth(key))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "cancelled" and sms_carrier.detached
    number = (
        (
            await async_session.execute(
                select(PhoneNumber)
                .where(PhoneNumber.organization_id == org_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    assert number.sms_registration_id is None


@pytest.mark.usefixtures("us_number")
async def test_incomplete_legacy_provider_address_returns_support_guidance(
    client, org_and_key, sms_carrier, async_session, monkeypatch
):
    _, _, key = org_and_key
    vid, _ = await _needs_information_application(
        client, key, sms_carrier, async_session, monkeypatch, saved=False
    )

    async def incomplete(refs, subject):
        return {
            "fields": {"legal_name": "Acme"},
            "address": {"country_code": "US", "street": None},
        }

    monkeypatch.setattr(sms_carrier, "application_identity", incomplete)
    response = await client.get(f"/verifications/{vid}/application", headers=_auth(key))
    assert response.status_code == 404 and "support" in response.json()["detail"]


def test_a_hold_found_while_submitting_keeps_its_repair_guidance() -> None:
    from datetime import datetime, timezone

    row = CarrierVerification(
        state="submitting",
        purpose="sms",
        updated_at=datetime.now(timezone.utc),
        provider_refs={"needs_information": True, "campaign_needs_repair": True},
    )
    status = ProviderStatus(state="pending", reason="Review the terms.", raw_reason="x")
    assert verifications_routes._apply_status(row, status) is True
    assert row.state == "submitted" and row.rejection_reason == "Review the terms."


async def test_a_legacy_evidence_hold_is_not_audited_as_a_refusal(monkeypatch) -> None:
    from datetime import datetime, timezone

    written: list = []

    async def fake_audit(*args, **kwargs):
        written.append(args[2])

    monkeypatch.setattr(verifications_routes, "write_audit_log", fake_audit)
    row = CarrierVerification(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        state="submitted",
        purpose="sms",
        updated_at=datetime.now(timezone.utc),
        provider_refs={"needs_information": True},
    )
    legacy = ProviderStatus(state="pending", reason="Complete the evidence.")
    refs = dict(row.provider_refs)
    assert await verifications_routes._poll_and_apply(
        None, row, legacy, lambda *a: None, refs
    )
    assert row.rejection_reason == "Complete the evidence." and written == []
    row.provider_refs = {**refs, "campaign_needs_repair": True}
    refused = ProviderStatus(
        state="pending", reason="Review the terms.", raw_reason="x"
    )
    assert await verifications_routes._poll_and_apply(
        None, row, refused, lambda *a: None, dict(row.provider_refs)
    )
    assert written == ["verification.campaign_refused"]


async def test_refusal_audit_is_dropped_on_rollback(async_session):
    """A rolled-back refusal leaves no audit row; a committed one writes it."""
    db = async_session
    number = SimpleNamespace(id=uuid.uuid4(), e164="+15550001111")
    org = uuid.uuid4()
    await db.execute(select(1))
    sms_registration._audit_after_commit(db, org, number.id, {"e164": number.e164})
    await db.rollback()
    await sms_registration.drain_audits()
    assert db.sync_session.info.get("sms_attach_refused_audits") is None
