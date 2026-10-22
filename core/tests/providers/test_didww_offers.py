"""``didww_offers`` against DIDWW's JSON:API, mocked with ``responses``."""

from __future__ import annotations

from uuid import uuid4

import pytest
import responses
from hailhq.core.carrier_offer import Approval
from hailhq.core.config import settings
from hailhq.core.providers.voice import didww as mod
from hailhq.core.providers.voice.didww import didww_offers

BASE = "https://sandbox-api.didww.com/v3"
ORG = uuid4()
COUNTRY_ID = "c0000000-0000-0000-0000-000000000001"
NATIONAL_ID = "t0000000-0000-0000-0000-000000000002"
GROUP_ID = "g0000000-0000-0000-0000-000000000003"
SKU_ID = "s0000000-0000-0000-0000-000000000004"
REQ_ID = "r0000000-0000-0000-0000-000000000005"


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "didww_api_key", "test-key")
    monkeypatch.setattr(settings, "didww_environment", "sandbox")
    monkeypatch.setattr(settings, "livekit_didww_sip_outbound_trunk_id", "ST_didww")
    mod.lookup_ids.cache_clear()


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
                    "id": "t-local",
                    "type": "did_group_types",
                    "attributes": {"name": "Local"},
                },
                {
                    "id": NATIONAL_ID,
                    "type": "did_group_types",
                    "attributes": {"name": "National"},
                },
            ]
        },
    )


def _inventory(
    *,
    features=("voice_in", "voice_out"),
    skus=None,
    needs_registration=True,
    numbers=("351300000001",),
    identity_type=None,
):
    if skus is None:
        skus = [
            {
                "id": SKU_ID,
                "type": "stock_keeping_units",
                "attributes": {
                    "setup_price": "3.5",
                    "monthly_price": "3.5",
                    "channels_included_count": 0,
                },
            }
        ]
    included = [
        {
            "id": GROUP_ID,
            "type": "did_groups",
            "attributes": {"features": list(features), "area_name": "Portugal"},
            "meta": {"needs_registration": needs_registration},
            "relationships": {
                "stock_keeping_units": {
                    "data": [
                        {"id": s["id"], "type": "stock_keeping_units"} for s in skus
                    ]
                },
                "address_requirement": {
                    "data": {"id": REQ_ID, "type": "address_requirements"}
                },
            },
        },
        *skus,
        {
            "id": REQ_ID,
            "type": "address_requirements",
            "attributes": {
                "personal_proof_qty": 1,
                "business_proof_qty": 1,
                "address_proof_qty": 0,
                **({"identity_type": identity_type} if identity_type else {}),
            },
        },
    ]
    responses.add(
        responses.GET,
        f"{BASE}/available_dids",
        json={
            "data": [
                {
                    "id": f"a-{n}",
                    "type": "available_dids",
                    "attributes": {"number": n},
                    "relationships": {
                        "did_group": {"data": {"id": GROUP_ID, "type": "did_groups"}}
                    },
                }
                for n in numbers
            ],
            "included": included,
        },
    )


def _no_address():
    responses.add(responses.GET, f"{BASE}/addresses", json={"data": []})


@responses.activate
async def test_offers_verification_required_with_documents():
    _static()
    _inventory()
    _no_address()
    offers = await didww_offers(ORG, "PT", "national", ["voice"])
    assert len(offers) == 1
    o = offers[0]
    assert o.provider == "didww"
    assert o.e164 == "+351300000001"
    assert o.capabilities == ["voice"]
    assert (o.monthly_cents, o.setup_cents) == (350, 350)
    assert o.readiness == "verification_required"
    assert o.regulatory_friction == "documents"
    assert o.verification_id is None
    query = responses.calls[2].request.url
    assert "filter%5Bdid_group.features%5D=voice_out" in query
    assert f"filter%5Bdid_group_type.id%5D={NATIONAL_ID}" in query


