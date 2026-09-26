"""Offline checks for the number catalog sync: each carrier mapper on saved
API responses, and the merge rules (hand-verified rows win, vanished rows stay)."""

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "sync_numbers", Path(__file__).with_name("sync_numbers.py")
)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

FIX = Path(__file__).with_name("fixtures")


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def test_money_formats_carrier_prices():
    assert sync.money("2.00000") == "2.00"
    assert sync.money("1.15") == "1.15"
    assert sync.money("0.3") == "0.30"
    assert sync.money(2) == "2.00"
    assert sync.money("0.0") == "0.00"


def test_twilio_gb_rows():
    countries = load("twilio_available_GB.json")
    countries = [
        {"country_code": countries["country_code"], "country": countries["country"]}
    ]
    types = {"GB": ["local", "mobile", "toll_free"]}
    pricing = {"GB": load("twilio_pricing_GB.json")}
    mobile = load("twilio_available_GB_Mobile.json")["available_phone_numbers"]
    local = [
        {
            "capabilities": {"voice": True, "SMS": False, "MMS": False},
            "address_requirements": "local",
        }
    ]
    samples = {
        ("GB", "mobile"): mobile,
        ("GB", "local"): local,
        ("GB", "toll_free"): [],
    }
    regulated = sync.twilio_regulated(load("twilio_regulations_GB.json")["results"])
    rows, skipped = sync.map_twilio(
        countries, types, pricing, samples, regulated, {"GB": "44"}
    )
    by = {r["number_type"]: r for r in rows}
    assert by["mobile"]["usd_per_month"] == "2.50"
    assert by["mobile"]["sms"] is True and by["mobile"]["mms"] is False
    assert by["mobile"]["verification_required"] is True  # GB mobile has a regulation
    assert by["local"]["usd_per_month"] == "1.15" and by["local"]["sms"] is False
    assert by["local"]["verification_required"] is True  # address_requirements local
    assert "toll_free" not in by and any("toll_free" in s for s in skipped)
    assert by["mobile"]["display_name"] == "United Kingdom mobile"


def test_telnyx_gb_mobile_row():
    coverage = load("telnyx_country_coverage.json")["data"]
    sample = load("telnyx_available_GB_mobile.json")["data"]
    reqs = {("GB", "mobile"): load("telnyx_requirements_GB_mobile.json")["data"]}
    rows, skipped = sync.map_telnyx(
        coverage, {("GB", "mobile"): sample}, reqs, {"GB": "44", "US": "1"}
    )
    gb = next(
        r for r in rows if r["country_code"] == "GB" and r["number_type"] == "mobile"
    )
    assert gb["usd_per_month"] == "2.00" and gb["setup_usd"] == "2.00"
    assert gb["voice"] and gb["sms"] and not gb["mms"]
    assert gb["verification_required"] is True
    # types with no sample are reported, never invented
    assert any(s.startswith("GB:local") for s in skipped)
    assert not any(r["country_code"] == "AF" for r in rows)


def test_telnyx_no_requirements_means_no_verification():
    coverage = {
        "United States": {"code": "US", "numbers": True, "phone_number_type": ["local"]}
    }
    sample = [
        {
            "cost_information": {
                "monthly_cost": "1.00000",
                "upfront_cost": "1.00000",
                "currency": "USD",
            },
            "features": [{"name": "voice"}, {"name": "sms"}, {"name": "mms"}],
        }
    ]
    rows, _ = sync.map_telnyx(
        coverage,
        {("US", "local"): sample},
        {("US", "local"): load("telnyx_requirements_US_local.json")["data"]},
        {"US": "1"},
    )
    assert rows[0]["verification_required"] is False and rows[0]["mms"] is True


def test_telnyx_price_range_is_noted():
    coverage = {"X": {"code": "XX", "numbers": True, "phone_number_type": ["local"]}}
    sample = [
        {
            "cost_information": {
                "monthly_cost": "1.00000",
                "upfront_cost": "0",
                "currency": "USD",
            },
            "features": [{"name": "voice"}],
        },
        {
            "cost_information": {
                "monthly_cost": "3.50000",
                "upfront_cost": "0",
                "currency": "USD",
            },
            "features": [{"name": "voice"}],
        },
    ]
    rows, _ = sync.map_telnyx(coverage, {("XX", "local"): sample}, {}, {"XX": "99"})
    assert rows[0]["usd_per_month"] == "1.00"
    assert "1.00 to 3.50" in rows[0]["notes"]
    assert "setup_usd" not in rows[0]


