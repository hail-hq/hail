from __future__ import annotations

import json
from uuid import uuid4

import pytest
import responses
from hailhq.core.config import settings
from hailhq.core.providers.voice import CarrierNotConfigured, CarrierRequestError
from hailhq.core.providers.voice.didww import (
    didww_order_outcome,
    place_didww_order,
    release_didww_number,
    terminate_did,
)

BASE = "https://sandbox-api.didww.com/v3"
NUMBER = uuid4()
E164 = "+351300000001"
ORDER = "o0000000-0000-0000-0000-000000000001"
DID = "d0000000-0000-0000-0000-000000000002"
ADDR = "a0000000-0000-0000-0000-000000000003"
VER = "v0000000-0000-0000-0000-000000000004"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "didww_api_key", "test-key")
    monkeypatch.setattr(settings, "didww_environment", "sandbox")


def _inventory():
    responses.add(
        responses.GET,
        f"{BASE}/available_dids",
        json={
            "data": [
                {
                    "id": "avail-1",
                    "type": "available_dids",
                    "attributes": {"number": "351300000001"},
                    "relationships": {
                        "did_group": {"data": {"id": "g1", "type": "did_groups"}}
                    },
                }
            ],
            "included": [
                {
                    "id": "g1",
                    "type": "did_groups",
                    "attributes": {"features": ["voice_out"]},
                    "relationships": {
                        "stock_keeping_units": {
                            "data": [{"id": "sku-1", "type": "stock_keeping_units"}]
                        }
                    },
                },
                {
                    "id": "sku-1",
                    "type": "stock_keeping_units",
                    "attributes": {
                        "channels_included_count": 0,
                        "monthly_price": "3.5",
                        "setup_price": "3.5",
                    },
                },
            ],
        },
    )


def _did(awaiting: bool, verification: str | None):
    responses.add(
        responses.GET,
        f"{BASE}/dids",
        json={
            "data": [
                {
                    "id": DID,
                    "type": "dids",
                    "attributes": {
                        "number": "351300000001",
                        "awaiting_registration": awaiting,
                    },
                    "relationships": {
                        "address_verification": {
                            "data": (
                                {"id": verification, "type": "address_verifications"}
                                if verification
                                else None
                            )
                        }
                    },
                }
            ]
        },
    )


def _order(status):
    responses.add(
        responses.GET,
        f"{BASE}/orders/{ORDER}",
        json={
            "data": {"id": ORDER, "type": "orders", "attributes": {"status": status}}
        },
    )


@responses.activate
async def test_place_order_uses_exact_number_and_metered_sku():
    _inventory()
    responses.add(
        responses.POST,
        f"{BASE}/orders",
        status=201,
        json={
            "data": {"id": ORDER, "type": "orders", "attributes": {"status": "pending"}}
        },
    )
    assert await place_didww_order(NUMBER, E164, ADDR) == ORDER
    sent = json.loads(responses.calls[1].request.body)
    item = sent["data"]["attributes"]["items"][0]["attributes"]
    assert item == {"available_did_id": "avail-1", "sku_id": "sku-1"}
    assert sent["data"]["attributes"]["external_reference_id"] == str(NUMBER)
    assert sent["data"]["attributes"]["allow_back_ordering"] is False


@responses.activate
async def test_place_order_number_gone_is_a_409():
    responses.add(
        responses.GET, f"{BASE}/available_dids", json={"data": [], "included": []}
    )
    with pytest.raises(CarrierRequestError) as exc:
        await place_didww_order(NUMBER, E164, ADDR)
    assert exc.value.status == 409


@responses.activate
async def test_place_order_carrier_error_is_carrier_request_error():
    _inventory()
    responses.add(
        responses.POST,
        f"{BASE}/orders",
        status=422,
        json={"errors": [{"title": "insufficient funds"}]},
    )
    with pytest.raises(CarrierRequestError) as exc:
        await place_didww_order(NUMBER, E164, ADDR)
    assert exc.value.status == 422


@responses.activate
async def test_outcome_pending_order():
    _order("pending")
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == (
        "pending",
        None,
        ORDER,
    )


@responses.activate
async def test_outcome_canceled_order_is_failed():
    _order("canceled")
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == (
        "failed",
        None,
        ORDER,
    )


@responses.activate
async def test_outcome_completed_and_registered_is_active():
    _order("completed")
    _did(awaiting=False, verification=None)
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == (
        "active",
        DID,
        ORDER,
    )
    assert f"filter%5Border.id%5D={ORDER}" in responses.calls[1].request.url


