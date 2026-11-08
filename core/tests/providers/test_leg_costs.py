"""Each carrier's call cost lookup (``leg_costs``), mocked at the HTTP boundary."""

from __future__ import annotations

import gzip
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import requests
import responses
from hailhq.core.config import settings
from hailhq.core.providers import telnyx as telnyx_client
from hailhq.core.providers.voice import didww, telnyx, twilio
from hailhq.core.providers.voice.base import (
    CarrierNotConfigured,
    CarrierRequestError,
    LegCost,
    LegRef,
)

NOW = datetime.now(timezone.utc) - timedelta(hours=1)
ACCOUNT_SID = "ACtest1234567890abcdef1234567890ab"
TWILIO_CALLS = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Calls"
DIDWW = "https://sandbox-api.didww.com/v3"


def _leg(direction="outbound", sip=None, sid=None) -> LegRef:
    return LegRef(uuid4(), direction, sip, sid, NOW, NOW + timedelta(minutes=2))


# -- Twilio -------------------------------------------------------------------


@pytest.fixture()
def twilio_keys(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", ACCOUNT_SID)
    monkeypatch.setattr(settings, "twilio_auth_token", "token")


def _twilio_call(sid, price, unit="USD", duration="38"):
    responses.add(
        responses.GET,
        f"{TWILIO_CALLS}/{sid}.json",
        json={
            "sid": sid,
            "account_sid": ACCOUNT_SID,
            "status": "completed",
            "price": price,
            "price_unit": unit,
            "duration": duration,
        },
    )


@responses.activate
async def test_twilio_reads_the_call_price(twilio_keys):
    priced, unpriced, other, no_sid = (
        _leg(sid="CA1"),
        _leg(sid="CA2"),
        _leg(sid="CA3"),
        _leg(sip="x"),
    )
    _twilio_call("CA1", "-0.15630")
    _twilio_call("CA2", None)  # Twilio has not priced it yet
    _twilio_call("CA3", "-1.0", unit="EUR")
    found = await twilio.leg_costs([priced, unpriced, other, no_sid])
    assert found == {priced.id: LegCost(Decimal("0.15630"))}
    assert len(responses.calls) == 3


@responses.activate
async def test_twilio_skips_an_unknown_call(twilio_keys):
    responses.add(
        responses.GET,
        f"{TWILIO_CALLS}/CA9.json",
        json={"code": 20404, "message": "not found", "status": 404},
        status=404,
    )
    assert await twilio.leg_costs([_leg(sid="CA9")]) == {}


@responses.activate
async def test_twilio_raises_when_every_lookup_fails(twilio_keys):
    # A bad key or an outage is not "no record yet": the legs stay pending.
    responses.add(
        responses.GET,
        f"{TWILIO_CALLS}/CA1.json",
        json={"code": 20003, "message": "auth", "status": 401},
        status=401,
    )
    with pytest.raises(CarrierRequestError):
        await twilio.leg_costs([_leg(sid="CA1")])


@responses.activate
async def test_twilio_keeps_found_costs_when_one_lookup_times_out(twilio_keys):
    _twilio_call("CA1", "-0.0068")
    responses.add(
        responses.GET,
        f"{TWILIO_CALLS}/CA2.json",
        body=requests.exceptions.ConnectTimeout(),
    )
    priced = _leg(sid="CA1")
    found = await twilio.leg_costs([priced, _leg(sid="CA2")])
    assert found == {priced.id: LegCost(Decimal("0.0068"))}


async def test_twilio_needs_credentials(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    with pytest.raises(CarrierNotConfigured):
        await twilio.leg_costs([_leg(sid="CA1")])


# -- Telnyx -------------------------------------------------------------------


@pytest.fixture()
def telnyx_records(monkeypatch):
    """Telnyx answers from ``records``, keyed by SIP Call-ID."""
    monkeypatch.setattr(settings, "telnyx_api_key", "key")
    records: dict[str, list[dict]] = {}
    asked: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/detail_records"
        params = dict(request.url.params)
        asked.append(params)
        return httpx.Response(
            200, json={"data": records.get(params["filter[sip_call_id]"], [])}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    monkeypatch.setattr(telnyx_client, "get_http_client", lambda: client)
    return records, asked


async def test_telnyx_reads_the_detail_record(telnyx_records):
    records, asked = telnyx_records
    records["sip-1"] = [
        {"cost": "0.0128", "billed_sec": 240, "rate": "0.00320", "currency": "USD"}
    ]
    leg, waiting, no_sip = _leg(sip="sip-1"), _leg(sip="sip-2"), _leg(sid="CA1")
    found = await telnyx.leg_costs([leg, waiting, no_sip])
    assert found == {leg.id: LegCost(Decimal("0.0128"), 240, Decimal("0.00320"))}
    assert [a["filter[sip_call_id]"] for a in asked] == ["sip-1", "sip-2"]
    assert {a["filter[record_type]"] for a in asked} == {"sip-trunking"}


async def test_telnyx_unknown_billed_time_stays_unknown(telnyx_records):
    records, _ = telnyx_records
    records["sip-1"] = [{"cost": "0.01", "rate": "0", "currency": "USD"}]
    leg = _leg(sip="sip-1")
    assert await telnyx.leg_costs([leg]) == {
        leg.id: LegCost(Decimal("0.01"), None, Decimal(0))
    }


async def test_telnyx_skips_a_non_usd_record(telnyx_records):
    records, _ = telnyx_records
    records["sip-1"] = [{"cost": "0.01", "billed_sec": 60, "currency": "EUR"}]
    assert await telnyx.leg_costs([_leg(sip="sip-1")]) == {}


async def test_telnyx_raises_when_every_lookup_fails(monkeypatch):
    monkeypatch.setattr(settings, "telnyx_api_key", "key")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(401, json={}))
    )
    monkeypatch.setattr(telnyx_client, "get_http_client", lambda: client)
    with pytest.raises(httpx.HTTPError):
        await telnyx.leg_costs([_leg(sip="sip-1")])


async def test_telnyx_needs_a_key(monkeypatch):
    monkeypatch.setattr(settings, "telnyx_api_key", "")
    with pytest.raises(CarrierNotConfigured):
        await telnyx.leg_costs([_leg(sip="sip-1")])


# -- DIDWW --------------------------------------------------------------------

CSV_HEADER = (
    "Date/Time Start (UTC),Date/Time Connect (UTC),Date/Time End (UTC),Status,"
    "Source,CLI,Destination Number,Call Duration (sec),Billing Duration (sec),"
    "Disconnect Code,Disconnect Reason,Voice OUT Trunk,Destination Country,"
    "Network,Call Type,Rate (USD),Charged (USD),P-Charge-Info,Call ID\n"
)


@pytest.fixture()
def didww_keys(monkeypatch):
    monkeypatch.setattr(settings, "didww_api_key", "test-key")
    monkeypatch.setattr(settings, "didww_environment", "sandbox")
    monkeypatch.setattr(didww.time, "sleep", lambda _s: None)


def _export(rows: str, ready_after: int = 0):
    responses.add(
        responses.POST,
        f"{DIDWW}/exports",
        json={"data": {"id": "e1", "type": "exports", "attributes": {}}},
        status=201,
    )
    for _ in range(ready_after):
        responses.add(
            responses.GET,
            f"{DIDWW}/exports/e1",
            json={"data": {"attributes": {"status": "processing", "url": None}}},
        )
    responses.add(
        responses.GET,
        f"{DIDWW}/exports/e1",
        json={
            "data": {
                "attributes": {
                    "status": "completed",
                    "url": f"{DIDWW}/exports/e1.csv.gz",
                }
            }
        },
    )
    responses.add(
        responses.GET,
        f"{DIDWW}/exports/e1.csv.gz",
        body=gzip.compress((CSV_HEADER + rows).encode()),
    )


@responses.activate
async def test_didww_matches_the_export_by_call_id(didww_keys):
    leg, other, inbound = (
        _leg(sip="sip-1"),
        _leg(sip="sip-2"),
        _leg("inbound", sip="sip-3"),
    )
    _export(
        "2026-11-08 10:00:00,2026-11-08 10:00:05,2026-11-08 10:01:00,success,,"
        "+351300000001,+34600000000,55,60,200,OK,hail,ES,mobile,INTERNATIONAL,"
        "0.0450,0.0450,,sip-1\n",
        ready_after=1,
    )
    found = await didww.leg_costs([leg, other, inbound])
    assert found == {leg.id: LegCost(Decimal("0.0450"), 60, Decimal("0.0450"))}
    body = responses.calls[0].request.body
    assert b'"export_type": "cdr_out"' in body


@responses.activate
async def test_didww_asks_nothing_for_inbound_legs(didww_keys):
    assert await didww.leg_costs([_leg("inbound", sip="sip-3")]) == {}
    assert len(responses.calls) == 0


@responses.activate
async def test_didww_export_not_ready_is_tried_next_pass(didww_keys, monkeypatch):
    monkeypatch.setattr(didww, "EXPORT_WAIT_SECONDS", -1)
    _export("", ready_after=1)
    with pytest.raises(didww.ExportNotReady):
        await didww.leg_costs([_leg(sip="sip-1")])


@responses.activate
async def test_didww_skips_a_row_it_cannot_read(didww_keys):
    bad, good = _leg(sip="sip-1"), _leg(sip="sip-2")
    _export(
        "2026-11-08 10:00:00,,,success,,,,0,0,200,OK,hail,ES,mobile,X,"
        "n/a,n/a,,sip-1\n"
        "2026-11-08 10:00:00,,,success,,,,55,60,200,OK,hail,ES,mobile,X,"
        "0.0450,0.0450,,sip-2\n",
    )
    found = await didww.leg_costs([bad, good])
    assert found == {good.id: LegCost(Decimal("0.0450"), 60, Decimal("0.0450"))}
