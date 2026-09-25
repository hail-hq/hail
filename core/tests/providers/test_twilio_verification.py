"""Unit tests for ``TwilioVerificationProvider``.

Twilio is mocked at the HTTP boundary (``responses``), and the requirement
data is Twilio's real Regulations response saved under ``fixtures/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest
import responses
from hailhq.core.providers.verification import (
    Address,
    DocumentInput,
    DocumentSlot,
    UnsupportedSubjectType,
    UploadedFile,
    get_verification_provider,
)
from hailhq.core.providers.verification.twilio import TwilioVerificationProvider

ACCOUNT_SID = "ACtest1234567890abcdef1234567890ab"
ORG = "11111111-2222-3333-4444-555555555555"
CONTACT = "ops@hail.test"
NUMBERS = "https://numbers.twilio.com/v2/RegulatoryCompliance"
UPLOAD = "https://numbers-upload.twilio.com/v2/RegulatoryCompliance/SupportingDocuments"
ADDRESSES = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Addresses.json"
FIXTURES = Path(__file__).parent / "fixtures"


def _regs(name: str) -> dict:
    return json.loads((FIXTURES / f"twilio-regulations-{name}.json").read_text())


def _empty_regs() -> dict:
    return {
        "results": [],
        "meta": {
            "page": 0,
            "page_size": 50,
            "first_page_url": NUMBERS + "/Regulations",
            "previous_page_url": None,
            "url": NUMBERS + "/Regulations",
            "next_page_url": None,
            "key": "results",
        },
    }


@pytest.fixture()
def provider() -> TwilioVerificationProvider:
    return TwilioVerificationProvider(account_sid=ACCOUNT_SID, auth_token="tok")


def _regs_response(name: str) -> None:
    responses.add(responses.GET, f"{NUMBERS}/Regulations", json=_regs(name), status=200)


PERSON_FIELDS = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone_number": "+351911000000",
}
ADDRESS = Address(
    customer_name="Ada Lovelace",
    street="Rua Um 1",
    city="Lisboa",
    region="Lisboa",
    postal_code="1000-001",
    country_code="PT",
)


def _passport() -> dict[str, DocumentInput]:
    return {
        "proof_of_identity": DocumentInput(
            option="passport",
            file=UploadedFile(
                filename="ada-lovelace-passport.jpg",
                content_type="image/jpeg",
                data=b"\xff\xd8bytes",
            ),
        )
    }


# -- requirements ---------------------------------------------------------


@responses.activate
async def test_requirements_uk_mobile_person(provider) -> None:
    _regs_response("GB-mobile-individual")
    req = await provider.requirements("GB", "mobile", "person")

    assert req.required is True
    assert req.subject_type == "person"
    assert {f.name for f in req.fields} >= {
        "first_name",
        "last_name",
        "email",
        "phone_number",
    }
    email = next(f for f in req.fields if f.name == "email")
    assert email.kind == "email" and email.required
    assert next(f for f in req.fields if f.name == "comments").required is False

    identity, address_proof = req.documents
    assert identity.needs_input is True
    assert [o.key for o in identity.options] == [
        "government_issued_document",
        "passport",
    ]
    passport = identity.options[1]
    assert passport.file_required is True
    assert passport.fields == ()  # names are copied from the person, not asked again
    assert passport.copies == ("first_name", "last_name")
    assert address_proof.needs_input is False  # just the address the customer gave
    assert address_proof.options[0].file_required is False
    assert address_proof.options[0].needs_address is True
    assert req.address_required is True

    qs = parse_qs(responses.calls[0].request.url.split("?", 1)[1])
    assert qs["IsoCountry"] == ["GB"]
    assert qs["NumberType"] == ["mobile"]
    assert (
        req.version == (await provider.requirements("GB", "mobile", "person")).version
    )


@responses.activate
async def test_requirements_germany_business_needs_files_and_address(provider) -> None:
    _regs_response("DE-local-business")
    req = await provider.requirements("DE", "local", "business")

    assert len(req.documents) == 3
    assert all(o.file_required for s in req.documents for o in s.options)
    assert req.address_required is True
    # registration number is a subject field, so documents copy it
    proof = next(s for s in req.documents if "Registration" in s.label)
    assert "business_registration_number" in proof.options[0].copies


@responses.activate
async def test_requirements_none_needed(provider) -> None:
    responses.add(
        responses.GET, f"{NUMBERS}/Regulations", json=_empty_regs(), status=200
    )
    req = await provider.requirements("US", "local", "person")
    assert req.required is False
    assert req.fields == () and req.documents == ()


@responses.activate
async def test_requirements_unsupported_subject_type(provider) -> None:
    _regs_response("GB-mobile-business")  # only business is offered
    with pytest.raises(UnsupportedSubjectType) as excinfo:
        await provider.requirements("GB", "mobile", "person")
    assert excinfo.value.allowed == ("business",)


# -- create_draft ---------------------------------------------------------


def _register_draft_calls(evaluation: dict) -> None:
    responses.add(responses.POST, ADDRESSES, json={"sid": "AD" + "1" * 32}, status=201)
    responses.add(
        responses.POST, f"{NUMBERS}/EndUsers", json={"sid": "IT" + "1" * 32}, status=201
    )
    responses.add(
        responses.POST,
        f"{NUMBERS}/SupportingDocuments",
        json={"sid": "RD" + "1" * 32},
        status=201,
    )
    responses.add(responses.POST, UPLOAD, json={"sid": "RD" + "2" * 32}, status=201)
    responses.add(
        responses.POST, f"{NUMBERS}/Bundles", json={"sid": "BU" + "1" * 32}, status=201
    )
    responses.add(
        responses.POST,
        f"{NUMBERS}/Bundles/BU{'1' * 32}/ItemAssignments",
        json={"sid": "BV" + "1" * 32},
        status=201,
    )
    responses.add(
        responses.POST,
        f"{NUMBERS}/Bundles/BU{'1' * 32}/Evaluations",
        json=evaluation,
        status=201,
    )


async def _person_requirements(provider):
    _regs_response("GB-mobile-individual")
    req = await provider.requirements("GB", "mobile", "person")
    responses.reset()
    return req


@responses.activate
async def test_create_draft_reports_missing_input_without_calling_the_carrier(
    provider,
) -> None:
    req = await _person_requirements(provider)
    result = await provider.create_draft(
        organization_id=ORG,
        contact_email=CONTACT,
        requirements=req,
        fields={"first_name": "Ada"},
        address=None,
        documents={},
    )
    fields = {p.field for p in result.problems}
    assert {
        "last_name",
        "email",
        "phone_number",
        "proof_of_identity",
        "address",
    } <= fields
    assert result.refs == {}
    assert len(responses.calls) == 0


@responses.activate
async def test_create_draft_tolerates_a_slot_with_no_options(provider) -> None:
    req = await _person_requirements(provider)
    empty = DocumentSlot(name="extra", label="Extra", options=())
    req = req.model_copy(update={"documents": (*req.documents, empty)})
    result = await provider.create_draft(
        organization_id=ORG,
        contact_email=CONTACT,
        requirements=req,
        fields={},
        address=None,
        documents={},
    )
    assert result.problems
    assert "extra" not in {p.field for p in result.problems}


@responses.activate
async def test_create_draft_rejects_invalid_email_and_wrong_file_slot(provider) -> None:
    req = await _person_requirements(provider)
    result = await provider.create_draft(
        organization_id=ORG,
        contact_email=CONTACT,
        requirements=req,
        fields={**PERSON_FIELDS, "email": "not-an-email"},
        address=ADDRESS,
        documents={"proof_of_identity": DocumentInput(option="passport")},  # no file
    )
    fields = {p.field for p in result.problems}
    assert fields == {"email", "proof_of_identity"}
    assert len(responses.calls) == 0


@responses.activate
async def test_create_draft_happy_path(provider) -> None:
    req = await _person_requirements(provider)
    _register_draft_calls(
        {"sid": "EL" + "1" * 32, "status": "compliant", "results": []}
    )

    result = await provider.create_draft(
        organization_id=ORG,
        contact_email=CONTACT,
        requirements=req,
        fields=PERSON_FIELDS,
        address=ADDRESS,
        documents=_passport(),
    )

    assert result.problems == []
    assert result.refs["bundle_sid"] == "BU" + "1" * 32
    assert result.refs["end_user_sid"] == "IT" + "1" * 32
    assert result.refs["address_sid"] == "AD" + "1" * 32
    assert len(result.refs["document_sids"]) == 2

    by_url = {}
    for call in responses.calls:
        by_url.setdefault(call.request.url, []).append(call.request)

    upload = by_url[UPLOAD][0]
    assert b"document.jpg" in upload.body  # fixed name, not the customer's file name
    assert b"ada-lovelace" not in upload.body.replace(b"Lovelace", b"")
    attrs = json.loads(_form_value(upload.body, b"Attributes"))
    assert attrs == {"first_name": "Ada", "last_name": "Lovelace"}
    assert _form_value(upload.body, b"Type") == b"passport"

    doc_bodies = [parse_qs(r.body) for r in by_url[f"{NUMBERS}/SupportingDocuments"]]
    assert doc_bodies[0]["Type"] == ["individual_address"]
    assert json.loads(doc_bodies[0]["Attributes"][0]) == {
        "address_sids": ["AD" + "1" * 32]
    }

    end_user = parse_qs(by_url[f"{NUMBERS}/EndUsers"][0].body)
    assert end_user["Type"] == ["individual"]
    # the customer's email is a field that describes them, so it stays here
    assert json.loads(end_user["Attributes"][0]) == PERSON_FIELDS

    bundle = parse_qs(by_url[f"{NUMBERS}/Bundles"][0].body)
    assert bundle["IsoCountry"] == ["GB"]
    assert bundle["NumberType"] == ["mobile"]
    assert bundle["EndUserType"] == ["individual"]
    assert bundle["FriendlyName"] == [f"hail-{ORG}"]
    # notices go to the operator's contact address, never to the customer
    assert bundle["Email"] == [CONTACT]
    assert "ada@example.com" not in json.dumps(bundle)
    assignments = by_url[f"{NUMBERS}/Bundles/BU{'1' * 32}/ItemAssignments"]
    assert len(assignments) == 3  # end user + two documents


def _form_value(body: bytes, name: bytes) -> bytes:
    marker = b'name="' + name + b'"\r\n\r\n'
    start = body.index(marker) + len(marker)
    return body[start : body.index(b"\r\n--", start)]


@responses.activate
async def test_create_draft_carrier_problems_are_returned_and_draft_is_deleted(
    provider,
) -> None:
    req = await _person_requirements(provider)
    _register_draft_calls(
        {
            "sid": "EL" + "1" * 32,
            "status": "noncompliant",
            "results": [
                {
                    "requirement_friendly_name": "Proof of Identity",
                    "passed": False,
                    "invalid": [
                        {
                            "friendly_name": "First Name",
                            "object_field": "first_name",
                            "failure_reason": "Name does not match.",
                        }
                    ],
                }
            ],
        }
    )
    for path in (
        f"Bundles/BU{'1' * 32}",
        f"SupportingDocuments/RD{'1' * 32}",
        f"SupportingDocuments/RD{'2' * 32}",
        f"EndUsers/IT{'1' * 32}",
    ):
        responses.add(responses.DELETE, f"{NUMBERS}/{path}", status=204)
    responses.add(
        responses.DELETE,
        f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Addresses/AD{'1' * 32}.json",
        status=204,
    )

    result = await provider.create_draft(
        organization_id=ORG,
        contact_email=CONTACT,
        requirements=req,
        fields=PERSON_FIELDS,
        address=ADDRESS,
        documents=_passport(),
    )

    assert result.refs == {}
    assert [(p.field, p.message) for p in result.problems] == [
        ("proof_of_identity.first_name", "Name does not match.")
    ]
    deletes = [c.request.url for c in responses.calls if c.request.method == "DELETE"]
    assert len(deletes) == 5  # bundle, two documents, end user, address


@responses.activate
async def test_create_draft_carrier_400_becomes_a_problem(provider) -> None:
    req = await _person_requirements(provider)
    responses.add(responses.POST, ADDRESSES, json={"sid": "AD" + "1" * 32}, status=201)
    responses.add(
        responses.POST,
        f"{NUMBERS}/EndUsers",
        json={"code": 20001, "message": "Invalid attributes", "status": 400},
        status=400,
    )
    responses.add(
        responses.DELETE,
        f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Addresses/AD{'1' * 32}.json",
        status=204,
    )
    result = await provider.create_draft(
        organization_id=ORG,
        contact_email=CONTACT,
        requirements=req,
        fields=PERSON_FIELDS,
        address=ADDRESS,
        documents=_passport(),
    )
    assert result.refs == {}
    assert len(result.problems) == 1 and result.problems[0].field == ""


# -- submit / status / handle / discard -----------------------------------

REFS = {
    "bundle_sid": "BU" + "1" * 32,
    "end_user_sid": "IT" + "1" * 32,
    "address_sid": "AD" + "1" * 32,
    "document_sids": ["RD" + "1" * 32],
}


@responses.activate
async def test_submit_sets_pending_review(provider) -> None:
    responses.add(
        responses.POST,
        f"{NUMBERS}/Bundles/{REFS['bundle_sid']}",
        json={"sid": REFS["bundle_sid"], "status": "pending-review"},
        status=200,
    )
    await provider.submit(REFS)
    assert parse_qs(responses.calls[0].request.body)["Status"] == ["pending-review"]


@responses.activate
@pytest.mark.parametrize(
    ("twilio_status", "state"),
    [
        ("draft", "pending"),
        ("pending-review", "pending"),
        ("in-review", "pending"),
        ("provisionally-approved", "pending"),
        ("twilio-approved", "approved"),
        ("twilio-rejected", "rejected"),
    ],
)
async def test_status_mapping(provider, twilio_status, state) -> None:
    responses.add(
        responses.GET,
        f"{NUMBERS}/Bundles/{REFS['bundle_sid']}",
        json={"sid": REFS["bundle_sid"], "status": twilio_status},
        status=200,
    )
    assert (await provider.status(REFS)).state == state


async def test_purchase_handle(provider) -> None:
    assert await provider.purchase_handle(REFS) == {
        "bundle_sid": REFS["bundle_sid"],
        "address_sid": REFS["address_sid"],
    }
    assert await provider.purchase_handle({"bundle_sid": "BUx"}) == {
        "bundle_sid": "BUx"
    }


@responses.activate
async def test_discard_never_raises(provider) -> None:
    # nothing is registered, so every call fails; discard must still return
    await provider.discard(REFS)


def test_registry_returns_plugin_when_configured(monkeypatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_account_sid", ACCOUNT_SID)
    monkeypatch.setattr(settings, "twilio_auth_token", "tok")
    assert isinstance(get_verification_provider("twilio"), TwilioVerificationProvider)
    assert get_verification_provider("nope") is None


def test_registry_none_without_credentials(monkeypatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_account_sid", "")
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    assert get_verification_provider("twilio") is None
