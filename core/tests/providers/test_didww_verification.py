"""``DidwwVerificationProvider`` against DIDWW's JSON:API (``responses``)."""

from __future__ import annotations

import json

import pytest
import responses
from hailhq.core.config import settings
from hailhq.core.providers.verification import (
    Address,
    DocumentInput,
    UnsupportedSubjectType,
    UploadedFile,
    get_verification_provider,
)
from hailhq.core.providers.verification.didww import DidwwVerificationProvider
from hailhq.core.providers.voice import didww as voice_mod

BASE = "https://sandbox-api.didww.com/v3"
ORG = "11111111-2222-3333-4444-555555555555"
COUNTRY_ID = "c0000000-0000-0000-0000-000000000001"
NATIONAL_ID = "t0000000-0000-0000-0000-000000000002"
REQ_ID = "r0000000-0000-0000-0000-000000000005"
PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAu1SU1LfVLPHCozMxH2Mo\n"
    "4lgOEePzNm0tRgeLezV6ffAt0gunVTLw7onLRnrq0/IzW7yWR7QkrmBL7jTKEn5u\n"
    "+qKhbwKfBstIs+bMY2Zkp18gnTxKLxoS2tFczGkPLPgizskuemMghRniWaoLcyeh\n"
    "kd3qqGElvW/VDL5AaWTg0nLVkjRo9z+40RQzuVaE8AkAFmxZzow3x+VJYKdjykkJ\n"
    "0iT9wCS0DRTXu269V264Vf/3jvredZiKRkgwlL9xNAwxXFg0x/XFw005UWVRIkdg\n"
    "cKWTjpBP2dPwVZ4WWC+9aGVd+Gyn1o0CLelf4rEjGoXbAAEgAqeGUxrcIlbjXfbc\n"
    "mwIDAQAB\n"
    "-----END PUBLIC KEY-----"
)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "didww_api_key", "test-key")
    monkeypatch.setattr(settings, "didww_environment", "sandbox")
    voice_mod.lookup_ids.cache_clear()


@pytest.fixture()
def provider() -> DidwwVerificationProvider:
    return DidwwVerificationProvider()


def _static():
    responses.add(
        responses.GET,
        f"{BASE}/countries",
        json={
            "data": [
                {"id": COUNTRY_ID, "type": "countries", "attributes": {"iso": "PT"}}
            ]
        },
    )
    responses.add(
        responses.GET,
        f"{BASE}/did_group_types",
        json={
            "data": [
                {
                    "id": NATIONAL_ID,
                    "type": "did_group_types",
                    "attributes": {"name": "National"},
                }
            ]
        },
    )


def _requirement(
    *,
    identity_type="any",
    personal_qty=1,
    address_qty=1,
    service_description=False,
    area="country",
):
    responses.add(
        responses.GET,
        f"{BASE}/address_requirements",
        json={
            "data": [
                {
                    "id": REQ_ID,
                    "type": "address_requirements",
                    "attributes": {
                        "identity_type": identity_type,
                        "personal_area_level": "world_wide",
                        "business_area_level": "world_wide",
                        "address_area_level": area,
                        "personal_proof_qty": personal_qty,
                        "business_proof_qty": 1,
                        "address_proof_qty": address_qty,
                        "personal_mandatory_fields": ["birth_date", "id_number"],
                        "business_mandatory_fields": ["company_reg_number"],
                        "service_description_required": service_description,
                        "restriction_message": "Local presence required.",
                    },
                    "relationships": {
                        "personal_proof_types": {
                            "data": [{"id": "pt-passport", "type": "proof_types"}]
                        },
                        "business_proof_types": {
                            "data": [{"id": "pt-reg", "type": "proof_types"}]
                        },
                        "address_proof_types": {
                            "data": [{"id": "pt-bill", "type": "proof_types"}]
                        },
                    },
                }
            ],
            "included": [
                {
                    "id": "pt-passport",
                    "type": "proof_types",
                    "attributes": {"name": "Passport", "entity_type": "Personal"},
                },
                {
                    "id": "pt-reg",
                    "type": "proof_types",
                    "attributes": {
                        "name": "Company registration",
                        "entity_type": "Business",
                    },
                },
                {
                    "id": "pt-bill",
                    "type": "proof_types",
                    "attributes": {"name": "Utility bill", "entity_type": "Address"},
                },
            ],
        },
    )


