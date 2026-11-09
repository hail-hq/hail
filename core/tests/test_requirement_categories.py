"""Tests for the requirement classifier. Requirements are built through each
carrier plug-in's own mapping, from saved carrier answers."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from hailhq.core.providers.verification.base import (
    DocumentSlot,
    FieldSpec,
    Requirements,
)
from hailhq.core.providers.verification.telnyx import TelnyxVerificationProvider
from hailhq.core.providers.verification.twilio import _map_requirements
from hailhq.core.requirement_categories import (
    LABELS,
    classify,
    is_company_document,
    paperwork,
)

FIXTURES = Path(__file__).parent / "providers" / "fixtures"


def twilio_req(country: str, number_type: str, subject: str) -> Requirements:
    name = "individual" if subject == "person" else "business"
    data = json.loads(
        (
            FIXTURES / f"twilio-regulations-{country}-{number_type}-{name}.json"
        ).read_text()
    )
    fields, docs, address = _map_requirements(data["results"][0]["requirements"])
    return Requirements(
        provider="twilio",
        country_code=country,
        number_type=number_type,
        subject_type=subject,
        required=True,
        fields=fields,
        documents=docs,
        address_required=address,
    )


def _telnyx_rules(country: str, number_type: str) -> list[dict]:
    data = json.loads(
        (FIXTURES / f"telnyx-regulatory-{country}-{number_type}.json").read_text()
    )
    return data["data"][0]["regulatory_requirements"]


def req_from_rules(rules: list[dict], subject: str) -> Requirements:
    provider = TelnyxVerificationProvider(api_key="k")

    async def _rules(*_args):
        return rules

    provider._rules = _rules  # type: ignore[method-assign]
    return asyncio.run(provider.requirements("XX", "local", subject))  # type: ignore[arg-type]


def telnyx_req(country: str, number_type: str, subject: str) -> Requirements:
    return req_from_rules(_telnyx_rules(country, number_type), subject)


def _rule(name: str, field_type: str = "textual", id_: str = "x") -> dict:
    return {"id": id_, "name": name, "description": "", "field_type": field_type}


def test_twilio_gb_mobile_person():
    items = classify(twilio_req("GB", "mobile", "person"), "twilio")
    assert items[:1] == ["id_document"]
    assert items == ["id_document", "contact_details", "proof_of_address"]


def test_twilio_gb_mobile_business():
    items = classify(twilio_req("GB", "mobile", "business"), "twilio")
    assert "company_name" in items
    assert "company_id" in items
    assert "proof_of_address" in items


def test_twilio_de_business_documents():
    items = classify(twilio_req("DE", "local", "business"), "twilio")
    assert "company_registration" in items
    assert "proof_of_address" in items


def test_twilio_ch_person_and_business():
    person_fields = [
        FieldSpec(name=n, label=n)
        for n in ("first_name", "last_name", "birth_date", "nationality", "profession")
    ]
    person_docs = [
        DocumentSlot(name=n, label=n, options=())
        for n in (
            "birth_date_info",
            "name_info",
            "type_no_and_issuing_country_of_official_id_info",
        )
    ]
    req = Requirements(
        provider="twilio",
        country_code="CH",
        number_type="local",
        subject_type="person",
        required=True,
        fields=tuple(person_fields),
        documents=tuple(person_docs),
    )
    assert classify(req, "twilio") == [
        "id_document",
        "birth_date",
        "personal_details",
        "contact_details",
    ]
    biz = Requirements(
        provider="twilio",
        country_code="CH",
        number_type="local",
        subject_type="business",
        required=True,
        fields=(
            FieldSpec(name="business_name", label="Business name"),
            FieldSpec(name="enterprise_id_number", label="Enterprise id"),
        ),
        documents=(
            DocumentSlot(name="name_info", label="Name", options=()),
            DocumentSlot(
                name="registered_office_address_proof_info", label="Addr", options=()
            ),
        ),
    )
    assert classify(biz, "twilio") == [
        "proof_of_address",
        "company_registration",
        "company_id",
        "company_name",
    ]


def test_telnyx_ids_are_keys():
    # 2708e569-696a-4fc7-9305-5fdb3eb9c7dd is Contact Information in every country
    assert "contact_details" in classify(
        telnyx_req("SE", "local", "business"), "telnyx"
    )


def test_telnyx_se_local_business_full():
    items = classify(telnyx_req("SE", "local", "business"), "telnyx")
    assert items == [
        "id_document",
        "contact_details",
        "proof_of_address",
        "company_registration",
        "company_id",
        "company_name",
        "local_usage",
    ]


def test_telnyx_address_rule_by_label():
    rules = _telnyx_rules("SE", "local")
    req = req_from_rules(rules, "business")
    assert "address_in_area" in classify(req, "telnyx", rules=rules)
    assert "address_in_country" not in classify(req, "telnyx", rules=rules)
    national = [
        _rule(
            "Physical Address Within the Country of the Phone Number (National)",
            "address",
        )
    ]
    items = classify(req_from_rules(national, "person"), "telnyx", rules=national)
    assert items == ["address_in_country"]


def test_label_examples():
    cases = [
        ("Contact Email Address of End Client", "textual", ["contact_details"]),
        ("Company Name (Full Legal Name)", "textual", ["company_name"]),
        ("Company Registration Number — Switzerland", "textual", ["company_id"]),
        ("Proof of Address (Recent Bill or Invoice)", "document", ["proof_of_address"]),
        (
            "Local Company Registration Certificate — Switzerland",
            "document",
            ["company_registration"],
        ),
        (
            "Passport or Government-Issued ID Copy (End User or Authorized Representative)",
            "document",
            ["id_document"],
        ),
        ("Portuguese NIF (Número de Identificação Fiscal)", "textual", ["company_id"]),
    ]
    for name, ftype, expected in cases:
        rule = _rule(name, ftype, id_="unknown-id")
        assert classify(req_from_rules([rule], "business"), "telnyx") == expected, name


def test_person_tax_number_is_personal_details():
    rule = _rule("Portuguese NIF (Número de Identificação Fiscal)")
    assert classify(req_from_rules([rule], "person"), "telnyx") == ["personal_details"]


def test_either_or_splits_by_subject():
    rule = _rule(
        "Local Government-Issued ID/Passport Copy or Local Company Registration Certificate",
        "document",
    )
    assert classify(req_from_rules([rule], "person"), "telnyx") == ["id_document"]
    assert classify(req_from_rules([rule], "business"), "telnyx") == [
        "company_registration"
    ]
    assert is_company_document(rule) is False


def test_spanish_tax_id_either_or():
    rule = _rule(
        "Spanish Tax ID: CIF (Business), DNI (Spanish Citizen), or NIE (Foreigner)"
    )
    assert classify(req_from_rules([rule], "person"), "telnyx") == ["personal_details"]
    assert classify(req_from_rules([rule], "business"), "telnyx") == ["company_id"]


def test_company_certificate_document():
    rule = _rule("Local Company Registration Certificate — Switzerland", "document")
    assert is_company_document(rule) is True


def test_id_document_is_not_company_document():
    rule = _rule(
        "Passport or Government-Issued ID Copy (End User or Authorized Representative)",
        "document",
    )
    assert is_company_document(rule) is False


def test_text_rule_is_never_company_document():
    assert (
        is_company_document(_rule("Company Registration Number — Switzerland")) is False
    )


def test_unknown_is_other_and_logged(caplog):
    caplog.set_level(logging.WARNING)
    items = classify(req_from_rules([_rule("Blood type")], "person"), "telnyx")
    assert items == [{"other": "Blood type"}]
    assert "Blood type" in caplog.text


def test_dedup_and_order():
    rules = [
        _rule("Proof of Address (Recent Bill or Invoice)", "document", "a"),
        _rule("Proof of Address (National), Recent Utility Bill", "document", "b"),
        _rule("Contact Information (Name, Business Name, Phone Number)", id_="c"),
        _rule("Passport or ID Copy in PDF Format", "document", "d"),
    ]
    items = classify(req_from_rules(rules, "person"), "telnyx")
    assert items == ["id_document", "contact_details", "proof_of_address"]


def test_didww_fields_and_proofs():
    req = Requirements(
        provider="didww",
        country_code="PT",
        number_type="national",
        subject_type="business",
        required=True,
        fields=(
            FieldSpec(name="company_name", label="Company name"),
            FieldSpec(name="company_reg_number", label="Reg"),
            FieldSpec(name="vat_id", label="VAT"),
            FieldSpec(name="phone_number", label="Phone"),
        ),
        documents=(
            DocumentSlot(
                name="identity_proof_1", label="Proof of identity", options=()
            ),
            DocumentSlot(name="address_proof_1", label="Proof of address", options=()),
        ),
        address_required=True,
    )
    assert classify(req, "didww") == [
        "contact_details",
        "proof_of_address",
        "company_registration",
        "company_id",
        "company_name",
    ]


def test_address_required_fallback():
    req = Requirements(
        provider="twilio",
        country_code="GB",
        number_type="local",
        subject_type="person",
        required=True,
        address_required=True,
    )
    assert classify(req, "twilio") == ["address_in_country"]


def test_paperwork_labels():
    assert paperwork(["id_document", {"other": "Blood type"}]) == [
        {"key": "id_document", "label": "ID card or passport"},
        {"key": "other", "label": "Blood type"},
    ]
    assert LABELS["company_registration"] == "Company register extract or certificate"


def test_every_known_didww_field_maps_to_a_category_or_nothing():
    from hailhq.core.providers.verification import VerificationRequirements
    from hailhq.core.providers.verification.didww import _LABELS, _field
    from hailhq.core.requirement_categories import classify

    # Names DIDWW sends in {personal,business}_mandatory_fields (read from its
    # API) plus every name the plug-in labels.
    names = set(_LABELS) | {
        "birth_country",
        "birth_date",
        "company_reg_number",
        "contact_email",
        "country",
        "id_number",
        "personal_tax_id",
        "vat_id",
        "description",
    }
    for name in sorted(names):
        for subject in ("person", "business"):
            req = VerificationRequirements(
                country_code="DE",
                number_type="local",
                subject_type=subject,
                required=True,
                fields=(_field(name),),
            )
            items = classify(req, "didww")
            assert not any(isinstance(i, dict) for i in items), (name, items)


def test_address_scope_decides_where_the_address_may_be():
    base = {
        "provider": "didww",
        "country_code": "GB",
        "number_type": "local",
        "subject_type": "person",
        "required": True,
        "documents": (
            DocumentSlot(name="address_proof_1", label="Proof of address", options=()),
        ),
        "address_required": True,
    }
    assert classify(Requirements(**base, address_scope="any"), "didww") == [
        "proof_of_address",
        "address_any_country",
    ]
    # Another place keeps the list as it was.
    assert classify(Requirements(**base, address_scope="area"), "didww") == [
        "proof_of_address"
    ]
    # Unknown: the old rule (a proof of address, nothing more is promised).
    assert classify(Requirements(**base), "didww") == ["proof_of_address"]
    assert LABELS["address_any_country"] == "An address in any country"


def test_address_scope_replaces_a_guess_from_a_label():
    req = Requirements(
        provider="twilio",
        country_code="GB",
        number_type="local",
        subject_type="person",
        required=True,
        address_required=True,
        address_scope="any",
    )
    # Without a scope this would be address_in_country (the fallback).
    assert classify(req, "twilio") == ["address_any_country"]


def test_address_scope_never_changes_the_stored_version_hash():
    base = {
        "provider": "twilio",
        "country_code": "GB",
        "number_type": "local",
        "subject_type": "person",
        "required": True,
        "address_required": True,
    }
    # Where the address may be is not part of the form: no hash moves.
    plain = Requirements(**base).version
    assert Requirements(**base, address_scope="any").version == plain
