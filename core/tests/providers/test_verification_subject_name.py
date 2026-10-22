"""Each verification plug-in names the person or company a verification is
for: from the form (``subject_name``) and, for the operator backfill, from
the carrier's record (``fetch_subject_name``). Carriers are faked."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from hailhq.core.carrier_offer import CarrierOffer
from hailhq.core.providers.verification import (
    Address,
    DocumentInput,
    FieldSpec,
    Requirements,
)
from hailhq.core.providers.verification.didww import DidwwVerificationProvider
from hailhq.core.providers.verification.telnyx import TelnyxVerificationProvider
from hailhq.core.providers.verification.telnyx_sms import (
    TelnyxSmsRegistrationProvider,
)
from hailhq.core.providers.verification.twilio import TwilioVerificationProvider
from hailhq.core.providers.verification.twilio_sms import (
    TwilioSmsRegistrationProvider,
)


def _bare(cls):
    """A plug-in without credentials: ``subject_name`` reads no state."""
    return cls.__new__(cls)


def _req(kind: str, fields: tuple[FieldSpec, ...] = ()) -> Requirements:
    return Requirements(
        provider="x",
        country_code="GB",
        number_type="mobile",
        subject_type=kind,  # type: ignore[arg-type]
        required=True,
        fields=fields,
    )


def _address(name: str = "Address Name") -> Address:
    return Address(
        customer_name=name,
        street="1 Main St",
        city="London",
        region="London",
        postal_code="N1 1AA",
        country_code="GB",
    )


# -- from the form ----------------------------------------------------------


def test_twilio_name_from_the_form() -> None:
    p = _bare(TwilioVerificationProvider)
    assert (
        p.subject_name(
            _req("business"),
            {"business_name": " Acme Ltd ", "first_name": "Ada"},
            _address(),
            {},
        )
        == "Acme Ltd"
    )
    assert (
        p.subject_name(
            _req("person"), {"first_name": "Ada", "last_name": "Lovelace"}, None, {}
        )
        == "Ada Lovelace"
    )
    # A name copied onto a document, then the address.
    doc = DocumentInput(option="passport", fields={"first_name": "Grace"})
    assert p.subject_name(_req("person"), {}, None, {"id": doc}) == "Grace"
    assert p.subject_name(_req("person"), {}, _address("Ada L"), {}) == "Ada L"
    assert p.subject_name(_req("business"), {}, None, {}) is None


def test_telnyx_name_from_the_address_first() -> None:
    p = _bare(TelnyxVerificationProvider)
    company = FieldSpec(name="req-1", label="Company Name (Full Legal Name)")
    contact = FieldSpec(name="req-2", label="Contact Information (Name, Phone)")
    fields = {"req-1": "Acme AB", "req-2": "Ada, +46 70 000"}
    assert (
        p.subject_name(_req("business", (company, contact)), fields, _address(), {})
        == "Address Name"
    )
    assert (
        p.subject_name(_req("business", (company, contact)), fields, None, {})
        == "Acme AB"
    )
    # A free-text contact field is never used.
    assert p.subject_name(_req("person", (company, contact)), fields, None, {}) is None
    assert p.subject_name(_req("person"), {}, _address("Ada L"), {}) == "Ada L"


def test_didww_name_from_the_identity_fields() -> None:
    p = _bare(DidwwVerificationProvider)
    assert (
        p.subject_name(_req("business"), {"company_name": "Acme SA"}, _address(), {})
        == "Acme SA"
    )
    assert (
        p.subject_name(
            _req("person"), {"first_name": "Ada", "last_name": "Lovelace"}, None, {}
        )
        == "Ada Lovelace"
    )
    assert p.subject_name(_req("business"), {}, _address("Acme"), {}) == "Acme"


@pytest.mark.parametrize(
    "cls", [TwilioSmsRegistrationProvider, TelnyxSmsRegistrationProvider]
)
def test_sms_name_from_the_form(cls) -> None:
    p = _bare(cls)
    assert (
        p.subject_name(
            _req("business"), {"legal_name": "Acme Inc", "rep_name": "Ada"}, None, {}
        )
        == "Acme Inc"
    )
    assert (
        p.subject_name(
            _req("person"), {"first_name": "Ada", "last_name": "Lovelace"}, None, {}
        )
        == "Ada Lovelace"
    )
    assert p.subject_name(_req("person"), {}, _address("Ada L"), {}) == "Ada L"


def test_a_long_name_is_cut() -> None:
    p = _bare(DidwwVerificationProvider)
    name = p.subject_name(_req("business"), {"company_name": "A" * 500}, None, {})
    assert name == "A" * 200


# -- from the carrier (backfill) ---------------------------------------------


async def test_twilio_fetch_reads_the_end_user_then_the_address() -> None:
    p = _bare(TwilioVerificationProvider)
    p._client = MagicMock()
    rc = p._client.numbers.v2.regulatory_compliance
    rc.end_users.return_value.fetch.return_value = SimpleNamespace(
        attributes={"business_name": "Acme Ltd"}
    )
    assert await p.fetch_subject_name({"end_user_sid": "IT1"}, "business") == "Acme Ltd"
    rc.end_users.assert_called_with("IT1")
    rc.end_users.return_value.fetch.return_value = SimpleNamespace(attributes={})
    p._client.addresses.return_value.fetch.return_value = SimpleNamespace(
        customer_name="Ada Lovelace"
    )
    assert (
        await p.fetch_subject_name(
            {"end_user_sid": "IT1", "address_sid": "AD1"}, "person"
        )
        == "Ada Lovelace"
    )
    assert await p.fetch_subject_name({}, "person") is None


async def test_telnyx_fetch_reads_the_address() -> None:
    p = _bare(TelnyxVerificationProvider)
    p._api = SimpleNamespace(
        request=AsyncMock(
            return_value={
                "data": {"first_name": "Ada", "last_name": "Lovelace"},
            }
        )
    )
    assert await p.fetch_subject_name({"address_id": "a1"}, "person") == (
        "Ada Lovelace"
    )
    p._api.request.assert_awaited_with("GET", "/addresses/a1")
    p._api.request.return_value = {"data": {"business_name": "Acme AB"}}
    assert await p.fetch_subject_name({"address_id": "a1"}, "business") == "Acme AB"
    assert await p.fetch_subject_name({"group_id": "g"}, "business") is None


async def test_didww_fetch_reads_the_identity() -> None:
    p = _bare(DidwwVerificationProvider)
    p._client = MagicMock()
    p._client.get.return_value = {
        "data": {"attributes": {"company_name": "Acme SA", "first_name": "Ada"}}
    }
    assert await p.fetch_subject_name({"identity_id": "i1"}, "business") == "Acme SA"
    p._client.get.assert_called_with("identities/i1")
    p._client.get.return_value = {
        "data": {"attributes": {"first_name": "Ada", "last_name": "Lovelace"}}
    }
    assert await p.fetch_subject_name({"identity_id": "i1"}, "person") == (
        "Ada Lovelace"
    )


async def test_twilio_sms_fetch_reads_the_profile_end_user() -> None:
    p = _bare(TwilioSmsRegistrationProvider)
    p._client = MagicMock()
    th = p._client.trusthub.v1
    th.customer_profiles.return_value.customer_profiles_entity_assignments.list.return_value = [
        SimpleNamespace(object_sid="RD1"),
        SimpleNamespace(object_sid="IT_rep"),
        SimpleNamespace(object_sid="IT_biz"),
    ]
    users = {
        "IT_rep": SimpleNamespace(
            type="authorized_representative_1", attributes={"first_name": "Ada"}
        ),
        "IT_biz": SimpleNamespace(
            type="customer_profile_business_information",
            attributes={"business_name": "Acme Inc"},
        ),
    }
    th.end_users.side_effect = lambda sid: SimpleNamespace(fetch=lambda: users[sid])
    assert await p.fetch_subject_name({"profile_sid": "BU1"}, "business") == (
        "Acme Inc"
    )
    assert await p.fetch_subject_name({}, "business") is None


async def test_telnyx_sms_fetch_reads_the_brand() -> None:
    p = _bare(TelnyxSmsRegistrationProvider)
    p._api = SimpleNamespace(
        request=AsyncMock(
            return_value={
                "firstName": "Ada",
                "lastName": "Lovelace",
                "companyName": "x",
            }
        )
    )
    assert await p.fetch_subject_name({"brand_id": "B1"}, "person") == "Ada Lovelace"
    p._api.request.return_value = {"companyName": "Acme Inc", "displayName": "Acme"}
    assert await p.fetch_subject_name({"brand_id": "B1"}, "business") == "Acme Inc"


# -- stored offers ----------------------------------------------------------


def test_an_offer_stored_with_approvals_per_kind_still_reads() -> None:
    """Offers stored before approvals became a list held one per kind."""
    from uuid import uuid4

    row = uuid4()
    offer = CarrierOffer.model_validate(
        {
            "provider": "twilio",
            "e164": "+447700900001",
            "country_code": "GB",
            "number_type": "mobile",
            "capabilities": ["voice"],
            "monthly_cents": 100,
            "setup_cents": 0,
            "readiness": "ready",
            "approvals": {"person": {"row_id": str(row), "verification_id": "BU1"}},
        }
    )
    assert [(a.row_id, a.subject_type) for a in offer.approvals] == [(row, "person")]