@responses.activate
async def test_requirements_person(provider):
    _static()
    _requirement(service_description=True)
    req = await provider.requirements("PT", "national", "person")
    assert req.required is True and req.provider == "didww"
    assert [f.name for f in req.fields] == [
        "first_name",
        "last_name",
        "birth_date",
        "id_number",
        "service_description",
    ]
    assert [s.name for s in req.documents] == ["identity_proof_1", "address_proof_1"]
    assert req.documents[0].options[0].key == "pt-passport"
    assert req.documents[0].options[0].label == "Passport"
    assert req.documents[1].options[0].needs_address is True
    assert req.address_required is True
    assert req.subject_types == ("business", "person")
    url = responses.calls[2].request.url
    assert (
        f"filter%5Bcountry.id%5D={COUNTRY_ID}" in url
        and f"filter%5Bdid_group_type.id%5D={NATIONAL_ID}" in url
    )


@responses.activate
async def test_requirements_business(provider):
    _static()
    _requirement()
    req = await provider.requirements("PT", "national", "business")
    assert [f.name for f in req.fields] == ["company_name", "company_reg_number"]
    assert req.documents[0].options[0].key == "pt-reg"


@responses.activate
async def test_requirements_rejects_disallowed_subject(provider):
    _static()
    _requirement(identity_type="business")
    with pytest.raises(UnsupportedSubjectType) as exc:
        await provider.requirements("PT", "national", "person")
    assert exc.value.allowed == ("business",)


@responses.activate
async def test_requirements_none_needed(provider):
    _static()
    responses.add(
        responses.GET, f"{BASE}/address_requirements", json={"data": [], "included": []}
    )
    req = await provider.requirements("PT", "national", "person")
    assert req.required is False and req.fields == () and req.documents == ()