def test_didww_gb_rows():
    countries = load("didww_countries.json")["data"]
    types = {
        t["id"]: t["attributes"]["name"]
        for t in load("didww_did_group_types.json")["data"]
    }
    rows, _ = sync.map_didww(countries, types, {"GB": load("didww_did_groups_GB.json")})
    by = {r["number_type"]: r for r in rows}
    assert (
        by["mobile"]["usd_per_month"] == "2.50"
    )  # the 0-channel SKU, not the 2-channel one
    assert by["mobile"]["voice"] and by["mobile"]["sms"]
    assert by["mobile"]["verification_required"] is False
    assert by["mobile"]["dial_code"] == "44"
    assert by["local"]["usd_per_month"] == "1.20"
    assert by["local"]["sms"] is False


def test_merge_keeps_hand_verified_and_vanished_rows():
    existing = [
        {
            "country_code": "GB",
            "number_type": "mobile",
            "display_name": "United Kingdom mobile",
            "dial_code": "44",
            "usd_per_month": "2.50",
            "voice": True,
            "sms": True,
            "mms": False,
            "verification_required": True,
            "last_verified": "2026-09-24",
            "last_changed_at": "2026-09-24",
            "verification_method": "manual-confirmed",
            "verified_by": "twilio-api",
            "source_url": "https://x",
        },
        {
            "country_code": "US",
            "number_type": "local",
            "display_name": "United States local",
            "dial_code": "1",
            "usd_per_month": "1.15",
            "voice": True,
            "sms": True,
            "mms": True,
            "verification_required": False,
            "last_verified": "2026-07-17",
            "last_changed_at": "2026-07-17",
            "verification_method": "carrier-sync",
            "verified_by": "twilio-sync",
            "source_url": "https://x",
        },
        {
            "country_code": "AR",
            "number_type": "local",
            "display_name": "Argentina local",
            "dial_code": "54",
            "usd_per_month": "8.00",
            "voice": True,
            "sms": False,
            "mms": False,
            "verification_required": True,
            "last_verified": "2026-07-17",
            "last_changed_at": "2026-07-17",
            "verification_method": "carrier-sync",
            "verified_by": "twilio-sync",
            "source_url": "https://x",
        },
    ]
    fetched = [
        {
            "country_code": "GB",
            "number_type": "mobile",
            "display_name": "United Kingdom mobile",
            "dial_code": "44",
            "usd_per_month": "1.15",
            "voice": True,
            "sms": True,
            "mms": False,
            "verification_required": True,
        },
        {
            "country_code": "US",
            "number_type": "local",
            "display_name": "United States local",
            "dial_code": "",
            "usd_per_month": "1.20",
            "voice": True,
            "sms": True,
            "mms": True,
            "verification_required": False,
        },
        {
            "country_code": "PT",
            "number_type": "local",
            "display_name": "Portugal local",
            "dial_code": "351",
            "usd_per_month": "1.00",
            "voice": True,
            "sms": False,
            "mms": False,
            "verification_required": True,
        },
    ]
    numbers, report = sync.merge(
        existing, fetched, "2026-09-28", "https://src", "twilio-api-sync"
    )
    by = {f"{n['country_code']}:{n['number_type']}": n for n in numbers}
    assert (
        by["GB:mobile"]["usd_per_month"] == "2.50"
        and by["GB:mobile"]["verification_method"] == "manual-confirmed"
    )
    assert report["kept"] and report["kept"][0].startswith("GB:mobile")
    assert (
        by["US:local"]["usd_per_month"] == "1.20"
        and by["US:local"]["last_changed_at"] == "2026-09-28"
    )
    assert by["US:local"]["dial_code"] == "1"  # borrowed from the previous row
    assert by["US:local"]["verified_by"] == "twilio-api-sync"
    assert by["PT:local"]["last_verified"] == "2026-09-28" and report["added"]
    assert "not offered" in by["AR:local"]["notes"] and report["vanished"] == [
        "AR:local"
    ]
    assert by["AR:local"]["available"] is False and by["US:local"]["available"] is True
    assert [n["country_code"] for n in numbers] == ["AR", "GB", "PT", "US"]


def test_regulatory_block_is_derived_from_rows(tmp_path):
    path = tmp_path / "twilio.json"
    path.write_text(
        json.dumps(
            {
                "regulatory": {"sms_registration_required": ["US"]},
                "a2p_10dlc": [{"carrier": "AT&T"}],
            }
        )
    )
    numbers = [
        {
            "country_code": "GB",
            "number_type": "mobile",
            "verification_required": True,
            "available": True,
        },
        {"country_code": "US", "number_type": "local", "verification_required": False},
    ]
    out = sync.write_catalog(path, "twilio", numbers)
    assert out["version"] == 3 and out["provider"] == "twilio"
    assert out["regulatory"]["phone_setup_required"] == ["GB:mobile"]
    assert out["regulatory"]["sms_registration_required"] == ["US"]
    assert out["a2p_10dlc"] == [{"carrier": "AT&T"}]
    assert json.loads(path.read_text())["provider"] == "twilio"