@responses.activate
async def test_outcome_creates_verification_once_when_awaiting():
    _order("completed")
    _did(awaiting=True, verification=None)
    responses.add(
        responses.GET,
        f"{BASE}/addresses/{ADDR}",
        json={
            "data": {
                "id": ADDR,
                "type": "addresses",
                "attributes": {"description": "Customer support line"},
            }
        },
    )
    responses.add(
        responses.POST,
        f"{BASE}/address_verifications",
        status=201,
        json={
            "data": {
                "id": VER,
                "type": "address_verifications",
                "attributes": {"status": "pending"},
            }
        },
    )
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == (
        "pending",
        None,
        ORDER,
    )
    sent = json.loads(responses.calls[3].request.body)
    assert sent["data"]["relationships"]["dids"]["data"] == [
        {"id": DID, "type": "dids"}
    ]
    assert sent["data"]["relationships"]["address"]["data"] == {
        "id": ADDR,
        "type": "addresses",
    }
    assert sent["data"]["attributes"]["service_description"] == "Customer support line"


@responses.activate
async def test_outcome_reuses_existing_verification():
    _order("completed")
    _did(awaiting=True, verification=VER)
    responses.add(
        responses.GET,
        f"{BASE}/address_verifications/{VER}",
        json={
            "data": {
                "id": VER,
                "type": "address_verifications",
                "attributes": {"status": "pending"},
            }
        },
    )
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == (
        "pending",
        None,
        ORDER,
    )
    assert not any(c.request.method == "POST" for c in responses.calls)


@responses.activate
async def test_outcome_approved_verification_is_active():
    _order("completed")
    _did(awaiting=True, verification=VER)
    responses.add(
        responses.GET,
        f"{BASE}/address_verifications/{VER}",
        json={
            "data": {
                "id": VER,
                "type": "address_verifications",
                "attributes": {"status": "approved"},
            }
        },
    )
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == (
        "active",
        DID,
        ORDER,
    )


@responses.activate
async def test_outcome_rejected_verification():
    _order("completed")
    _did(awaiting=True, verification=VER)
    responses.add(
        responses.GET,
        f"{BASE}/address_verifications/{VER}",
        json={
            "data": {
                "id": VER,
                "type": "address_verifications",
                "attributes": {"status": "rejected", "reject_reasons": ["blurry"]},
            }
        },
    )
    assert await didww_order_outcome(E164, NUMBER, ORDER, ADDR) == (
        "rejected_registration",
        DID,
        ORDER,
    )


@responses.activate
async def test_outcome_awaiting_without_address_stays_pending():
    _order("completed")
    _did(awaiting=True, verification=None)
    assert await didww_order_outcome(E164, NUMBER, ORDER, None) == (
        "pending",
        None,
        ORDER,
    )
    assert len(responses.calls) == 2


@responses.activate
async def test_outcome_recovers_lost_order_id_by_reference():
    responses.add(
        responses.GET,
        f"{BASE}/orders",
        json={
            "data": [
                {
                    "id": ORDER,
                    "type": "orders",
                    "attributes": {
                        "status": "pending",
                        "external_reference_id": str(NUMBER),
                    },
                }
            ]
        },
    )
    assert await didww_order_outcome(E164, NUMBER, None, ADDR) == (
        "pending",
        None,
        ORDER,
    )
    assert (
        f"filter%5Bexternal_reference_id%5D={NUMBER}" in responses.calls[0].request.url
    )


@responses.activate
async def test_outcome_missing_when_no_order_by_reference():
    responses.add(responses.GET, f"{BASE}/orders", json={"data": []})
    assert await didww_order_outcome(E164, NUMBER, None, ADDR) == (
        "missing",
        None,
        None,
    )


@responses.activate
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_outcome_transient_error_raises(status):
    responses.add(
        responses.GET,
        f"{BASE}/orders/{ORDER}",
        status=status,
        json={"errors": [{"title": "x"}]},
    )
    with pytest.raises(Exception):
        await didww_order_outcome(E164, NUMBER, ORDER, ADDR)


@responses.activate
async def test_terminate_did_patches_terminated():
    responses.add(
        responses.PATCH,
        f"{BASE}/dids/{DID}",
        json={"data": {"id": DID, "type": "dids", "attributes": {"terminated": True}}},
    )
    await terminate_did(DID)
    sent = json.loads(responses.calls[0].request.body)
    assert sent["data"]["attributes"] == {"terminated": True}


async def test_release_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "didww_api_key", "")
    with pytest.raises(CarrierNotConfigured):
        await release_didww_number(DID)


@responses.activate
async def test_release_tolerates_404():
    responses.add(
        responses.PATCH,
        f"{BASE}/dids/{DID}",
        status=404,
        json={"errors": [{"title": "not found"}]},
    )
    await release_didww_number(DID)  # already gone at the carrier