def _draft_endpoints(*, validation_status=201, validation_body=None):
    responses.add(responses.GET, f"{BASE}/identities", json={"data": []})
    responses.add(
        responses.POST,
        f"{BASE}/identities",
        status=201,
        json={"data": {"id": "id-1", "type": "identities"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/addresses",
        status=201,
        json={"data": {"id": "addr-1", "type": "addresses"}},
    )
    responses.add(
        responses.GET,
        f"{BASE}/public_keys",
        json={
            "data": [
                {"id": "k1", "type": "public_keys", "attributes": {"key": PEM}},
                {"id": "k2", "type": "public_keys", "attributes": {"key": PEM}},
            ]
        },
    )
    responses.add(
        responses.POST,
        f"{BASE}/encrypted_files",
        status=201,
        json={"data": {"id": "file-1", "type": "encrypted_files"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/encrypted_files",
        status=201,
        json={"data": {"id": "file-2", "type": "encrypted_files"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/proofs",
        status=201,
        json={"data": {"id": "proof-1", "type": "proofs"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/proofs",
        status=201,
        json={"data": {"id": "proof-2", "type": "proofs"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/address_requirement_validations",
        status=validation_status,
        json=validation_body
        or {"data": {"id": "val-1", "type": "address_requirement_validations"}},
    )


def _inputs():
    fields = {
        "first_name": "Ana",
        "last_name": "Silva",
        "birth_date": "1990-01-02",
        "id_number": "12345678",
        "service_description": "Support line",
    }
    address = Address(
        customer_name="Ana Silva",
        street="Rua A 1",
        city="Lisboa",
        region="Lisboa",
        postal_code="1000-001",
        country_code="PT",
    )
    pdf = UploadedFile(
        filename="passport.pdf", content_type="application/pdf", data=b"%PDF-1.4 test"
    )
    documents = {
        "identity_proof_1": DocumentInput(option="pt-passport", file=pdf),
        "address_proof_1": DocumentInput(option="pt-bill", file=pdf),
    }
    return fields, address, documents


@responses.activate
async def test_create_draft_happy_path(provider):
    _static()
    _requirement(service_description=True)
    req = await provider.requirements("PT", "national", "person")
    _draft_endpoints()
    fields, address, documents = _inputs()
    result = await provider.create_draft(
        organization_id=ORG,
        contact_email="ops@hail.test",
        requirements=req,
        fields=fields,
        address=address,
        documents=documents,
    )
    assert result.problems == []
    assert result.refs == {
        "organization_id": ORG,
        "identity_id": "id-1",
        "identity_created": True,
        "address_id": "addr-1",
        "proof_ids": ["proof-1", "proof-2"],
        "file_ids": ["file-1", "file-2"],
        "requirement_id": REQ_ID,
        "country_code": "PT",
        "number_type": "national",
    }
    bodies = {
        c.request.url.split("/v3/")[1].split("?")[0]: c.request for c in responses.calls
    }
    identity = json.loads(bodies["identities"].body)["data"]
    assert identity["attributes"]["identity_type"] == "personal"
    assert identity["attributes"]["first_name"] == "Ana"
    assert identity["attributes"]["external_reference_id"] == f"hail-{ORG}"
    assert identity["attributes"]["contact_email"] == "ops@hail.test"
    assert identity["relationships"]["country"]["data"] == {
        "id": COUNTRY_ID,
        "type": "countries",
    }
    addr = json.loads(bodies["addresses"].body)["data"]
    assert addr["attributes"] == {
        "address": "Rua A 1",
        "city_name": "Lisboa",
        "postal_code": "1000-001",
        "description": "Support line",
        "external_reference_id": f"hail-draft:{ORG}:PT:national",
    }
    assert addr["relationships"]["identity"]["data"] == {
        "id": "id-1",
        "type": "identities",
    }
    upload = next(
        c.request for c in responses.calls if c.request.url.endswith("/encrypted_files")
    )
    assert b"document.pdf" in upload.body and b"%PDF-1.4 test" not in upload.body
    proofs = [
        json.loads(c.request.body)["data"]
        for c in responses.calls
        if c.request.url.endswith("/proofs")
    ]
    assert proofs[0]["relationships"]["entity"]["data"] == {
        "id": "id-1",
        "type": "identities",
    }
    assert proofs[0]["relationships"]["proof_type"]["data"] == {
        "id": "pt-passport",
        "type": "proof_types",
    }
    assert proofs[1]["relationships"]["entity"]["data"] == {
        "id": "addr-1",
        "type": "addresses",
    }
    validation = json.loads(bodies["address_requirement_validations"].body)["data"][
        "relationships"
    ]
    assert validation["address_requirement"]["data"]["id"] == REQ_ID


@responses.activate
async def test_create_draft_validation_failure_discards_everything(provider):
    _static()
    _requirement()
    req = await provider.requirements("PT", "national", "person")
    _draft_endpoints(
        validation_status=422,
        validation_body={
            "errors": [
                {
                    "title": "Address in Portugal required",
                    "detail": "Address in Portugal required",
                    "source": {"pointer": "/data"},
                }
            ]
        },
    )
    for path in (
        "proofs/proof-1",
        "proofs/proof-2",
        "encrypted_files/file-1",
        "encrypted_files/file-2",
        "addresses/addr-1",
        "identities/id-1",
    ):
        responses.add(responses.DELETE, f"{BASE}/{path}", status=204)
    fields, address, documents = _inputs()
    result = await provider.create_draft(
        organization_id=ORG,
        contact_email="ops@hail.test",
        requirements=req,
        fields=fields,
        address=address,
        documents=documents,
    )
    assert result.refs == {}
    assert [p.message for p in result.problems] == ["Address in Portugal required"]
    deleted = [
        c.request.url.split("/v3/")[1]
        for c in responses.calls
        if c.request.method == "DELETE"
    ]
    assert deleted == [
        "proofs/proof-1",
        "proofs/proof-2",
        "encrypted_files/file-1",
        "encrypted_files/file-2",
        "addresses/addr-1",
        "identities/id-1",
    ]


@responses.activate
async def test_create_draft_reuses_existing_identity(provider):
    _static()
    _requirement()
    req = await provider.requirements("PT", "national", "person")
    responses.add(
        responses.GET,
        f"{BASE}/identities",
        json={
            "data": [
                {
                    "id": "id-old",
                    "type": "identities",
                    "attributes": {
                        "external_reference_id": f"hail-{ORG}",
                        "identity_type": "personal",
                        "first_name": "Ana",
                        "last_name": "Silva",
                        "birth_date": "1990-01-02",
                        "id_number": "12345678",
                    },
                    "relationships": {
                        "country": {"data": {"id": COUNTRY_ID, "type": "countries"}}
                    },
                }
            ]
        },
    )
    responses.add(
        responses.POST,
        f"{BASE}/addresses",
        status=201,
        json={"data": {"id": "addr-1", "type": "addresses"}},
    )
    responses.add(
        responses.GET,
        f"{BASE}/public_keys",
        json={
            "data": [
                {"id": "k1", "type": "public_keys", "attributes": {"key": PEM}},
                {"id": "k2", "type": "public_keys", "attributes": {"key": PEM}},
            ]
        },
    )
    responses.add(
        responses.POST,
        f"{BASE}/encrypted_files",
        status=201,
        json={"data": {"id": "file-1", "type": "encrypted_files"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/encrypted_files",
        status=201,
        json={"data": {"id": "file-2", "type": "encrypted_files"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/proofs",
        status=201,
        json={"data": {"id": "proof-1", "type": "proofs"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/proofs",
        status=201,
        json={"data": {"id": "proof-2", "type": "proofs"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/address_requirement_validations",
        status=201,
        json={"data": {"id": "val-1", "type": "address_requirement_validations"}},
    )
    fields, address, documents = _inputs()
    result = await provider.create_draft(
        organization_id=ORG,
        contact_email="ops@hail.test",
        requirements=req,
        fields=fields,
        address=address,
        documents=documents,
    )
    assert (
        result.refs["identity_id"] == "id-old"
        and result.refs["identity_created"] is False
    )
    assert not any(
        c.request.method == "POST" and c.request.url.endswith("/identities")
        for c in responses.calls
    )


@responses.activate
async def test_create_draft_creates_new_identity_when_details_differ(provider):
    _static()
    _requirement()
    req = await provider.requirements("PT", "national", "person")
    responses.add(
        responses.GET,
        f"{BASE}/identities",
        json={
            "data": [
                {
                    "id": "id-old",
                    "type": "identities",
                    "attributes": {
                        "external_reference_id": f"hail-{ORG}",
                        "identity_type": "personal",
                        "first_name": "Bruno",
                        "last_name": "Silva",
                        "birth_date": "1990-01-02",
                        "id_number": "12345678",
                    },
                    "relationships": {
                        "country": {"data": {"id": COUNTRY_ID, "type": "countries"}}
                    },
                }
            ]
        },
    )
    responses.add(
        responses.POST,
        f"{BASE}/identities",
        status=201,
        json={"data": {"id": "id-new", "type": "identities"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/addresses",
        status=201,
        json={"data": {"id": "addr-1", "type": "addresses"}},
    )
    responses.add(
        responses.GET,
        f"{BASE}/public_keys",
        json={
            "data": [
                {"id": "k1", "type": "public_keys", "attributes": {"key": PEM}},
                {"id": "k2", "type": "public_keys", "attributes": {"key": PEM}},
            ]
        },
    )
    responses.add(
        responses.POST,
        f"{BASE}/encrypted_files",
        status=201,
        json={"data": {"id": "file-1", "type": "encrypted_files"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/encrypted_files",
        status=201,
        json={"data": {"id": "file-2", "type": "encrypted_files"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/proofs",
        status=201,
        json={"data": {"id": "proof-1", "type": "proofs"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/proofs",
        status=201,
        json={"data": {"id": "proof-2", "type": "proofs"}},
    )
    responses.add(
        responses.POST,
        f"{BASE}/address_requirement_validations",
        status=201,
        json={"data": {"id": "val-1", "type": "address_requirement_validations"}},
    )
    fields, address, documents = _inputs()  # first_name: "Ana", not "Bruno"
    result = await provider.create_draft(
        organization_id=ORG,
        contact_email="ops@hail.test",
        requirements=req,
        fields=fields,
        address=address,
        documents=documents,
    )
    assert (
        result.refs["identity_id"] == "id-new"
        and result.refs["identity_created"] is True
    )
    assert any(
        c.request.method == "POST" and c.request.url.endswith("/identities")
        for c in responses.calls
    )


@responses.activate
async def test_create_draft_rejects_unknown_document_option_before_any_write(provider):
    _static()
    _requirement()
    req = await provider.requirements("PT", "national", "person")
    fields, address, documents = _inputs()
    documents["identity_proof_1"] = DocumentInput(
        option="not-a-real-option", file=documents["identity_proof_1"].file
    )
    calls_before = len(responses.calls)
    result = await provider.create_draft(
        organization_id=ORG,
        contact_email="ops@hail.test",
        requirements=req,
        fields=fields,
        address=address,
        documents=documents,
    )
    assert result.refs == {}
    assert [p.message for p in result.problems] == [
        "Pick one of the offered document types."
    ]
    assert len(responses.calls) == calls_before


@responses.activate
async def test_submit_stamps_approved_reference(provider):
    responses.add(
        responses.PATCH,
        f"{BASE}/addresses/addr-1",
        json={"data": {"id": "addr-1", "type": "addresses"}},
    )
    await provider.submit(
        {
            "address_id": "addr-1",
            "organization_id": ORG,
            "country_code": "PT",
            "number_type": "national",
        }
    )
    sent = json.loads(responses.calls[0].request.body)["data"]
    assert sent == {
        "id": "addr-1",
        "type": "addresses",
        "attributes": {"external_reference_id": f"hail:{ORG}:PT:national"},
    }


@responses.activate
@pytest.mark.parametrize(
    ("ref", "state"),
    [
        (f"hail:{ORG}:PT:national", "approved"),
        (f"hail-draft:{ORG}:PT:national", "draft"),
    ],
)
async def test_status_reads_reference(provider, ref, state):
    responses.add(
        responses.GET,
        f"{BASE}/addresses/addr-1",
        json={
            "data": {
                "id": "addr-1",
                "type": "addresses",
                "attributes": {"external_reference_id": ref},
            }
        },
    )
    assert (await provider.status({"address_id": "addr-1"})).state == state


async def test_purchase_handle(provider):
    assert await provider.purchase_handle(
        {"identity_id": "id-1", "address_id": "addr-1", "proof_ids": []}
    ) == {"identity_id": "id-1", "address_id": "addr-1"}


@responses.activate
async def test_check_reruns_validation(provider):
    responses.add(
        responses.POST,
        f"{BASE}/address_requirement_validations",
        status=422,
        json={
            "errors": [
                {
                    "title": "Identity id number missing",
                    "detail": "Identity id number missing",
                }
            ]
        },
    )
    problems = await provider.check(
        {"identity_id": "id-1", "address_id": "addr-1", "requirement_id": REQ_ID}
    )
    assert [p.message for p in problems] == ["Identity id number missing"]


@responses.activate
async def test_discard_keeps_reused_identity(provider):
    for path in ("proofs/proof-1", "encrypted_files/file-1", "addresses/addr-1"):
        responses.add(responses.DELETE, f"{BASE}/{path}", status=204)
    await provider.discard(
        {
            "identity_id": "id-old",
            "identity_created": False,
            "address_id": "addr-1",
            "proof_ids": ["proof-1"],
            "file_ids": ["file-1"],
        }
    )
    deleted = [
        c.request.url.split("/v3/")[1]
        for c in responses.calls
        if c.request.method == "DELETE"
    ]
    assert deleted == ["proofs/proof-1", "encrypted_files/file-1", "addresses/addr-1"]


def test_registered_when_configured():
    assert isinstance(get_verification_provider("didww"), DidwwVerificationProvider)


def test_not_registered_without_key(monkeypatch):
    from hailhq.core.providers.verification import _INSTANCES

    monkeypatch.setattr(settings, "didww_api_key", "")
    _INSTANCES.pop("didww", None)
    assert get_verification_provider("didww") is None


def test_not_registered_with_bad_environment(monkeypatch):
    from hailhq.core.providers.verification import _INSTANCES

    monkeypatch.setattr(settings, "didww_api_key", "test-key")
    monkeypatch.setattr(settings, "didww_environment", "staging")
    _INSTANCES.pop("didww", None)
    assert get_verification_provider("didww") is None
