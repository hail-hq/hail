"""Sync the per-carrier number catalogs from each carrier's API.

    uv run python scripts/costs/sync_numbers.py --provider all --env-file .env

One file per carrier: costs/twilio.json, costs/telnyx.json, costs/didww.json.
Each row is one (country, number type): monthly price, what it can do (calls,
texts, MMS) and whether the buyer must be verified first. Read-only: only GET
requests, no purchases, no account changes.

Rules the merge follows, in this order:
- A row someone verified by hand (verification_method "manual-confirmed") is
  never overwritten and never marked unavailable. Disagreements are printed.
- A row that vanished from the carrier is kept with available=false, a note
  and the date. A held number must stay billable; the picker hides it.
- A row DIDWW lists with no numbers in stock takes the price DIDWW shows
  for the empty areas: its check date moves and it gets a note.
- A row the sync could not observe this run (no numbers in stock at Telnyx
  or Twilio, no price, no dial code, or a country the sync account cannot
  see) is left exactly as it was: not seeing stock is not the same as the
  carrier dropping the type. Telnyx shows no price without a number, so
  such a row goes stale until its price is read on telnyx.com/pricing.md.
  Twilio only: a catalog country outside the account listing is asked the
  Pricing API; no prices at all means Twilio sells nothing there through the
  API, and its rows vanish. Per type, too: a type that is neither in the
  listing nor in the country's Pricing API answer is not sold, and its row
  vanishes.
- A carrier without credentials is skipped with a printed reason, not an error.

Calls, texts and MMS flags: a feature a sampled number has is a yes. For a
feature no sampled number has, the carrier is asked directly: Twilio with a
filtered search (VoiceEnabled/SmsEnabled/MmsEnabled), Telnyx through the
features its coverage list declares for the number type. "No" is never read
off the sample: the sample changes from run to run.

Credentials (env or --env-file): TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN,
TELNYX_API_KEY, DIDWW_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
COSTS_DIR = ROOT / "costs"
# DIDWW first: its country list carries dial prefixes the other two borrow.
PROVIDERS = ("didww", "telnyx", "twilio")
NUMBER_TYPES = ("local", "mobile", "toll_free", "national")
TYPE_LABEL = {
    "local": "local",
    "mobile": "mobile",
    "toll_free": "toll-free",
    "national": "national",
}
COUNTRY_FLOOR = 40  # never shrink a catalog below this many countries


class CatalogShrunk(Exception):
    """A fetch returned too few countries; the catalog is left unchanged."""


# -- helpers ------------------------------------------------------------------


def progress(msg: str) -> None:
    """One line per unit of work on stderr, so a long run is visible."""
    print(msg, file=sys.stderr, flush=True)


def money(value: str | float | None) -> str | None:
    """Carrier price -> decimal string without float noise ("2.00000" -> "2.00")."""
    if value is None:
        return None
    d = Decimal(str(value)).normalize()
    text = format(d, "f")
    if "." not in text:
        text += ".00"
    else:
        whole, frac = text.split(".")
        text = f"{whole}.{frac.ljust(2, '0')}"
    return text


class Http:
    """GET with retries. Timeouts, 429 and 5xx back off; anything else raises."""

    def __init__(self, session: requests.Session, rps: float = 8.0) -> None:
        self.session = session
        self.min_gap = 1.0 / rps
        self._last = 0.0

    def get(self, url: str, **kw: Any) -> Any:
        for attempt in range(6):
            gap = self.min_gap - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, timeout=60, **kw)
            except (requests.Timeout, requests.ConnectionError):
                time.sleep(min(2**attempt, 30))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(min(2**attempt, 30))
                continue
            resp.raise_for_status()
            return resp.json()
        # A RequestException so main() reports "catalog left unchanged" and
        # moves on to the next carrier instead of aborting the whole run.
        raise requests.exceptions.RetryError(f"gave up after retries: {url}")


def _row(
    *,
    country_code: str,
    number_type: str,
    country_name: str,
    dial_code: str,
    usd_per_month: str,
    voice: bool,
    sms: bool,
    mms: bool,
    verification_required: bool,
    setup_usd: str | None = None,
    notes: str | None = None,
    receive_only: bool = False,
    by_request: bool = False,
) -> dict:
    row = {
        "country_code": country_code,
        "number_type": number_type,
        "display_name": f"{country_name} {TYPE_LABEL[number_type]}",
        "dial_code": dial_code,
        "usd_per_month": usd_per_month,
        "voice": voice,
        "sms": sms,
        "mms": mms,
        "verification_required": verification_required,
        "available": True,
    }
    if setup_usd is not None:
        row["setup_usd"] = setup_usd
    if notes:
        row["notes"] = notes
    row["receive_only"] = receive_only
    row["by_request"] = by_request
    return _drop_unset(row)


def _drop_unset(row: dict) -> dict:
    """Remove the optional fields a row writes only when set: a zero setup
    fee, and the receive_only and by_request flags (a row without them is
    sold as usual)."""
    for k, unset in (
        ("setup_usd", "0.00"),
        ("receive_only", False),
        ("by_request", False),
    ):
        if k in row and row[k] in (unset, None):
            del row[k]
    return row


# -- Twilio -------------------------------------------------------------------

TWILIO_TYPE_PATH = {
    "local": "Local",
    "mobile": "Mobile",
    "toll_free": "TollFree",
    "national": "National",
}
TWILIO_FEATURE_FILTER = {
    "voice": "VoiceEnabled",
    "sms": "SmsEnabled",
    "mms": "MmsEnabled",
}
TWILIO_CAPABILITY_KEY = {"voice": "voice", "sms": "SMS", "mms": "MMS"}
TWILIO_PRICING_TYPE = {
    "local": "local",
    "mobile": "mobile",
    "toll free": "toll_free",
    "toll-free": "toll_free",
    "national": "national",
}


def twilio_regulated(regulations: list[dict]) -> set[tuple[str, str]]:
    """(country, type) pairs where any end-user type must hold a bundle."""
    out = set()
    for r in regulations:
        if any(r.get("requirements", {}).values()):
            out.add((r["iso_country"], r["number_type"].replace("-", "_")))
    return out


def map_twilio(
    countries: list[dict],
    types_by_country: dict[str, list[str]],
    pricing_by_country: dict[str, dict],
    samples: dict[tuple[str, str], list[dict]],
    regulated: set[tuple[str, str]],
    dial_codes: dict[str, str],
    capabilities: dict[tuple[str, str], dict[str, bool]],
) -> tuple[list[dict], list[str]]:
    """``capabilities`` holds the carrier's answer to "is any number with
    this feature on sale" per (country, type). The sample only proves stock
    exists and carries the address rule: five numbers without MMS do not
    show that no number has MMS."""
    rows, skipped = [], []
    for c in countries:
        iso, name = c["country_code"], c["country"]
        prices = twilio_prices(pricing_by_country.get(iso))
        for number_type in types_by_country.get(iso, []):
            if number_type not in NUMBER_TYPES:
                skipped.append(f"{iso}:{number_type}: unknown number type")
                continue
            sample = samples.get((iso, number_type), [])
            if not sample:
                skipped.append(
                    f"{iso}:{number_type}: no numbers offered to this account"
                )
                continue
            price = prices.get(number_type)
            if price is None:
                skipped.append(f"{iso}:{number_type}: no price from the Pricing API")
                continue
            caps = capabilities.get((iso, number_type))
            if caps is None:
                skipped.append(f"{iso}:{number_type}: capabilities not checked")
                continue
            address = any(
                n.get("address_requirements", "none") != "none" for n in sample
            )
            rows.append(
                _row(
                    country_code=iso,
                    number_type=number_type,
                    country_name=name,
                    dial_code=dial_codes.get(iso, ""),
                    usd_per_month=money(price),
                    voice=caps["voice"],
                    sms=caps["sms"],
                    mms=caps["mms"],
                    verification_required=address or (iso, number_type) in regulated,
                )
            )
    return rows, skipped


def twilio_prices(answer: dict | None) -> dict[str, str]:
    """Number type to price, from one Pricing API country answer."""
    return {
        TWILIO_PRICING_TYPE.get(p["number_type"], p["number_type"]): p["current_price"]
        for p in (answer or {}).get("phone_number_prices") or []
    }


def twilio_observed_keys(
    countries: list[dict],
    types_by_country: dict[str, list[str]],
    unsold: Iterable[str] = (),
    pricing: dict[str, dict] | None = None,
) -> set[str]:
    """The "ISO:type" pairs this run settled. A key is observed when the
    account's listing enumerates it. Twilio's AvailablePhoneNumbers list is
    scoped to the account's geographic permissions: a country or type it
    leaves out says nothing about what Twilio sells on its own.

    The Pricing API settles the rest. ``unsold`` names catalog countries
    outside the listing for which it has no prices at all: Twilio sells
    nothing there through the API (Hail's only way to buy), so every key of
    theirs counts as observed and, absent from the rows, vanishes.
    ``pricing`` holds the Pricing API answer for each country that has one,
    listed or not. A type that the listing leaves out and the answer does not
    price is not sold either (Brazil national, 2026-11), so it is observed
    gone too. A type the answer still prices stays unobserved: the account
    may only lack it."""
    keys = {
        f"{c['country_code']}:{t}"
        for c in countries
        for t in types_by_country.get(c["country_code"], [])
        if t in NUMBER_TYPES
    }
    keys |= {f"{iso}:{t}" for iso in unsold for t in NUMBER_TYPES}
    for iso, answer in (pricing or {}).items():
        priced = set(twilio_prices(answer))
        if not priced:
            continue
        listed_types = set(types_by_country.get(iso, []))
        keys |= {
            f"{iso}:{t}"
            for t in NUMBER_TYPES
            if t not in listed_types and t not in priced
        }
    return keys


def fetch_twilio(
    http: Http,
    account_sid: str,
    dial_codes: dict[str, str],
    catalog_countries: Iterable[str] = (),
) -> tuple[list[dict], list[str], set[str]]:
    """Rows, skipped lines, and the keys this run settled (see
    ``twilio_observed_keys``).

    ``catalog_countries`` are the countries the catalog on disk has. One
    outside the account listing is asked the Pricing API: no prices at all
    means Twilio sells nothing there through the API (Bulgaria, 2026-10),
    and its rows are observed gone rather than kept at a price nobody can
    buy."""
    base = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/AvailablePhoneNumbers"
    countries = http.get(f"{base}.json")["countries"]
    listed = {c["country_code"] for c in countries}
    unsold: set[str] = set()
    pricing: dict[str, dict] = {}
    for iso in sorted(set(catalog_countries) - listed):
        progress(f"twilio {iso} (not in the account listing)")
        try:
            prices = http.get(
                f"https://pricing.twilio.com/v1/PhoneNumbers/Countries/{iso}"
            )
        except requests.HTTPError:
            # A 4xx for one probe says nothing about the country. The row
            # stays unobserved; the other countries still sync.
            continue
        if not prices.get("phone_number_prices"):
            unsold.add(iso)
        else:
            pricing[iso] = prices
    types_by_country: dict[str, list[str]] = {}
    samples: dict[tuple[str, str], list[dict]] = {}
    capabilities: dict[tuple[str, str], dict[str, bool]] = {}
    for c in countries:
        iso = c["country_code"]
        progress(f"twilio {iso}")
        detail = http.get(f"{base}/{iso}.json")
        types = [t for t in detail.get("subresource_uris", {}) if t in NUMBER_TYPES]
        types_by_country[iso] = types
        pricing[iso] = http.get(
            f"https://pricing.twilio.com/v1/PhoneNumbers/Countries/{iso}"
        )
        for number_type in types:
            page = http.get(
                f"{base}/{iso}/{TWILIO_TYPE_PATH[number_type]}.json",
                params={"PageSize": 5},
            )
            samples[(iso, number_type)] = page.get("available_phone_numbers", [])
            if not samples[(iso, number_type)]:
                continue
            # A feature the sample shows is on sale. One it does not show is
            # asked for with a filtered search: a fixed answer, where reading
            # "no" off a random sample flipped from run to run.
            seen = {
                name
                for n in samples[(iso, number_type)]
                for name, key in TWILIO_CAPABILITY_KEY.items()
                if n.get("capabilities", {}).get(key)
            }
            capabilities[(iso, number_type)] = {
                name: name in seen
                or bool(
                    http.get(
                        f"{base}/{iso}/{TWILIO_TYPE_PATH[number_type]}.json",
                        params={param: "true", "PageSize": 1},
                    ).get("available_phone_numbers")
                )
                for name, param in TWILIO_FEATURE_FILTER.items()
            }
    regulations: list[dict] = []
    url = "https://numbers.twilio.com/v2/RegulatoryCompliance/Regulations?PageSize=1000"
    while url:
        page = http.get(url)
        regulations.extend(page["results"])
        url = page.get("meta", {}).get("next_page_url")
    rows, skipped = map_twilio(
        countries,
        types_by_country,
        pricing,
        samples,
        twilio_regulated(regulations),
        dial_codes,
        capabilities,
    )
    return (
        rows,
        skipped,
        twilio_observed_keys(countries, types_by_country, unsold, pricing),
    )


# -- Telnyx -------------------------------------------------------------------


def telnyx_capabilities(
    country: dict, number_type: str, sample: list[dict]
) -> dict[str, bool]:
    """What this number type can do: what Telnyx's coverage list declares
    for it, plus anything a number on sale has. Coverage is the carrier's
    own statement and does not change with which numbers a search returns;
    the sample adds what coverage leaves out (Caribbean toll-free numbers
    come from the US pool and carry SMS and MMS)."""
    declared = set((country.get(number_type) or {}).get("features", []))
    seen = {f["name"] for n in sample for f in n.get("features", [])}
    return {name: name in declared | seen for name in ("voice", "sms", "mms")}


def map_telnyx(
    coverage: dict[str, dict],
    samples: dict[tuple[str, str], list[dict]],
    requirements: dict[tuple[str, str], list[dict]],
    dial_codes: dict[str, str],
    capabilities: dict[tuple[str, str], dict[str, bool]],
) -> tuple[list[dict], list[str]]:
    """``capabilities``: see map_twilio. The sample gives price and stock."""
    rows, skipped = [], []
    for name, c in coverage.items():
        if not c.get("numbers"):
            continue
        iso = c["code"]
        for number_type in sorted(set(c.get("phone_number_type", []))):
            if number_type not in NUMBER_TYPES:
                continue  # shared_cost and friends are not sold by Hail
            sample = samples.get((iso, number_type), [])
            if not sample:
                skipped.append(f"{iso}:{number_type}: no numbers in stock")
                continue
            monthly = sorted(
                Decimal(n["cost_information"]["monthly_cost"]) for n in sample
            )
            setup = min(
                Decimal(n["cost_information"].get("upfront_cost") or 0) for n in sample
            )
            currency = {n["cost_information"].get("currency", "USD") for n in sample}
            if currency != {"USD"}:
                skipped.append(f"{iso}:{number_type}: non-USD price {currency}")
                continue
            caps = capabilities.get((iso, number_type))
            if caps is None:
                skipped.append(f"{iso}:{number_type}: capabilities not checked")
                continue
            notes = None
            if monthly[0] != monthly[-1]:
                notes = f"monthly price varies by number: {money(monthly[0])} to {money(monthly[-1])} USD in a sample of {len(sample)}"
            rows.append(
                _row(
                    country_code=iso,
                    number_type=number_type,
                    country_name=name,
                    dial_code=dial_codes.get(iso, ""),
                    usd_per_month=money(monthly[0]),
                    setup_usd=money(setup),
                    voice=caps["voice"],
                    sms=caps["sms"],
                    mms=caps["mms"],
                    verification_required=bool(requirements.get((iso, number_type))),
                    notes=notes,
                )
            )
    return rows, skipped


def fetch_telnyx(
    http: Http, dial_codes: dict[str, str]
) -> tuple[list[dict], list[str]]:
    coverage = http.get("https://api.telnyx.com/v2/country_coverage")["data"]
    samples: dict[tuple[str, str], list[dict]] = {}
    capabilities: dict[tuple[str, str], dict[str, bool]] = {}
    requirements: dict[tuple[str, str], list[dict]] = {}
    for c in coverage.values():
        if not c.get("numbers"):
            continue
        iso = c["code"]
        progress(f"telnyx {iso}")
        for number_type in sorted(set(c.get("phone_number_type", []))):
            if number_type not in NUMBER_TYPES:
                continue
            page = http.get(
                "https://api.telnyx.com/v2/available_phone_numbers",
                params={
                    "filter[country_code]": iso,
                    "filter[phone_number_type]": number_type,
                    "filter[limit]": 5,
                },
            )
            samples[(iso, number_type)] = page.get("data", [])
            if samples[(iso, number_type)]:
                capabilities[(iso, number_type)] = telnyx_capabilities(
                    c, number_type, samples[(iso, number_type)]
                )
            req = http.get(
                "https://api.telnyx.com/v2/requirements",
                params={
                    "filter[country_code]": iso,
                    "filter[phone_number_type]": number_type,
                    "filter[action]": "ordering",
                },
            )
            requirements[(iso, number_type)] = req.get("data", [])
    return map_telnyx(coverage, samples, requirements, dial_codes, capabilities)


# -- DIDWW --------------------------------------------------------------------

DIDWW_TYPE = {
    "Local": "local",
    "Mobile": "mobile",
    "National": "national",
    "Toll-free": "toll_free",
}


def _didww_type_fields(all_groups: list[dict]) -> tuple[dict, list[Decimal]]:
    """The row fields for one number type, from its areas. Hail sells a
    number it can show (listed) and that can place calls: the price comes
    from the areas Hail can sell, and the row is marked when there are
    none."""
    features = set().union(*(g["features"] for g in all_groups))
    outbound = [g for g in all_groups if g["outbound"]]
    sellable = [g for g in outbound if g["listed"]]
    groups = sellable or outbound or all_groups
    prices = sorted(g["monthly"] for g in groups)
    cheapest = min(groups, key=lambda g: g["monthly"])
    return {
        "usd_per_month": money(prices[0]),
        "setup_usd": money(cheapest["setup"]),
        "voice": any(f.startswith("voice") for f in features),
        "sms": any(f.startswith("sms") for f in features),
        "verification_required": cheapest["needs_registration"] or cheapest["address"],
        "receive_only": not outbound,
        "by_request": bool(outbound) and not sellable,
    }, prices


def _didww_range_note(prices: list[Decimal]) -> str | None:
    if prices[0] == prices[-1]:
        return None
    return f"monthly price varies by area: {money(prices[0])} to {money(prices[-1])} USD across {len(prices)} areas"


def map_didww(
    countries: list[dict],
    group_types: dict[str, str],
    groups_by_country: dict[str, dict],
) -> tuple[list[dict], list[str], dict[str, dict]]:
    """groups_by_country[iso] = {"data": [...did_groups...], "included": [...]}

    The third value maps each "ISO:type" that DIDWW lists with no numbers in
    stock to the row fields read from its empty areas: DIDWW shows the price
    and features of an area even when it has no numbers."""
    rows, skipped = [], []
    no_stock: dict[str, dict] = {}
    for c in countries:
        iso, name, prefix = (
            c["attributes"]["iso"],
            c["attributes"]["name"],
            c["attributes"]["prefix"],
        )
        payload = groups_by_country.get(iso)
        if not payload:
            continue
        included = {(i["type"], i["id"]): i for i in payload.get("included", [])}
        by_type: dict[str, list[dict]] = defaultdict(list)
        unstocked: dict[str, list[dict]] = defaultdict(list)
        stocked_unpriced: set[str] = set()
        listed: set[str] = set()
        for g in payload.get("data", []):
            gt = g["relationships"].get("did_group_type", {}).get("data") or {}
            number_type = DIDWW_TYPE.get(group_types.get(gt.get("id"), ""))
            if not number_type:
                if not gt.get("id"):
                    skipped.append(f"{iso}: group {g.get('id')} has no resolvable type")
                continue
            listed.add(number_type)
            skus = [
                included[("stock_keeping_units", s["id"])]["attributes"]
                for s in g["relationships"]
                .get("stock_keeping_units", {})
                .get("data", [])
                if ("stock_keeping_units", s["id"]) in included
            ]
            # A SKU without a price is passed over: it must not stop the run.
            priced = [s for s in skus if s.get("monthly_price") is not None]
            base = [
                s for s in priced if s.get("channels_included_count") == 0
            ] or priced
            available = g.get("meta", {}).get("is_available", True)
            if not base:
                if available:
                    stocked_unpriced.add(number_type)
                continue
            cheapest = min(base, key=lambda s: Decimal(s["monthly_price"]))
            features = g["attributes"].get("features", [])
            (by_type if available else unstocked)[number_type].append(
                {
                    "monthly": Decimal(cheapest["monthly_price"]),
                    "setup": Decimal(cheapest.get("setup_price") or 0),
                    "features": set(features),
                    "needs_registration": bool(
                        g.get("meta", {}).get("needs_registration")
                    ),
                    "address": g["relationships"]
                    .get("address_requirement", {})
                    .get("data")
                    is not None,
                    "outbound": "voice_out" in features,
                    # DIDWW lists each number of this area for sale, so a
                    # customer can be shown one and buy it. When false,
                    # DIDWW picks the number at order time.
                    "listed": bool(
                        g.get("meta", {}).get("available_dids_enabled", True)
                    ),
                }
            )
        # DIDWW still lists the type but has nothing to sell right now. Out
        # of stock is not dropped: merge() updates the row from the empty
        # areas, read the same way as areas with stock. A type with no
        # price on any group, or with an area on sale that has no price, is
        # kept exactly as it was.
        for number_type in sorted(listed - set(by_type)):
            if number_type in stocked_unpriced:
                skipped.append(
                    f"{iso}:{number_type}: listed, an area on sale has no price"
                )
                continue
            if not unstocked[number_type]:
                skipped.append(f"{iso}:{number_type}: listed, no price on any group")
                continue
            skipped.append(f"{iso}:{number_type}: listed, no numbers in stock")
            no_stock[f"{iso}:{number_type}"], _ = _didww_type_fields(
                unstocked[number_type]
            )
        for number_type, all_groups in by_type.items():
            fields, prices = _didww_type_fields(all_groups)
            fields["notes"] = _didww_range_note(prices)
            rows.append(
                _row(
                    country_code=iso,
                    number_type=number_type,
                    country_name=name,
                    dial_code=prefix,
                    mms=False,
                    **fields,
                )
            )
    return rows, skipped, no_stock


def fetch_didww(
    http: Http,
) -> tuple[list[dict], list[str], dict[str, str], dict[str, dict]]:
    """One request per (country, number type, page). The address requirement
    is not included: `meta.needs_registration` carries the verification flag
    and the extra include made pages slow enough to time out."""

    def paged(url: str, params: dict) -> tuple[list[dict], list[dict]]:
        # Stop on the API's own "next" link. Some endpoints (countries) ignore
        # page[size] and return everything at once; counting rows would loop.
        data, included, number = [], [], 1
        while True:
            page = http.get(
                url, params={**params, "page[size]": 100, "page[number]": number}
            )
            data.extend(page.get("data", []))
            included.extend(page.get("included", []))
            if not (page.get("links") or {}).get("next"):
                return data, included
            number += 1

    countries, _ = paged("https://api.didww.com/v3/countries", {})
    types_page = http.get("https://api.didww.com/v3/did_group_types")
    group_types = {t["id"]: t["attributes"]["name"] for t in types_page["data"]}
    wanted = [tid for tid, name in group_types.items() if name in DIDWW_TYPE]
    groups_by_country: dict[str, dict] = {}
    for c in countries:
        iso = c["attributes"]["iso"]
        data: list[dict] = []
        included: list[dict] = []
        for tid in wanted:
            progress(f"didww {iso} {group_types[tid]}")
            d, i = paged(
                "https://api.didww.com/v3/did_groups",
                {
                    "filter[country.id]": c["id"],
                    "filter[did_group_type.id]": tid,
                    "include": "stock_keeping_units",
                },
            )
            # Without include=did_group_type the relationship carries only
            # links; the filter already tells us the type, so record it.
            for g in d:
                g.setdefault("relationships", {})["did_group_type"] = {
                    "data": {"type": "did_group_types", "id": tid}
                }
            data.extend(d)
            included.extend(i)
        if data:
            groups_by_country[iso] = {"data": data, "included": included}
    dial_codes = {c["attributes"]["iso"]: c["attributes"]["prefix"] for c in countries}
    rows, skipped, no_stock = map_didww(countries, group_types, groups_by_country)
    return rows, skipped, dial_codes, no_stock


# -- merge + write -------------------------------------------------------------


def unobserved_keys(
    existing: list[dict], skipped: list[str], observed: set[str] | None
) -> set[str]:
    """Keys the sync must leave alone: the ones it skipped with a reason, and,
    when the fetch reports what it could see, every existing key outside that
    view. A key never enumerated was not observed to be gone."""
    keys = skipped_keys(skipped)
    if observed is not None:
        keys |= {
            f"{n['country_code']}:{n['number_type']}"
            for n in existing
            if f"{n['country_code']}:{n['number_type']}" not in observed
        }
    return keys


def skipped_keys(skipped: list[str]) -> set[str]:
    """The "ISO:type" keys in a mapper's skipped lines (each is
    "ISO:type: reason"; DIDWW's per-group lines carry no type and are ignored)."""
    keys = set()
    for line in skipped:
        key = line.split(": ", 1)[0]
        iso, _, number_type = key.partition(":")
        if len(iso) == 2 and number_type in NUMBER_TYPES:
            keys.add(key)
    return keys


def merge(
    existing: list[dict],
    fetched: list[dict],
    today: str,
    source_url: str,
    verified_by: str,
    unobserved: set[str] | frozenset[str] = frozenset(),
    kept_rows: list[dict] | None = None,
    out_of_stock: dict[str, dict] | None = None,
) -> tuple[list[dict], dict]:
    """Overlay fetched rows on the current catalog. Returns (numbers, report).

    ``unobserved`` names the keys the sync skipped this run; their rows are
    kept as they are instead of being marked vanished.

    ``out_of_stock`` maps the unobserved keys DIDWW still lists but has no
    numbers for to the price and flags DIDWW shows for those areas. The
    price was read today, so the row takes it, its check date moves and it
    gets a note. ``available`` does not move: a row on sale stays on sale, a
    vanished row stays hidden. The next sync that sees stock rebuilds the
    row without the note.

    ``kept_rows`` holds copies of unobserved rows whose ``requirements`` were
    refreshed by attach_requirements; only that field is taken from them."""
    fresh_by_key = {
        f"{n['country_code']}:{n['number_type']}": n for n in kept_rows or []
    }

    def refreshed(key: str, row: dict) -> dict:
        how = (
            "hand-verified row"
            if row.get("verification_method") == "manual-confirmed"
            else "not observed"
        )
        fresh = fresh_by_key.get(key)
        if fresh is None or fresh.get("requirements") == row.get("requirements"):
            return row
        report["changed"].append(
            _refreshed_line(key, how, row.get("requirements"), fresh["requirements"])
        )
        return {**row, "requirements": fresh["requirements"], "last_changed_at": today}

    prev_by_key = {f"{n['country_code']}:{n['number_type']}": n for n in existing}
    report: dict[str, list[str]] = {
        "kept": [],
        "changed": [],
        "added": [],
        "vanished": [],
        "no_stock": [],
        "unobserved": [],
    }
    watched = (
        "usd_per_month",
        "voice",
        "sms",
        "mms",
        "verification_required",
        "setup_usd",
        "available",
        "receive_only",
        "by_request",
        "requirements",
    )
    seen = set()
    numbers = []
    for n in sorted(fetched, key=lambda r: (r["country_code"], r["number_type"])):
        key = f"{n['country_code']}:{n['number_type']}"
        seen.add(key)
        prev = prev_by_key.get(key)
        n.setdefault("available", True)
        if not n.get("dial_code") and prev:
            n["dial_code"] = prev["dial_code"]
        if prev and prev.get("verification_method") == "manual-confirmed":
            diff = [
                f"{k}={n.get(k)}"
                for k in watched
                if k != "requirements" and prev.get(k) != n.get(k)
            ]
            fresh = n.get("requirements")
            old_block = prev.get("requirements")
            # A failed fetch copied the previous block into ``n`` in
            # attach_requirements, so None here means the carrier lists no
            # verification for this row. The hand-verified block stays.
            block = fresh if fresh is not None else old_block
            dropped = (
                fresh is None
                and old_block is not None
                and bool(prev.get("verification_required"))
                and not n.get("verification_required")
            )
            if diff:
                line = f"{key}: carrier says {', '.join(diff)}; "
                line += f"kept {prev['verified_by']} {prev['last_verified']}"
                if dropped:
                    line += " (hand-verified requirements block kept)"
                report["kept"].append(line)
            kept = {**prev, "requirements": block}
            if old_block != block:
                report["changed"].append(
                    _refreshed_line(key, "hand-verified row", old_block, block)
                )
                kept["last_changed_at"] = today
            numbers.append(kept)
            continue
        changed = [k for k in watched if (prev or {}).get(k) != n.get(k)]
        if prev is None:
            report["added"].append(
                f"{key}: {n['usd_per_month']} voice={n['voice']} sms={n['sms']} verify={n['verification_required']}"
            )
        elif changed:
            report["changed"].append(
                f"{key}: "
                + ", ".join(_change(k, prev.get(k), n.get(k)) for k in changed)
            )
        numbers.append(
            {
                **n,
                "last_verified": today,
                "last_changed_at": (
                    today if changed else prev.get("last_changed_at", today)
                ),
                "verification_method": "carrier-sync",
                "verified_by": verified_by,
                "source_url": source_url,
            }
        )
    for key, prev in prev_by_key.items():
        if key in seen:
            continue
        if key in unobserved:
            row = refreshed(key, prev)
            prices = (out_of_stock or {}).get(key)
            if prices is None or prev.get("verification_method") == "manual-confirmed":
                report["unobserved"].append(key)
                numbers.append(row)
                continue
            # A row that vanished stays hidden: listed again with nothing to
            # buy is not back on sale. Stock coming back puts it on sale.
            new = _drop_unset({**row, **prices})
            # Papers follow the flag, as attach_requirements does for fetched
            # rows: run_provider fetched a block for a row that needs one.
            if not new.get("verification_required") and new.get("requirements"):
                new["requirements"] = None
            changed = [k for k in watched if row.get(k) != new.get(k)]
            # A hidden row keeps its vanished note: the day it went off sale
            # still holds for numbers already held.
            kept_notes = _own_notes(
                row.get("notes"), keep_vanished=not new.get("available", True)
            )
            new["notes"] = "; ".join([*kept_notes, _NO_STOCK_NOTE])
            new.update(
                last_verified=today,
                verification_method="carrier-sync",
                verified_by=verified_by,
                source_url=source_url,
            )
            if changed:
                new["last_changed_at"] = today
            report["no_stock"].append(
                f"{key}: "
                + ", ".join(_change(k, row.get(k), new.get(k)) for k in changed)
                if changed
                else key
            )
            numbers.append(new)
            continue
        if prev.get("verification_method") == "manual-confirmed":
            # A person vouched for this row; only a person takes it off sale.
            report["kept"].append(
                f"{key}: carrier no longer lists it; kept {prev['verified_by']} {prev['last_verified']}"
            )
            numbers.append(prev)
            continue
        # The carrier was asked today and said no: the row is verified, and
        # the day it went off sale is a change of a watched field.
        kept = {**prev, "last_verified": today}
        if kept.get("available", True):
            kept["available"] = False
            kept["last_changed_at"] = today
            # Appended, not written over: a hand-verified row's notes say why
            # a human confirmed it and must outlive the carrier's feed.
            kept["notes"] = "; ".join(
                [
                    *_own_notes(kept.get("notes"), keep_ranges=True),
                    _VANISHED_NOTE.format(today=today),
                ]
            )
        elif any(
            part.startswith(_NO_STOCK_NOTE)
            for part in (kept.get("notes") or "").split("; ")
        ):
            # Hidden, then listed without stock, now dropped: the no-stock
            # note is no longer true. The first vanished date stays.
            parts = _own_notes(kept.get("notes"), keep_vanished=True)
            if not any(_VANISHED_NOTE_RE.fullmatch(p) for p in parts):
                parts.append(_VANISHED_NOTE.format(today=today))
            kept["notes"] = "; ".join(parts)
        report["vanished"].append(key)
        numbers.append(kept)
    numbers.sort(key=lambda r: (r["country_code"], r["number_type"]))
    return numbers, report


_VANISHED_NOTE = (
    "not offered by the carrier as of {today}; kept so held numbers stay billable"
)
_NO_STOCK_NOTE = "carrier lists this type but had no numbers in stock at the last check"


def _own_notes(
    notes: str | None, keep_vanished: bool = False, keep_ranges: bool = False
) -> list[str]:
    """The parts of a row's notes that the sync did not write. The vanished,
    no-stock and price-range notes are written again on each run; other
    notes stay. ``keep_vanished`` keeps the dated vanished note as one part.
    ``keep_ranges`` keeps a price-range note: a vanished row keeps the notes
    it had (rule 2)."""
    parts, rest = [], notes or ""
    for m in _VANISHED_NOTE_RE.finditer(rest):
        if keep_vanished:
            parts.append(m.group(0))
    rest = _VANISHED_NOTE_RE.sub("", rest)
    return [
        *(
            part
            for part in rest.split("; ")
            if part.strip()
            and not part.startswith(_NO_STOCK_NOTE)
            and (keep_ranges or not _PRICE_RANGE_NOTE_RE.fullmatch(part))
        ),
        *parts,
    ]


_PRICE_RANGE_NOTE_RE = re.compile(
    r"monthly price varies by (area|number): [\d.]+ to [\d.]+ USD"
    r" (across \d+ areas|in a sample of \d+)"
)
_VANISHED_NOTE_RE = re.compile(
    re.escape(_VANISHED_NOTE).replace(re.escape("{today}"), r"\d{4}-\d{2}-\d{2}")
)


def regulatory_block(existing: dict, numbers: list[dict], provider: str) -> dict:
    prev = existing.get("regulatory", {})
    return {
        "note": (
            f"phone_setup_required is derived from each row's verification_required, "
            f"which comes from {provider}'s API (regulatory bundle, address, or requirement group). "
            "sms_registration_required is maintained by hand: A2P registration is separate from "
            "buying the number. Hail collects the verification in the console and files it "
            "with the carrier before the purchase."
        ),
        "phone_setup_required": [
            f"{n['country_code']}:{n['number_type']}"
            for n in numbers
            if n.get("verification_required")
        ],
        "sms_registration_required": prev.get("sms_registration_required", []),
    }


def write_catalog(path: Path, provider: str, numbers: list[dict]) -> dict:
    existing = json.loads(path.read_text()) if path.exists() else {}
    out = {
        "version": 3,
        "license": "CC-BY-4.0",
        "provider": provider,
        "numbers": numbers,
    }
    if "capability_monthly_fees" in existing:
        out["capability_monthly_fees"] = existing["capability_monthly_fees"]
    if existing.get("a2p_10dlc"):
        out["a2p_10dlc"] = existing["a2p_10dlc"]
    out["regulatory"] = regulatory_block(existing, numbers, provider)
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return out


def load_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("numbers", [])


SOURCE_URLS = {
    "twilio": "https://www.twilio.com/docs/phone-numbers/pricing",
    "telnyx": "https://developers.telnyx.com/api/numbers/list-available-phone-numbers",
    "didww": "https://doc.didww.com/api3/2026-04-16/coverage-resources/did-group/get-did-groups.html",
}


# -- Requirements ---------------------------------------------------------------

SUBJECTS = ("person", "business")
_LOOP: asyncio.AbstractEventLoop | None = None


class VerificationUnavailable(RuntimeError):
    """A carrier has rows that need verification but no verification plug-in
    is configured. The run skips that carrier and leaves its catalog file
    unchanged: empty blocks must never be written."""


def _run(coro: Any) -> Any:
    """Run a plug-in coroutine on one loop for the whole run. The plug-ins
    keep HTTP clients that a second, new loop would break."""
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


def _verification_provider(provider: str) -> Any:
    # Imported here: the rest of this script needs only `requests`.
    from hailhq.core.providers import verification

    plugin = verification.get_verification_provider(provider)
    if plugin is None:
        raise VerificationUnavailable(
            f"no verification plug-in for {provider}: its credentials are missing "
            "or the plug-in cannot start"
        )
    return plugin


def _union(lists: list[list[Any]]) -> list[Any]:
    """Every item of every list once: known categories in the fixed catalog
    order, then ``{other}`` items in the order they appear."""
    from hailhq.core.requirement_categories import LABELS

    keys = {i for items in lists for i in items if isinstance(i, str)}
    others: list[dict[str, str]] = []
    for items in lists:
        for i in items:
            if isinstance(i, dict) and i not in others:
                others.append(i)
    return [k for k in LABELS if k in keys] + others


def fetch_requirements(provider: str, country_code: str, number_type: str) -> dict:
    """The requirements block for one (country, type) at one carrier: for each
    kind of buyer, whether it may buy and which paper categories it needs.
    A kind the carrier refuses gives ``{"accepted": False, "items": []}``.
    DIDWW lists one requirement per DID group; the items are their union."""
    from hailhq.core.requirement_categories import classify

    plugin = _verification_provider(provider)
    rules = None
    if provider == "telnyx":
        # The raw rules tell an address in the number's area from one in
        # the country; `requirements` alone does not. Fetched once here and
        # reused for both kinds of buyer.
        rules = _run(plugin.rules(country_code, number_type))
    return _requirements_block(
        plugin, provider, country_code, number_type, rules, classify
    )


def _requirements_block(
    plugin: Any,
    provider: str,
    country_code: str,
    number_type: str,
    rules: list[dict] | None,
    classify: Any,
) -> dict:
    from hailhq.core.providers.verification import UnsupportedSubjectType

    block: dict[str, dict] = {}
    for subject in SUBJECTS:
        try:
            if provider == "didww":
                found = _run(
                    plugin.requirements_per_group(country_code, number_type, subject)
                )
                if not found:
                    # The caller only asks for rows marked verification_required.
                    # No group at all contradicts that: an error, so the sync
                    # keeps the last good block.
                    raise RequirementsUnavailable(
                        f"DIDWW lists no requirement group for {country_code} "
                        f"{number_type}"
                    )
            elif provider == "telnyx":
                # Telnyx is the only carrier whose rules are a separate fetch.
                found = [
                    plugin.requirements_from_rules(
                        country_code, number_type, subject, rules
                    )
                ]
            else:
                found = [_run(plugin.requirements(country_code, number_type, subject))]
        except UnsupportedSubjectType:
            block[subject] = {"accepted": False, "items": []}
            continue
        block[subject] = {
            "accepted": True,
            "items": _union([classify(r, provider, rules=rules) for r in found]),
        }
    return block


class RequirementsUnavailable(Exception):
    """A requirements fetch that failed for a reason this script states. Its
    message is written here, so it is safe for the public PR body."""


def _error_detail(exc: Exception) -> str:
    """What a failed fetch shows in the public PR body. Never the carrier's
    text: it can hold URLs, account ids and request bodies. Our own
    ``RequirementsUnavailable`` message, or the class name plus the HTTP
    status when the error carries one."""
    if isinstance(exc, RequirementsUnavailable):
        return str(exc)[:160]
    import httpx  # the verification plug-ins need it; the rest of this file does not

    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        status = None
        if isinstance(cur, httpx.HTTPStatusError):
            status = cur.response.status_code
        elif cur.__class__.__name__ == "TwilioRestException":
            status = getattr(cur, "status", None)
        if isinstance(status, int):
            return f"{exc.__class__.__name__} (HTTP {status})"
        if cur.__cause__ is not None:
            cur = cur.__cause__
        elif not cur.__suppress_context__:
            cur = cur.__context__
        else:
            cur = None
    return exc.__class__.__name__


def _change(key: str, old: Any, new: Any) -> str:
    """One changed field of a carrier row, for the "Changed" section. A
    requirements block is summarized, not printed whole."""
    if key == "requirements":
        return f"requirements {_block_summary(old)} -> {_block_summary(new)}"
    return f"{key} {old} -> {new}"


SUMMARY_LIMIT = 60_000  # GitHub allows 65,536 characters in a PR body
SUMMARY_CUT = "… summary cut at 60,000 characters. See the file diff for every row.\n"


def _cap_summary(text: str) -> str:
    """Cut at the last full line under the limit, with a note."""
    if len(text) <= SUMMARY_LIMIT:
        return text
    cut = text[:SUMMARY_LIMIT].rsplit("\n", 1)[0]
    return cut + "\n" + SUMMARY_CUT


def _block_summary(block: dict | None) -> str:
    if not block:
        return "none"
    parts = []
    for subject in SUBJECTS:
        side = block.get(subject) or {}
        if not side.get("accepted"):
            parts.append(f"{subject} not allowed")
            continue
        keys = []
        for i in side.get("items", []):
            if isinstance(i, str):
                keys.append(i)
            elif isinstance(i, dict):
                keys.append(f"other: {str(i.get('other', ''))[:40]}")
            else:
                keys.append("other")
        parts.append(f"{subject} {'; '.join(keys) or 'no papers'}")
    extra = [str(k) for k in block if k not in SUBJECTS]
    if extra:
        parts.append(f"also keys {', '.join(extra)}")
    return " | ".join(parts)


def _refreshed_line(key: str, how: str, old: dict | None, new: dict | None) -> str:
    return (
        f"{key} ({how}, only requirements refreshed): "
        f"{_block_summary(old)} -> {_block_summary(new)}"
    )


def _fetch_block(
    provider: str, row: dict, previous: dict | None, report: list[str]
) -> dict | None:
    """The carrier's block for one row. On any error the previous block is
    returned and one line goes to ``report``."""
    key = f"{row['country_code']}:{row['number_type']}"
    try:
        block = fetch_requirements(provider, row["country_code"], row["number_type"])
        return block
    except Exception as exc:  # one bad row must not stop the run
        detail = _error_detail(exc)
        # The logs are public: only what the PR summary shows.
        print(f"requirements failed for {provider} {key}: {detail}", file=sys.stderr)
        kept = "kept the last block" if previous else "no block stored"
        report.append(f"{key}: requirements failed ({detail}), {kept}")
        return previous


def attach_requirements(
    provider: str,
    rows: list[dict],
    existing: list[dict],
    report: list[str],
    kept_rows: list[dict] | None = None,
) -> None:
    """Set ``requirements`` on each fetched row. A row that needs verification
    gets the carrier's block; on a carrier error it keeps the block of the
    current catalog (None when there is none) and the error goes to
    ``report``. Any other row gets None.

    ``kept_rows`` are copies of catalog rows the fetch did not see but that
    are still sold. Their block is refreshed in the same pass. They never
    make the carrier fail: with no verification plug-in they stay as they are."""
    needing = [r for r in rows if r["verification_required"]]
    if needing:
        _verification_provider(
            provider
        )  # raises VerificationUnavailable before any row is written; main() skips the carrier
    kept_needing = list(kept_rows or [])
    if kept_needing and not needing:
        try:
            _verification_provider(provider)
        except VerificationUnavailable:
            report.append(
                f"{provider}: {len(kept_needing)} rows not observed this run kept "
                "their papers: verification is not configured"
            )
            kept_needing = []
    previous = {
        f"{n['country_code']}:{n['number_type']}": n.get("requirements")
        for n in existing
    }
    for row in rows:
        row["requirements"] = None
        if row["verification_required"]:
            key = f"{row['country_code']}:{row['number_type']}"
            row["requirements"] = _fetch_block(provider, row, previous.get(key), report)
    for row in kept_needing:
        key = f"{row['country_code']}:{row['number_type']}"
        row["requirements"] = _fetch_block(provider, row, previous.get(key), report)


def _section(summary: list[str], label: str, lines: list[str], cap: int = 60) -> None:
    """A PR-summary section: the first ``cap`` lines, then "… N more"."""
    if not lines:
        return
    summary.append(f"### {label} ({len(lines)})")
    summary.extend(f"- {line}" for line in lines[:cap])
    if len(lines) > cap:
        summary.append(f"- … {len(lines) - cap} more")


def _row_sections(summary: list[str], report: dict[str, list[str]]) -> None:
    """Every row change, uncapped: the reviewer must see them all."""
    for label, key in (
        ("Kept hand-verified rows", "kept"),
        ("Changed", "changed"),
        ("Added", "added"),
        ("No longer offered (kept, noted)", "vanished"),
        ("Listed with no numbers in stock (checked, noted)", "no_stock"),
        ("Not observed this run (kept as is)", "unobserved"),
    ):
        if report[key]:
            summary.append(f"### {label} ({len(report[key])})")
            summary.extend(f"- {line}" for line in report[key])


def run_provider(
    provider: str,
    config: dict[str, str],
    costs_dir: Path,
    today: str,
    summary: list[str],
    shared_dial_codes: dict[str, str] | None = None,
    http_factory: Callable[[requests.Session], Http] = Http,
    *,
    not_synced: list[str],
) -> bool:
    """``shared_dial_codes`` is one dict for the whole run: DIDWW's country
    list fills it (every country, not only the ones it sells), and the
    carriers after it borrow prefixes for rows no catalog has seen yet."""
    if shared_dial_codes is None:
        shared_dial_codes = {}
    # A carrier skipped for missing credentials goes to ``not_synced`` with
    # the failures. main() prints each entry as a bullet.
    path = costs_dir / f"{provider}.json"
    existing = load_existing(path)
    dial_codes = {n["country_code"]: n["dial_code"] for n in existing}
    for other in PROVIDERS:
        for n in load_existing(costs_dir / f"{other}.json"):
            dial_codes.setdefault(n["country_code"], n["dial_code"])
    for iso, prefix in shared_dial_codes.items():
        dial_codes.setdefault(iso, prefix)
    session = requests.Session()
    observed: set[str] | None = None
    no_stock: dict[str, dict] = {}
    if provider == "twilio":
        sid, tok = config.get("TWILIO_ACCOUNT_SID"), config.get("TWILIO_AUTH_TOKEN")
        if not sid or not tok:
            not_synced.append(
                "twilio: skipped, TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN not set"
            )
            return False
        session.auth = (sid, tok)
        rows, skipped, observed = fetch_twilio(
            http_factory(session),
            sid,
            dial_codes,
            catalog_countries={n["country_code"] for n in existing},
        )
    elif provider == "telnyx":
        key = config.get("TELNYX_API_KEY")
        if not key:
            not_synced.append("telnyx: skipped, TELNYX_API_KEY not set")
            return False
        session.headers["Authorization"] = f"Bearer {key}"
        rows, skipped = fetch_telnyx(http_factory(session), dial_codes)
    else:
        key = config.get("DIDWW_API_KEY")
        if not key:
            not_synced.append("didww: skipped, DIDWW_API_KEY not set")
            return False
        session.headers.update({"Api-Key": key, "Accept": "application/vnd.api+json"})
        rows, skipped, didww_dial_codes, no_stock = fetch_didww(http_factory(session))
        shared_dial_codes.update(didww_dial_codes)
    missing_dial = [r for r in rows if not r["dial_code"]]
    for r in missing_dial:
        skipped.append(
            f"{r['country_code']}:{r['number_type']}: no dial code known; add one by hand"
        )
    rows = [r for r in rows if r["dial_code"]]
    countries = {r["country_code"] for r in rows}
    if len(countries) < COUNTRY_FLOOR and existing:
        raise CatalogShrunk(
            f"only {len(countries)} countries fetched (< {COUNTRY_FLOOR}); refusing to shrink the catalog"
        )
    requirement_errors: list[str] = []
    # Rows still sold but not seen this run (a skip with a reason). Rows the
    # carrier dropped, or a country the sync could not look at, are not
    # refreshed: their papers say nothing about a number anyone can buy.
    fetched_keys = {f"{r['country_code']}:{r['number_type']}" for r in rows}
    sold_unseen = skipped_keys(skipped) - fetched_keys
    # A DIDWW row out of stock takes its verification flag from the empty
    # areas, so its papers follow that flag. A hidden row is not on sale.
    kept_rows = []
    for n in existing:
        key = f"{n['country_code']}:{n['number_type']}"
        needs = no_stock.get(key, {}).get(
            "verification_required", n.get("verification_required")
        )
        if key in sold_unseen and needs and n.get("available", True):
            kept_rows.append({**n, "verification_required": True})
    attach_requirements(provider, rows, existing, requirement_errors, kept_rows)
    numbers, report = merge(
        existing,
        rows,
        today,
        SOURCE_URLS[provider],
        f"{provider}-api-sync",
        unobserved=unobserved_keys(existing, skipped, observed),
        kept_rows=kept_rows,
        out_of_stock=no_stock,
    )
    write_catalog(path, provider, numbers)
    summary.append(f"## {provider}: {len(numbers)} rows, {len(countries)} countries")
    _row_sections(summary, report)
    _section(summary, "Requirements not refreshed", requirement_errors)
    _section(summary, "Skipped by the sync", skipped)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--provider", choices=(*PROVIDERS, "all"), default="all")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--costs-dir", type=Path, default=COSTS_DIR)
    parser.add_argument(
        "--summary-file", type=Path, help="Markdown summary for the PR body"
    )
    args = parser.parse_args(argv)
    config: dict[str, str] = {}
    if args.env_file:
        from dotenv import dotenv_values

        config.update({k: v for k, v in dotenv_values(args.env_file).items() if v})
    config.update({k: v for k, v in os.environ.items() if v})
    today = datetime.now(tz=timezone.utc).date().isoformat()
    header = [f"# Number catalog sync {today}", ""]
    body: list[str] = []
    providers = PROVIDERS if args.provider == "all" else (args.provider,)
    ran = 0
    shared_dial_codes: dict[str, str] = {}
    not_synced: list[str] = []
    for provider in providers:
        try:
            ran += run_provider(
                provider,
                config,
                args.costs_dir,
                today,
                body,
                shared_dial_codes,
                not_synced=not_synced,
            )
        except requests.RequestException as exc:
            not_synced.append(
                f"{provider}: failed, {exc.__class__.__name__} {getattr(exc.response, 'status_code', '')} (catalog left unchanged)"
            )
        except CatalogShrunk as exc:
            not_synced.append(f"{provider}: failed, {exc} (catalog left unchanged)")
        except VerificationUnavailable:
            # Raised before any write: the provider's file stays as it was.
            not_synced.append(
                f"{provider}: failed, verification is not configured "
                "(check the carrier credentials) (catalog left unchanged)"
            )
    # Carriers not synced come first, so the reviewer sees them before the rest.
    top = (
        ["## Carriers not synced", *(f"- {line}" for line in not_synced), ""]
        if not_synced
        else []
    )
    summary = header + top + body
    text = _cap_summary("\n".join(summary) + "\n")
    print(text)
    if args.summary_file:
        args.summary_file.write_text(text)
    return 0 if ran else 1


if __name__ == "__main__":
    sys.exit(main())
