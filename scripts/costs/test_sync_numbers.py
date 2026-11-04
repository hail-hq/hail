"""Offline checks for the number catalog sync: each carrier mapper on saved
API responses, and the merge rules (hand-verified rows win, vanished rows stay)."""

import importlib.util
import json
from pathlib import Path

import pytest

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
    caps = {
        ("GB", "mobile"): {"voice": True, "sms": True, "mms": False},
        ("GB", "local"): {"voice": True, "sms": False, "mms": False},
    }
    rows, skipped = sync.map_twilio(
        countries, types, pricing, samples, regulated, {"GB": "44"}, caps
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
        coverage,
        {("GB", "mobile"): sample},
        reqs,
        {"GB": "44", "US": "1"},
        {("GB", "mobile"): {"voice": True, "sms": True, "mms": False}},
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
        {("US", "local"): {"voice": True, "sms": True, "mms": True}},
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
    rows, _ = sync.map_telnyx(
        coverage,
        {("XX", "local"): sample},
        {},
        {"XX": "99"},
        {("XX", "local"): {"voice": True, "sms": False, "mms": False}},
    )
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


def test_didww_marks_numbers_hail_cannot_sell():
    """Portugal-like country. Mobile: listed numbers that call out, sold as
    usual. National: calls out, but DIDWW picks the number at order time
    (not listed), so only support can order it. Toll-free: takes calls,
    cannot place them. Local: one listed and one unlisted area; the price
    comes from the listed one, the one Hail can sell."""
    countries = [
        {"id": "c1", "attributes": {"iso": "PT", "name": "Portugal", "prefix": "351"}}
    ]
    types = {"t-m": "Mobile", "t-n": "National", "t-t": "Toll-free", "t-l": "Local"}

    def group(gid, tid, features, listed, registration=False):
        return {
            "id": gid,
            "attributes": {"features": features},
            "meta": {
                "is_available": True,
                "needs_registration": registration,
                "available_dids_enabled": listed,
            },
            "relationships": {
                "did_group_type": {"data": {"type": "did_group_types", "id": tid}},
                "stock_keeping_units": {
                    "data": [{"type": "stock_keeping_units", "id": f"sku-{gid}"}]
                },
            },
        }

    def sku(gid, price):
        return {
            "type": "stock_keeping_units",
            "id": f"sku-{gid}",
            "attributes": {
                "monthly_price": price,
                "setup_price": "0.0",
                "channels_included_count": 0,
            },
        }

    out = ["voice_in", "voice_out"]
    payload = {
        "data": [
            group("m", "t-m", out, True),
            group("n", "t-n", out, False, registration=True),
            group("t", "t-t", ["voice_in"], False),
            group("l1", "t-l", out, False),
            group("l2", "t-l", out, True),
        ],
        "included": [
            sku("m", "8.0"),
            sku("n", "3.5"),
            sku("t", "10.0"),
            sku("l1", "1.0"),
            sku("l2", "2.0"),
        ],
    }
    rows, skipped = sync.map_didww(countries, types, {"PT": payload})
    by = {r["number_type"]: r for r in rows}
    assert not skipped
    assert "by_request" not in by["mobile"] and "receive_only" not in by["mobile"]
    assert by["national"]["by_request"] is True
    assert "receive_only" not in by["national"]
    assert by["national"]["verification_required"] is True
    assert by["toll_free"]["receive_only"] is True
    assert "by_request" not in by["toll_free"]
    assert by["toll_free"]["voice"] is True  # it takes calls
    assert "by_request" not in by["local"]
    assert by["local"]["usd_per_month"] == "2.00"
    assert "notes" not in by["local"]


def test_merge_reports_a_sale_mode_change():
    prev = {
        "country_code": "PT",
        "number_type": "national",
        "dial_code": "351",
        "usd_per_month": "3.50",
        "voice": True,
        "sms": False,
        "mms": False,
        "verification_required": True,
        "available": True,
        "last_verified": "2026-09-26",
        "last_changed_at": "2026-09-26",
        "verification_method": "carrier-sync",
        "verified_by": "didww-api-sync",
    }
    fetched = {**prev, "by_request": True}
    for k in ("last_verified", "last_changed_at", "verification_method", "verified_by"):
        fetched.pop(k)
    numbers, report = sync.merge(
        [prev], [fetched], "2026-10-07", "https://src", "didww-api-sync"
    )
    assert numbers[0]["by_request"] is True
    assert report["changed"] == ["PT:national: by_request None -> True"]


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


def test_merge_leaves_unobserved_rows_alone():
    prev = {
        "country_code": "PT",
        "number_type": "mobile",
        "display_name": "Portugal mobile",
        "dial_code": "351",
        "usd_per_month": "5.00",
        "voice": True,
        "sms": True,
        "mms": False,
        "verification_required": True,
        "available": True,
        "last_verified": "2026-07-17",
        "last_changed_at": "2026-07-17",
        "verification_method": "carrier-sync",
        "verified_by": "twilio-sync",
        "source_url": "https://x",
    }
    skipped = [
        "PT:mobile: no numbers offered to this account",
        "PT: group 1 has no resolvable type",
    ]
    assert sync.skipped_keys(skipped) == {"PT:mobile"}
    numbers, report = sync.merge(
        [prev],
        [],
        "2026-09-28",
        "https://src",
        "twilio-api-sync",
        unobserved=sync.skipped_keys(skipped),
    )
    # Not seeing stock is not the carrier dropping the type: nothing changes.
    assert numbers == [prev] and "notes" not in numbers[0]
    assert report["unobserved"] == ["PT:mobile"] and report["vanished"] == []


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


def test_merge_keeps_notes_on_a_vanished_row_and_rebuilds_it_on_return():
    prev = {
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
        "verification_method": "carrier-sync",
        "verified_by": "telnyx-api-sync",
        "source_url": "https://x",
        "notes": "price range 1.00 to 3.50; cheapest shown",
    }
    fetched_gb = {
        "country_code": "GB",
        "number_type": "mobile",
        "display_name": "United Kingdom mobile",
        "dial_code": "44",
        "usd_per_month": "1.15",
        "voice": True,
        "sms": True,
        "mms": False,
        "verification_required": True,
    }
    # Vanishes: the existing note stays, the vanished note is added after it.
    gone, report = sync.merge([prev], [], "2026-09-28", "https://src", "sync")
    assert report["vanished"] == ["GB:mobile"] and gone[0]["available"] is False
    assert gone[0]["last_verified"] == gone[0]["last_changed_at"] == "2026-09-28"
    assert gone[0]["notes"].startswith(prev["notes"] + "; not offered")
    # Vanishes again: no second copy of the vanished note.
    gone2, _ = sync.merge(gone, [], "2026-09-29", "https://src", "sync")
    assert gone2[0]["notes"] == gone[0]["notes"]
    assert gone2[0]["last_verified"] == "2026-09-29"
    assert gone2[0]["last_changed_at"] == "2026-09-28"
    # Returns: rebuilt from the carrier, on sale again, vanished note gone.
    back, report = sync.merge(gone2, [fetched_gb], "2026-09-30", "https://src", "sync")
    assert back[0]["available"] is True and "notes" not in back[0]
    assert back[0]["usd_per_month"] == "1.15" and report["vanished"] == []


def test_twilio_keys_outside_the_account_listing_are_unobserved():
    """The account is not enabled for PT, so PT is absent from Twilio's
    country list. That is not evidence Twilio stopped selling PT numbers."""
    countries = [{"country_code": "GB", "country": "United Kingdom"}]
    types = {"GB": ["local", "mobile", "fax"]}
    assert sync.twilio_observed_keys(countries, types) == {"GB:local", "GB:mobile"}
    existing = [
        {"country_code": "GB", "number_type": "local"},
        {"country_code": "GB", "number_type": "toll_free"},
        {"country_code": "PT", "number_type": "local"},
        {"country_code": "PT", "number_type": "national"},
    ]
    skipped = ["GB:mobile: no numbers offered to this account"]
    observed = sync.twilio_observed_keys(countries, types)
    assert sync.unobserved_keys(existing, skipped, observed) == {
        "GB:mobile",
        "GB:toll_free",
        "PT:local",
        "PT:national",
    }
    # A fetch that reports no view (other carriers) only trusts the skips.
    assert sync.unobserved_keys(existing, skipped, None) == {"GB:mobile"}


def test_merge_never_marks_a_hand_verified_row_unavailable():
    hand = {
        "country_code": "PT",
        "number_type": "national",
        "display_name": "Portugal national",
        "dial_code": "351",
        "usd_per_month": "1.00",
        "voice": True,
        "sms": False,
        "mms": False,
        "verification_required": True,
        "last_verified": "2026-09-25",
        "last_changed_at": "2026-09-25",
        "verification_method": "manual-confirmed",
        "verified_by": "r13i",
        "source_url": "https://x",
    }
    numbers, report = sync.merge([hand], [], "2026-09-28", "https://src", "sync")
    assert numbers == [hand] and report["vanished"] == []
    assert report["kept"] == [
        "PT:national: carrier no longer lists it; kept r13i 2026-09-25"
    ]


def test_twilio_flags_come_from_the_filtered_search_not_the_sample():
    """Puerto Rico local: the five sampled numbers had no MMS, the search
    filtered on MMS found one. The catalog must say MMS yes, every run."""
    countries = [{"country_code": "PR", "country": "Puerto Rico"}]
    pricing = {
        "PR": {
            "phone_number_prices": [{"number_type": "local", "current_price": "3.25"}]
        }
    }
    sample = [
        {"capabilities": {"voice": True, "SMS": True, "MMS": False}} for _ in range(5)
    ]
    args = (countries, {"PR": ["local"]}, pricing, {("PR", "local"): sample}, set())
    rows, skipped = sync.map_twilio(
        *args, {"PR": "1"}, {("PR", "local"): {"voice": True, "sms": True, "mms": True}}
    )
    assert rows[0]["mms"] is True and rows[0]["sms"] is True and not skipped
    # No answer for the pair: the row is skipped (left as it was), not guessed.
    rows, skipped = sync.map_twilio(*args, {"PR": "1"}, {})
    assert rows == [] and skipped == ["PR:local: capabilities not checked"]


def test_telnyx_flags_come_from_coverage_plus_what_is_on_sale():
    lt = {"code": "LT", "mobile": {"features": ["sms", "voice", "fax"]}}
    voice_only = [{"features": [{"name": "voice"}]}]
    # Coverage declares SMS; this week's sample happened to show none.
    assert sync.telnyx_capabilities(lt, "mobile", voice_only) == {
        "voice": True,
        "sms": True,
        "mms": False,
    }
    # Coverage declares nothing for the type; numbers on sale have SMS and MMS.
    bm = {"code": "BM", "toll_free": {}}
    us_pool = [{"features": [{"name": "voice"}, {"name": "sms"}, {"name": "mms"}]}]
    assert sync.telnyx_capabilities(bm, "toll_free", us_pool) == {
        "voice": True,
        "sms": True,
        "mms": True,
    }


def test_fetch_telnyx_makes_no_extra_searches():
    number = {
        "cost_information": {
            "monthly_cost": "1.00000",
            "upfront_cost": "0",
            "currency": "USD",
        },
        "features": [{"name": "voice"}],
    }
    calls = []

    class FakeHttp:
        def get(self, url, params=None, **kw):
            calls.append(url.rsplit("/", 1)[1])
            if url.endswith("/country_coverage"):
                return {
                    "data": {
                        "Lithuania": {
                            "code": "LT",
                            "numbers": True,
                            "phone_number_type": ["mobile"],
                            "mobile": {"features": ["sms", "voice"]},
                        }
                    }
                }
            if url.endswith("/requirements"):
                return {"data": []}
            return {"data": [number]}

    rows, skipped = sync.fetch_telnyx(FakeHttp(), {"LT": "370"})
    assert calls == ["country_coverage", "available_phone_numbers", "requirements"]
    assert rows[0]["voice"] and rows[0]["sms"] and rows[0]["mms"] is False
    assert not skipped


def test_didww_out_of_stock_is_not_gone():
    """Colombia mobile: DIDWW lists the group with a price but has no numbers
    today (is_available false). The row must be left as it was."""
    countries = [
        {"id": "c1", "attributes": {"iso": "CO", "name": "Colombia", "prefix": "57"}}
    ]
    types = {"t-mobile": "Mobile", "t-local": "Local"}

    def group(gid, tid, available):
        return {
            "id": gid,
            "attributes": {"features": ["voice_in", "voice_out"]},
            "meta": {"is_available": available, "needs_registration": False},
            "relationships": {
                "did_group_type": {"data": {"type": "did_group_types", "id": tid}},
                "stock_keeping_units": {
                    "data": [{"type": "stock_keeping_units", "id": f"sku-{gid}"}]
                },
            },
        }

    def sku(gid, price):
        return {
            "type": "stock_keeping_units",
            "id": f"sku-{gid}",
            "attributes": {
                "monthly_price": price,
                "setup_price": "0.0",
                "channels_included_count": 0,
            },
        }

    payload = {
        "data": [group("g1", "t-mobile", False), group("g2", "t-local", True)],
        "included": [sku("g1", "15.0"), sku("g2", "15.0")],
    }
    rows, skipped = sync.map_didww(countries, types, {"CO": payload})
    assert [r["number_type"] for r in rows] == ["local"]
    assert skipped == ["CO:mobile: listed, no numbers in stock"]
    assert sync.skipped_keys(skipped) == {"CO:mobile"}
    previous = {
        "country_code": "CO",
        "number_type": "mobile",
        "dial_code": "57",
        "usd_per_month": "15.00",
        "voice": True,
        "sms": False,
        "mms": False,
        "verification_required": False,
        "available": True,
        "last_verified": "2026-09-26",
        "verification_method": "carrier-sync",
        "verified_by": "didww-api-sync",
    }
    numbers, report = sync.merge(
        [previous],
        rows,
        "2026-09-28",
        "https://src",
        "didww-api-sync",
        unobserved=sync.skipped_keys(skipped),
    )
    by = {n["number_type"]: n for n in numbers}
    assert by["mobile"] == previous and report["vanished"] == []
    assert report["unobserved"] == ["CO:mobile"]


def test_twilio_marks_a_country_it_no_longer_prices_as_gone():
    """Bulgaria sat in the catalog at $3.00 since July while Twilio sold no
    Bulgarian number: absent from the account listing, and the Pricing API
    answers with no prices at all. That country is observed gone. A country
    the Pricing API still prices (Uganda) stays unobserved: the account may
    only lack it."""
    countries = [{"country_code": "GB", "country": "United Kingdom"}]
    types = {"GB": ["local"]}
    assert sync.twilio_observed_keys(countries, types, unsold={"BG"}) == {
        "GB:local",
        "BG:local",
        "BG:mobile",
        "BG:toll_free",
        "BG:national",
    }

    calls: list[str] = []

    class FakeHttp:
        def get(self, url, **kw):
            calls.append(url)
            if url.endswith("/AvailablePhoneNumbers.json"):
                return {"countries": countries}
            if url.endswith("/AvailablePhoneNumbers/GB.json"):
                return {"subresource_uris": {"local": "x"}}
            if url.endswith("/Countries/GB"):
                return {
                    "phone_number_prices": [
                        {"number_type": "local", "current_price": "1.15"}
                    ]
                }
            if url.endswith("/Countries/BG"):
                return {
                    "country": None,
                    "phone_number_prices": None,
                    "price_unit": None,
                }
            if url.endswith("/Countries/UG"):
                return {
                    "phone_number_prices": [
                        {"number_type": "local", "current_price": "40.00"}
                    ]
                }
            if "/AvailablePhoneNumbers/GB/Local.json" in url:
                return {"available_phone_numbers": []}
            if "RegulatoryCompliance/Regulations" in url:
                return {"results": [], "meta": {}}
            raise AssertionError(url)

    rows, skipped, observed = sync.fetch_twilio(
        FakeHttp(), "AC1", {"GB": "44"}, catalog_countries={"GB", "BG", "UG"}
    )
    assert rows == [] and skipped == ["GB:local: no numbers offered to this account"]
    assert observed == {
        "GB:local",
        "BG:local",
        "BG:mobile",
        "BG:toll_free",
        "BG:national",
    }
    # One pricing lookup per catalog country outside the listing, none twice.
    assert [u for u in calls if u.endswith("/Countries/BG")] == [
        "https://pricing.twilio.com/v1/PhoneNumbers/Countries/BG"
    ]
    assert sum(u.endswith("/Countries/UG") for u in calls) == 1
    existing = [
        {"country_code": "BG", "number_type": "local"},
        {"country_code": "UG", "number_type": "local"},
    ]
    assert sync.unobserved_keys(existing, skipped, observed) == {"GB:local", "UG:local"}


def test_twilio_pricing_probe_4xx_leaves_the_country_unobserved():
    """A 4xx from the Pricing API for one unlisted catalog country must not
    abort the Twilio sync, and says nothing about that country."""
    import requests

    class FakeHttp:
        def get(self, url, **kw):
            if url.endswith("/AvailablePhoneNumbers.json"):
                return {"countries": []}
            if url.endswith("/Countries/XK"):
                raise requests.HTTPError("404 Client Error")
            if url.endswith("/Countries/BG"):
                return {"phone_number_prices": None}
            if "RegulatoryCompliance/Regulations" in url:
                return {"results": [], "meta": {}}
            raise AssertionError(url)

    rows, skipped, observed = sync.fetch_twilio(
        FakeHttp(), "AC1", {}, catalog_countries={"XK", "BG"}
    )
    assert rows == [] and skipped == []
    assert observed == {f"BG:{t}" for t in sync.NUMBER_TYPES}


def test_sync_preserves_capability_fees_as_separate_source(tmp_path):
    path = tmp_path / "telnyx.json"
    fees = [
        {
            "capabilities": ["sms", "mms"],
            "usd_per_month": "0.10",
            "source_url": "https://telnyx.com/pricing/numbers",
            "last_verified": "2026-11-02",
            "effective_from": "2026-11-02",
        }
    ]
    path.write_text(json.dumps({"capability_monthly_fees": fees}))
    assert sync.write_catalog(path, "telnyx", [])["capability_monthly_fees"] == fees


# -- requirements block ---------------------------------------------------------


def _req(subject, *, docs=(), fields=(), provider_rules=None, address=False):
    from hailhq.core.providers.verification import (
        DocumentOption,
        DocumentSlot,
        FieldSpec,
        VerificationRequirements,
    )

    return VerificationRequirements(
        country_code="DE",
        number_type="local",
        subject_type=subject,
        required=True,
        fields=tuple(FieldSpec(name=n, label=n) for n in fields),
        documents=tuple(
            DocumentSlot(name=n, label=n, options=(DocumentOption(key="a", label="a"),))
            for n in docs
        ),
        address_required=address,
    )


class _FakeProvider:
    """Twilio-style plug-in: ``requirements`` per subject."""

    def __init__(self, by_subject, calls=None):
        self.by_subject = by_subject
        self.calls = calls if calls is not None else []

    async def requirements(self, cc, nt, subject):
        from hailhq.core.providers.verification import UnsupportedSubjectType

        self.calls.append((cc, nt, subject))
        value = self.by_subject[subject]
        if value is None:
            raise UnsupportedSubjectType("no", allowed=())
        return value


def _use(monkeypatch, provider):
    from hailhq.core.providers import verification

    monkeypatch.setattr(
        verification,
        "get_verification_provider",
        lambda name, purpose="number": provider,
    )


def _row_needing(cc="DE", nt="local", verify=True):
    return {
        "country_code": cc,
        "number_type": nt,
        "usd_per_month": "1.00",
        "verification_required": verify,
    }


def test_requirements_written_for_verified_rows(monkeypatch):
    fake = _FakeProvider(
        {
            "person": _req("person", docs=("proof_of_identity",)),
            "business": _req("business", docs=("business_address",)),
        }
    )
    _use(monkeypatch, fake)
    row = _row_needing()
    sync.attach_requirements("twilio", [row], [], [])
    assert row["requirements"] == {
        "person": {"accepted": True, "items": ["id_document"]},
        "business": {"accepted": True, "items": ["proof_of_address"]},
    }


def test_unsupported_subject_is_not_accepted(monkeypatch):
    fake = _FakeProvider(
        {"person": None, "business": _req("business", docs=("business_address",))}
    )
    _use(monkeypatch, fake)
    block = sync.fetch_requirements("twilio", "DE", "local")
    assert block["person"] == {"accepted": False, "items": []}
    assert block["business"]["accepted"] is True


def test_no_requirements_for_unverified_rows(monkeypatch):
    fake = _FakeProvider({})
    _use(monkeypatch, fake)
    row = _row_needing(verify=False)
    sync.attach_requirements("twilio", [row], [], [])
    assert row["requirements"] is None
    assert fake.calls == []


def test_carrier_error_keeps_last_good_block(monkeypatch):
    class Boom:
        async def requirements(self, *a):
            raise RuntimeError("carrier down")

    _use(monkeypatch, Boom())
    good = {
        "person": {"accepted": True, "items": ["id_document"]},
        "business": {"accepted": False, "items": []},
    }
    existing = [{**_row_needing(), "requirements": good}]
    row, row2 = _row_needing(), _row_needing("FR")
    report: list[str] = []
    sync.attach_requirements("twilio", [row, row2], existing, report)
    assert row["requirements"] == good
    assert row2["requirements"] is None  # nothing stored before
    assert len(report) == 2 and "DE:local" in report[0]


def test_missing_verification_provider_fails_loudly(monkeypatch):
    _use(monkeypatch, None)
    with pytest.raises(RuntimeError, match="twilio"):
        sync.attach_requirements("twilio", [_row_needing()], [], [])
    # No row needs verification: nothing to fetch, no error.
    sync.attach_requirements("twilio", [_row_needing(verify=False)], [], [])


def test_didww_union_of_groups(monkeypatch):
    class Didww:
        async def requirements_per_group(self, cc, nt, subject):
            if subject == "person":
                return [
                    _req("person", docs=("identity_proof_1",)),
                    _req(
                        "person",
                        docs=("identity_proof_1", "address_proof_1"),
                        fields=("birth_date",),
                    ),
                ]
            return [_req("business")]

    _use(monkeypatch, Didww())
    block = sync.fetch_requirements("didww", "DE", "local")
    assert block["person"] == {
        "accepted": True,
        "items": ["id_document", "birth_date", "proof_of_address"],
    }
    assert block["business"] == {"accepted": True, "items": []}


def test_telnyx_passes_raw_rules_to_classify(monkeypatch):
    class Telnyx:
        async def rules(self, cc, nt):
            return [{"id": "x", "field_type": "address", "name": "Local address"}]

        def requirements_from_rules(self, cc, nt, subject, rules):
            return _req(subject, address=True)

    _use(monkeypatch, Telnyx())
    block = sync.fetch_requirements("telnyx", "DE", "local")
    assert block["person"]["items"] == ["address_in_country"]


def test_schema_accepts_requirements():
    import jsonschema

    schema = json.loads(
        (Path(__file__).parents[2] / "costs/schema/numbers.schema.json").read_text()
    )
    base = json.loads((Path(__file__).parents[2] / "costs/telnyx.json").read_text())
    row = dict(base["numbers"][0])
    block = {
        "person": {"accepted": True, "items": ["id_document", {"other": "Odd paper"}]},
        "business": {"accepted": False, "items": []},
    }
    for value in (block, None):
        doc = {**base, "numbers": [{**row, "requirements": value}]}
        jsonschema.validate(doc, schema)
    bad = {**base, "numbers": [{**row, "requirements": {"person": {"accepted": True}}}]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_telnyx_rules_fetched_once_per_row(monkeypatch):
    class Telnyx:
        def __init__(self):
            self.rule_calls = 0
            self.from_rules_calls = []

        async def rules(self, cc, nt):
            self.rule_calls += 1
            return [{"id": "x", "field_type": "address", "name": "Local address"}]

        def requirements_from_rules(self, cc, nt, subject, rules):
            self.from_rules_calls.append(subject)
            return _req(subject, address=True)

    fake = Telnyx()
    _use(monkeypatch, fake)
    block = sync.fetch_requirements("telnyx", "DE", "local")
    assert block["business"]["items"] == ["address_in_country"]
    assert fake.rule_calls == 1
    assert fake.from_rules_calls == ["person", "business"]


def _manual_prev(**extra):
    return {
        "country_code": "DE",
        "number_type": "local",
        "usd_per_month": "9.00",
        "voice": True,
        "sms": False,
        "mms": False,
        "verification_required": True,
        "dial_code": "49",
        "verification_method": "manual-confirmed",
        "verified_by": "me",
        "last_verified": "2026-01-01",
        "last_changed_at": "2026-01-01",
        **extra,
    }


def test_manual_confirmed_row_takes_fresh_requirements():
    old = {"person": {"accepted": True, "items": ["id_document"]}}
    new = {"person": {"accepted": True, "items": ["birth_date"]}}
    prev = _manual_prev(requirements=old)
    fetched = {**prev, "usd_per_month": "5.00", "requirements": new}
    numbers, report = sync.merge([prev], [fetched], "2026-11-03", "u", "bot")
    (out,) = numbers
    assert out["requirements"] == new
    assert out["usd_per_month"] == "9.00" and out["verified_by"] == "me"
    assert not any("requirements" in line for line in report["kept"])
    assert any("requirements" in line for line in report["changed"])
    assert out["last_changed_at"] == "2026-11-03"


def test_requirements_change_is_reported_and_bumps_last_changed():
    old = {"person": {"accepted": True, "items": ["id_document"]}}
    new = {"person": {"accepted": True, "items": ["birth_date"]}}
    prev = {
        **_manual_prev(requirements=old),
        "verification_method": "carrier-sync",
    }
    fetched = {**prev, "requirements": new}
    numbers, report = sync.merge([prev], [fetched], "2026-11-03", "u", "bot")
    assert numbers[0]["last_changed_at"] == "2026-11-03"
    assert any("requirements" in line for line in report["changed"])


def test_didww_no_groups_keeps_last_good_block(monkeypatch):
    class Didww:
        async def requirements_per_group(self, cc, nt, subject):
            return []

    _use(monkeypatch, Didww())
    good = {
        "person": {"accepted": True, "items": ["id_document"]},
        "business": {"accepted": True, "items": []},
    }
    existing = [{**_row_needing(), "requirements": good}]
    row = _row_needing()
    report: list[str] = []
    sync.attach_requirements("didww", [row], existing, report)
    assert row["requirements"] == good
    assert len(report) == 1 and "DE:local" in report[0]


def test_manual_confirmed_row_keeps_block_when_new_block_is_none():
    old = {"person": {"accepted": True, "items": ["id_document"]}}
    prev = _manual_prev(requirements=old)
    fetched = {**prev, "verification_required": False, "requirements": None}
    numbers, report = sync.merge([prev], [fetched], "2026-11-03", "u", "bot")
    assert numbers[0]["requirements"] == old
    assert numbers[0]["last_changed_at"] == "2026-01-01"
    assert not any("requirements" in line for line in report["changed"])


def test_error_detail_hides_carrier_text():
    class TwilioRestException(Exception):
        status = 401

    exc = TwilioRestException(
        "HTTP 401 https://api.twilio.com/2010-04-01/Accounts/ACabc123/x.json?t=1"
    )
    detail = sync._error_detail(exc)
    assert detail == "TwilioRestException (HTTP 401)"
    assert "ACabc123" not in detail


def test_error_detail_ignores_status_on_other_classes():
    class Other(Exception):
        status = 401

    assert sync._error_detail(Other("x")) == "Other"


def test_error_detail_httpx_status():
    import httpx

    req = httpx.Request("GET", "https://api.example.com/v2/x?key=SECRET")
    exc = httpx.HTTPStatusError(
        "boom SECRET", request=req, response=httpx.Response(503, request=req)
    )
    assert sync._error_detail(exc) == "HTTPStatusError (HTTP 503)"


def test_error_detail_plain_exception_is_class_name_only():
    assert sync._error_detail(RuntimeError("secret https://x?y=1")) == "RuntimeError"


def test_error_detail_own_message_is_shown():
    exc = sync.RequirementsUnavailable("DIDWW lists no requirement group for DE local")
    assert sync._error_detail(exc) == "DIDWW lists no requirement group for DE local"


def test_error_report_line_has_no_carrier_text(monkeypatch):
    class Boom:
        async def requirements(self, *a):
            raise RuntimeError("secret https://x?y=1")

    _use(monkeypatch, Boom())
    report: list[str] = []
    sync.attach_requirements("twilio", [_row_needing()], [], report)
    assert report == ["DE:local: requirements failed (RuntimeError), no block stored"]


def test_manual_row_without_new_block_is_marked_in_kept_line():
    old = {"person": {"accepted": True, "items": ["id_document"]}}
    prev = _manual_prev(requirements=old)
    fetched = {**prev, "verification_required": False, "requirements": None}
    _, report = sync.merge([prev], [fetched], "2026-11-03", "u", "bot")
    assert len(report["kept"]) == 1
    assert report["kept"][0].endswith("(hand-verified requirements block kept)")
    assert "requirements_kept" not in report


def test_manual_row_requirements_change_line_is_short():
    old = {
        "person": {"accepted": True, "items": ["id_document"]},
        "business": {"accepted": True, "items": ["company_name"]},
    }
    new = {
        "person": {"accepted": False, "items": []},
        "business": {
            "accepted": True,
            "items": ["company_name", {"other": "Odd paper"}],
        },
    }
    prev = _manual_prev(requirements=old)
    _, report = sync.merge(
        [prev], [{**prev, "requirements": new}], "2026-11-03", "u", "bot"
    )
    assert report["changed"] == [
        (
            "DE:local (hand-verified row, only requirements refreshed): "
            "person id_document | business company_name -> "
            "person not allowed | business company_name; other: Odd paper"
        )
    ]


def _http_503():
    import httpx

    req = httpx.Request("GET", "https://x.example/a?k=SECRET")
    return httpx.HTTPStatusError(
        "m", request=req, response=httpx.Response(503, request=req)
    )


def test_error_detail_follows_cause_and_implicit_context():
    try:
        try:
            raise _http_503()
        except Exception as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert sync._error_detail(outer) == "RuntimeError (HTTP 503)"
    try:
        try:
            raise _http_503()
        except Exception:
            raise RuntimeError("wrapped")
    except RuntimeError as outer:
        assert sync._error_detail(outer) == "RuntimeError (HTTP 503)"


def test_error_detail_skips_suppressed_context_and_survives_cycles():
    try:
        try:
            raise _http_503()
        except Exception:
            raise RuntimeError("wrapped") from None
    except RuntimeError as outer:
        assert sync._error_detail(outer) == "RuntimeError"
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert sync._error_detail(a) == "RuntimeError"


def test_block_summary_odd_items_and_extra_keys():
    block = {
        "person": {"accepted": True, "items": ["id_document", 7, {"other": "A, b"}]},
        "business": {"accepted": False, "items": []},
        "secret": "do not print",
    }
    out = sync._block_summary(block)
    assert "person id_document; other; other: A, b" in out
    assert "business not allowed" in out
    assert "also keys secret" in out and "do not print" not in out


def test_dropped_suffix_only_when_verification_flag_changed():
    old = {"person": {"accepted": True, "items": ["id_document"]}}
    prev = _manual_prev(requirements=old, usd_per_month="9.00")
    same = {**prev, "usd_per_month": "5.00", "requirements": None}
    _, report = sync.merge([prev], [same], "2026-11-03", "u", "bot")
    assert (
        report["kept"]
        and "hand-verified requirements block kept" not in report["kept"][0]
    )


def test_failure_is_logged_in_full_to_stderr(monkeypatch, capsys):
    class Boom:
        async def requirements(self, *a):
            raise RuntimeError("secret detail")

    _use(monkeypatch, Boom())
    report: list[str] = []
    sync.attach_requirements("twilio", [_row_needing()], [], report)
    err = capsys.readouterr().err
    assert "twilio DE:local" in err and "RuntimeError" in err
    assert "secret detail" not in err and "secret detail" not in report[0]


def test_section_caps_lines():
    out: list[str] = []
    sync._section(out, "X", [str(i) for i in range(62)])
    assert out[0] == "### X (62)" and out[-1] == "- … 2 more" and len(out) == 62
    sync._section(out, "Y", [])
    assert len(out) == 62


def test_row_sections_are_not_capped():
    lines = [f"CC{i}:local: changed" for i in range(70)]
    report = {
        "kept": [],
        "changed": lines,
        "added": [],
        "vanished": [],
        "unobserved": [],
    }
    out: list[str] = []
    sync._row_sections(out, report)
    assert out[0] == "### Changed (70)" and len(out) == 71
    assert not any("more" in line for line in out)


def test_carrier_row_requirements_change_is_short():
    new = {
        "person": {"accepted": True, "items": ["id_document"]},
        "business": {"accepted": False, "items": []},
    }
    prev = {
        **_manual_prev(),
        "verification_method": "carrier-sync",
        "available": True,
    }
    _, report = sync.merge(
        [prev], [{**prev, "requirements": new}], "2026-11-03", "u", "bot"
    )
    assert report["changed"] == [
        "DE:local: requirements none -> person id_document | business not allowed"
    ]


def test_summary_is_cut_under_the_pr_body_limit():
    block = {
        "person": {"accepted": True, "items": ["id_document", "birth_date"]},
        "business": {"accepted": True, "items": ["company_name"]},
    }
    prevs = [
        {
            **_manual_prev(),
            "country_code": f"{a}{b}",
            "verification_method": "carrier-sync",
        }
        for a in "ABCDEFGHIJ"
        for b in "ABCDEFGHIJ"
    ][:100]
    fetched = [{**p, "requirements": block, "usd_per_month": "1.00"} for p in prevs]
    _, report = sync.merge(prevs, fetched, "2026-11-03", "u", "bot")
    out: list[str] = []
    sync._row_sections(out, report)
    full = "\n".join(out) + "\n"
    assert len(full) < sync.SUMMARY_LIMIT  # 100 short lines fit
    big = "\n".join(f"- line {i} " + "x" * 200 for i in range(500)) + "\n"
    cut = sync._cap_summary(big)
    assert len(cut) <= sync.SUMMARY_LIMIT + len(sync.SUMMARY_CUT) + 1
    assert cut.endswith(sync.SUMMARY_CUT)
    body = cut[: -len(sync.SUMMARY_CUT)].rstrip("\n")
    assert body.splitlines()[-1].endswith("x" * 200)  # last line is whole
    assert sync._cap_summary("short\n") == "short\n"


def test_carrier_without_verification_does_not_stop_the_run(tmp_path, monkeypatch):
    for p in sync.PROVIDERS:
        (tmp_path / f"{p}.json").write_text("old")
    summary_file = tmp_path / "summary.md"

    def fake_run(provider, config, costs_dir, today, summary, *args, **kwargs):
        if provider == "telnyx":
            raise sync.VerificationUnavailable("no plug-in")
        (costs_dir / f"{provider}.json").write_text("new")
        summary.append(f"## {provider}: 1 rows, 1 countries")
        return True

    monkeypatch.setattr(sync, "run_provider", fake_run)
    code = sync.main(
        ["--costs-dir", str(tmp_path), "--summary-file", str(summary_file)]
    )
    assert code == 0
    assert (tmp_path / "telnyx.json").read_text() == "old"
    assert (tmp_path / "didww.json").read_text() == "new"
    assert (tmp_path / "twilio.json").read_text() == "new"
    text = summary_file.read_text()
    assert (
        "- telnyx: failed, verification is not configured "
        "(check the carrier credentials) (catalog left unchanged)"
    ) in text
    assert "## Carriers not synced" in text
    assert text.index("## Carriers not synced") < text.index("## didww")
    assert "## telnyx" not in text


def test_carrier_skipped_for_missing_credentials_is_listed_with_failures(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TELNYX_API_KEY", raising=False)
    summary_file = tmp_path / "summary.md"
    code = sync.main(
        [
            "--provider",
            "telnyx",
            "--costs-dir",
            str(tmp_path),
            "--summary-file",
            str(summary_file),
        ]
    )
    text = summary_file.read_text()
    assert code == 1
    assert "## Carriers not synced\n- telnyx: skipped, TELNYX_API_KEY not set" in text
    assert text.count("telnyx: skipped, TELNYX_API_KEY not set") == 1


def _kept_prev(verify=True, **extra):
    return {
        "country_code": "CH",
        "number_type": "local",
        "usd_per_month": "5.00",
        "voice": True,
        "sms": False,
        "mms": False,
        "verification_required": verify,
        "dial_code": "41",
        "available": True,
        "verification_method": "carrier-sync",
        "verified_by": "telnyx-api-sync",
        "last_verified": "2026-10-01",
        "last_changed_at": "2026-10-01",
        **extra,
    }


def _kept_provider():
    return _FakeProvider(
        {
            "person": _req("person", docs=("proof_of_identity",)),
            "business": _req("business", docs=("business_address",)),
        }
    )


def test_unobserved_row_needing_verification_gets_a_block(monkeypatch):
    _use(monkeypatch, _kept_provider())
    prev = _kept_prev()
    errors: list[str] = []
    blocks = sync.fetch_kept_requirements("twilio", [prev], set(), errors)
    numbers, report = sync.merge(
        [prev], [], "2026-11-04", "https://s", "x", {"CH:local"}, blocks
    )
    assert numbers[0]["requirements"] == blocks["CH:local"]
    assert {k: v for k, v in numbers[0].items() if k != "requirements"} == {
        **prev,
        "last_changed_at": "2026-11-04",
    }
    assert report["unobserved"] == ["CH:local"] and len(report["changed"]) == 1
    assert errors == []


def test_no_longer_offered_row_gets_a_block(monkeypatch):
    _use(monkeypatch, _kept_provider())
    prev = _kept_prev()
    blocks = sync.fetch_kept_requirements("twilio", [prev], set(), [])
    numbers, report = sync.merge(
        [prev], [], "2026-11-04", "https://s", "x", set(), blocks
    )
    assert numbers[0]["requirements"] == blocks["CH:local"]
    assert numbers[0]["available"] is False
    assert report["vanished"] == ["CH:local"] and len(report["changed"]) == 1


def test_fetch_failure_on_kept_row_keeps_old_block(monkeypatch):
    class Boom:
        async def requirements(self, *a):
            raise RuntimeError("carrier down")

    _use(monkeypatch, Boom())
    old = {"person": {"accepted": True, "items": ["id_document"]}}
    prev = _kept_prev(requirements=old)
    errors: list[str] = []
    blocks = sync.fetch_kept_requirements("twilio", [prev], set(), errors)
    assert blocks == {} and len(errors) == 1 and "CH:local" in errors[0]
    numbers, report = sync.merge(
        [prev], [], "2026-11-04", "https://s", "x", {"CH:local"}, blocks
    )
    assert numbers == [prev] and report["changed"] == []


def test_kept_row_without_verification_gets_no_block(monkeypatch):
    _use(monkeypatch, None)  # would raise if anything were fetched
    prev = _kept_prev(verify=False)
    blocks = sync.fetch_kept_requirements("twilio", [prev], set(), [])
    assert blocks == {}
    numbers, _ = sync.merge(
        [prev], [], "2026-11-04", "https://s", "x", {"CH:local"}, blocks
    )
    assert numbers == [prev] and "requirements" not in numbers[0]