def _approved_addresses(*ids):
    responses.add(
        responses.GET,
        f"{BASE}/addresses",
        json={
            "data": [
                {
                    "id": i,
                    "type": "addresses",
                    "attributes": {"external_reference_id": f"hail:{ORG}:PT:national"},
                }
                for i in ids
            ]
        },
    )


@responses.activate
async def test_offers_ready_with_approved_address():
    """Both kinds' addresses carry the same approved reference: each Hail
    approval is matched by its own address id, never the first found."""
    _static()
    _inventory()
    _approved_addresses("addr-0", "addr-1")
    person = Approval(
        row_id=uuid4(),
        subject_type="person",
        verification_id="addr-1",
        address_id="addr-1",
    )
    # A second person of the same org: several per kind are allowed.
    other = Approval(
        row_id=uuid4(),
        subject_type="person",
        verification_id="addr-0",
        address_id="addr-0",
    )
    business = Approval(
        row_id=uuid4(),
        subject_type="business",
        verification_id="addr-9",
        address_id="addr-9",
    )
    (o,) = await didww_offers(
        ORG,
        "PT",
        "national",
        ["voice"],
        approvals=[person, other, business],
    )
    assert o.readiness == "ready"
    assert o.subject_types == ["business", "person"]
    assert o.approvals == [person, other]
    assert "filter%5Bexternal_reference_id%5D=hail%3A" in responses.calls[3].request.url


@responses.activate
async def test_offers_ignore_an_approval_with_no_hail_row():
    """An address approved at DIDWW that Hail has no approved row for is
    not used: Hail's rows decide."""
    _static()
    _inventory()
    _approved_addresses("addr-1")
    (o,) = await didww_offers(ORG, "PT", "national", ["voice"])
    assert o.readiness == "verification_required"
    assert o.approvals == []
    assert len(responses.calls) == 3  # no address lookup without approvals


@pytest.mark.parametrize(
    ("identity", "kinds"),
    [
        ("personal", ["person"]),
        ("business", ["business"]),
        ("any", ["business", "person"]),
    ],
)
@responses.activate
async def test_offers_list_who_may_register(identity, kinds):
    _static()
    _inventory(identity_type=identity)
    _approved_addresses("addr-1")
    person = Approval(
        row_id=uuid4(),
        subject_type="person",
        verification_id="addr-1",
        address_id="addr-1",
    )
    (o,) = await didww_offers(ORG, "PT", "national", ["voice"], approvals=[person])
    assert o.subject_types == kinds
    # A person's approval does not count where only a business may register.
    assert o.readiness == ("ready" if "person" in kinds else "verification_required")


@responses.activate
async def test_offers_ready_when_no_registration_needed():
    _static()
    _inventory(needs_registration=False)
    (o,) = await didww_offers(ORG, "PT", "national", ["voice"])
    assert o.readiness == "ready" and o.regulatory_friction == "none"
    assert len(responses.calls) == 3  # no address lookup


@responses.activate
async def test_offers_skip_group_without_metered_sku():
    _static()
    _inventory(
        skus=[
            {
                "id": SKU_ID,
                "type": "stock_keeping_units",
                "attributes": {
                    "setup_price": "3.5",
                    "monthly_price": "3.5",
                    "channels_included_count": 2,
                },
            }
        ]
    )
    assert await didww_offers(ORG, "PT", "national", ["voice"]) == []


@responses.activate
async def test_offers_skip_group_without_voice_out():
    _static()
    _inventory(features=("voice_in",))
    assert await didww_offers(ORG, "PT", "national", ["voice"]) == []


@responses.activate
async def test_sms_request_yields_nothing():
    _static()
    assert await didww_offers(ORG, "PT", "national", ["voice", "sms"]) == []
    assert len(responses.calls) == 0


@responses.activate
async def test_unknown_country_yields_nothing():
    responses.add(responses.GET, f"{BASE}/countries", json={"data": []})
    assert await didww_offers(ORG, "XX", "national", ["voice"]) == []


