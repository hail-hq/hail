"""Tests for scripts/costs/sync_voice.py (no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import jsonschema
import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("sync_voice", HERE / "sync_voice.py")
sv = importlib.util.module_from_spec(spec)
sys.modules["sync_voice"] = sv
spec.loader.exec_module(sv)

SCHEMA = json.loads(
    (HERE.parents[1] / "costs" / "schema" / "voice.schema.json").read_text()
)

SPAIN = {
    "country": "Spain",
    "iso_country": "ES",
    "originating_call_prices": [
        {"number_type": "local", "base_price": None, "current_price": "0.006"},
        {"number_type": "tollfree", "base_price": None, "current_price": "0.326"},
    ],
    "terminating_prefix_prices": [
        {
            "friendly_name": "Trunking Outbound Minute - Spain",
            "destination_prefixes": ["34"],
            "origination_prefixes": ["ALL"],
            "base_price": None,
            "current_price": "0.01380",
        },
        {
            "friendly_name": "Trunking Outbound Minute - Spain - Mobile",
            "destination_prefixes": ["346", "347"],
            "origination_prefixes": ["ALL"],
            "base_price": None,
            "current_price": "0.176",
        },
        {
            "friendly_name": "Trunking Outbound Minute - Spain - Mobile - From Eea",
            "destination_prefixes": ["346", "347"],
            "origination_prefixes": ["33", "34"],
            "base_price": None,
            "current_price": "0.0446",
        },
    ],
}


def rows(answer=SPAIN, today="2026-11-07"):
    return sv.map_twilio(answer, today)


@pytest.mark.parametrize(
    ("name", "line_type"),
    [
        ("Trunking Outbound Minute - Spain", "landline"),
        ("Trunking Outbound Minute - Spain - From Eea", "landline"),
        ("Trunking Outbound Minute - Spain - Mobile - From Eea", "mobile"),
        ("Trunking Outbound Minute - France - Mobile Other", "mobile"),
        ("Trunking Outbound Minute - Antigua & Barbuda - Mobile", "mobile"),
        ("Trunking Outbound Minute - Antigua & Barbuda", "landline"),
        ("Trunking Outbound Minute - United States - Toll Free", "toll_free"),
        ("Trunking Outbound Minute - Spain - Special Services", "special"),
        ("Trunking Outbound Minute - Belgium - Premium Numbers", "special"),
        ("Trunking Outbound Minute - Finland - Helsinki - From Us/ca", "other"),
        ("Trunking Outbound Minute - Switzerland - Other", "other"),
    ],
)
def test_line_type(name, line_type):
    assert sv.twilio_line_type(name) == line_type


def test_map_twilio_rows_match_schema():
    out = rows()
    jsonschema.validate(
        {
            "version": 1,
            "license": "CC-BY-4.0",
            "rates": [{**r, "last_changed_at": "2026-11-07"} for r in out],
        },
        SCHEMA,
    )
    inbound = [r for r in out if r["direction"] == "inbound"]
    assert [(r["number_type"], r["usd_per_minute"]) for r in inbound] == [
        ("local", "0.006"),
        ("toll_free", "0.326"),
    ]
    eea = next(r for r in out if r.get("name", "").endswith("From Eea"))
    assert eea["origination_prefixes"] == ["33", "34"]
    assert eea["usd_per_minute"] == "0.0446"
    landline = next(
        r for r in out if r.get("name") == "Trunking Outbound Minute - Spain"
    )
    assert landline["usd_per_minute"] == "0.0138"
    assert (
        landline["source_url"] == "https://www.twilio.com/en-us/sip-trunking/pricing/es"
    )


def test_map_twilio_skips_rows_without_price():
    answer = {
        **SPAIN,
        "originating_call_prices": [{"number_type": "local", "current_price": None}],
        "terminating_prefix_prices": [
            {**SPAIN["terminating_prefix_prices"][0], "current_price": None}
        ],
    }
    assert rows(answer) == []


def test_merge_keeps_change_date_when_price_is_the_same():
    first, _ = sv.merge([], "twilio", rows(today="2026-11-01"), "2026-11-01")
    second, changes = sv.merge(first, "twilio", rows(today="2026-11-08"), "2026-11-08")
    assert changes == []
    assert {r["last_changed_at"] for r in second} == {"2026-11-01"}
    assert {r["last_verified"] for r in second} == {"2026-11-08"}


def test_merge_reports_price_change_add_and_removal():
    first, _ = sv.merge([], "twilio", rows(today="2026-11-01"), "2026-11-01")
    changed = {
        **SPAIN,
        "terminating_prefix_prices": [
            {**SPAIN["terminating_prefix_prices"][1], "current_price": "0.18"},
            SPAIN["terminating_prefix_prices"][2],
            {
                "friendly_name": "Trunking Outbound Minute - Spain - Special Services",
                "destination_prefixes": ["34901"],
                "origination_prefixes": ["ALL"],
                "current_price": "0.717",
            },
        ],
    }
    merged, changes = sv.merge(
        first, "twilio", rows(changed, "2026-11-08"), "2026-11-08"
    )
    assert (
        "ES Trunking Outbound Minute - Spain - Mobile: $0.176 -> $0.18/min" in changes
    )
    assert (
        "added ES Trunking Outbound Minute - Spain - Special Services: $0.717/min"
        in changes
    )
    assert "removed ES Trunking Outbound Minute - Spain" in changes
    mobile = next(
        r
        for r in merged
        if r.get("name") == "Trunking Outbound Minute - Spain - Mobile"
    )
    assert mobile["last_changed_at"] == "2026-11-08"


def test_merge_keeps_other_carriers_rows():
    other = {
        "provider": "telnyx",
        "country_code": "ES",
        "direction": "inbound",
        "number_type": "local",
        "usd_per_minute": "0.003",
        "last_verified": "2026-10-01",
        "last_changed_at": "2026-10-01",
        "verification_method": "manual-confirmed",
        "verified_by": "r13i",
        "source_url": "https://telnyx.com/pricing/elastic-sip",
    }
    merged, _ = sv.merge([other], "twilio", rows(), "2026-11-07")
    assert other in merged


def test_merge_refuses_a_shrunk_fetch():
    many = []
    for cc in ("ES", "FR", "DE", "IT", "PT"):
        many += rows({**SPAIN, "iso_country": cc})
    existing, _ = sv.merge([], "twilio", many, "2026-11-01")
    with pytest.raises(sv.RatesShrunk):
        sv.merge(existing, "twilio", rows(), "2026-11-08")


def test_dump_puts_prefix_lists_on_one_line():
    merged, _ = sv.merge([], "twilio", rows(), "2026-11-07")
    text = sv.dump(merged)
    assert '"destination_prefixes": ["346", "347"],' in text
    assert json.loads(text)["rates"] == sv.sort_rows(merged)


def test_main_writes_twilio_and_reports_the_others(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "fetch_twilio", lambda http, today: rows(today=today))
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    target = tmp_path / "voice.json"
    summary = tmp_path / "summary.md"
    assert sv.main(["--file", str(target), "--summary-file", str(summary)]) == 0
    data = json.loads(target.read_text())
    jsonschema.validate(data, SCHEMA)
    text = summary.read_text()
    assert "- telnyx: no rate API known yet" in text
    assert "- didww: no rate API known yet" in text
    assert "## twilio" in text


def test_main_without_credentials_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    target = tmp_path / "voice.json"
    assert sv.main(["--provider", "twilio", "--file", str(target)]) == 1
    assert not target.exists()


def test_committed_voice_json_matches_schema_and_sync_output():
    path = HERE.parents[1] / "costs" / "voice.json"
    jsonschema.validate(json.loads(path.read_text()), SCHEMA)
    assert sv.dump(sv.load(path)) == path.read_text()