async def test_unconfigured_yields_nothing(monkeypatch):
    monkeypatch.setattr(settings, "didww_api_key", "")
    assert await didww_offers(ORG, "PT", "national", ["voice"]) == []


@responses.activate
async def test_no_trunk_yields_nothing(monkeypatch):
    """A number that cannot dial is never quoted, so it is never sold."""
    monkeypatch.setattr(settings, "livekit_didww_sip_outbound_trunk_id", "")
    assert await didww_offers(ORG, "PT", "national", ["voice"]) == []
    assert len(responses.calls) == 0


@responses.activate
async def test_e164_filter_and_exact_match():
    _static()
    _inventory(numbers=("351300000001", "351300000002"))
    _no_address()
    offers = await didww_offers(ORG, "PT", "national", ["voice"], e164="+351300000002")
    assert [o.e164 for o in offers] == ["+351300000002"]
    assert "filter%5Bnumber_contains%5D=351300000002" in responses.calls[2].request.url


@responses.activate
async def test_area_code_keeps_only_numbers_that_start_with_it(monkeypatch):
    monkeypatch.setattr(
        mod, "lookup_ids", lambda country: (COUNTRY_ID, {"local": "t-local"})
    )
    # "contains" also matches 1480 in the middle of a number.
    _inventory(numbers=("12148050000", "14805550001"))
    _no_address()
    offers = await didww_offers(ORG, "US", "local", ["voice"], area_code="480")
    assert [o.e164 for o in offers] == ["+14805550001"]
    assert "filter%5Bnumber_contains%5D=1480" in responses.calls[0].request.url
    # Mid-number matches are dropped after the call: ask for more than 3.
    assert "page%5Bsize%5D=50" in responses.calls[0].request.url


def _cities(*cities):
    """(city id, region id) pairs, as DIDWW returns them with include=region."""
    responses.add(
        responses.GET,
        f"{BASE}/cities",
        json={
            "data": [
                {
                    "id": city,
                    "type": "cities",
                    "attributes": {"name": "Phoenix"},
                    "relationships": {
                        "region": {"data": {"id": region, "type": "regions"}}
                    },
                }
                for city, region in cities
            ]
        },
    )


@responses.activate
async def test_city_in_a_state_filters_by_that_city_id(monkeypatch):
    monkeypatch.setattr(
        mod, "lookup_ids", lambda country: (COUNTRY_ID, {"local": "t-local"})
    )
    # Phoenix, Oregon comes first: the state must pick Phoenix, Arizona.
    _cities(("city-or", "region-or"), ("city-az", "region-az"))
    responses.add(
        responses.GET,
        f"{BASE}/regions",
        json={"data": [{"id": "region-az", "type": "regions"}]},
    )
    _inventory(numbers=("16235550100",))
    _no_address()
    offers = await didww_offers(
        ORG, "US", "local", ["voice"], city="Phoenix", region="AZ"
    )
    assert [o.e164 for o in offers] == ["+16235550100"]
    assert "include=region" in responses.calls[0].request.url
    assert "filter%5Biso%5D=US-AZ" in responses.calls[1].request.url
    assert "filter%5Bcity.id%5D=city-az" in responses.calls[2].request.url


@responses.activate
async def test_unknown_city_is_no_stock(monkeypatch):
    monkeypatch.setattr(
        mod, "lookup_ids", lambda country: (COUNTRY_ID, {"local": "t-local"})
    )
    _cities()
    assert await didww_offers(ORG, "GB", "local", ["voice"], city="Atlantis") == []
    assert len(responses.calls) == 1  # No inventory search without a city id.


@responses.activate
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_errors_propagate(status):
    responses.add(
        responses.GET,
        f"{BASE}/countries",
        status=status,
        json={"errors": [{"title": "x"}]},
    )
    with pytest.raises(Exception):
        await didww_offers(ORG, "PT", "national", ["voice"])
