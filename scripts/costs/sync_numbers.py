"""Sync the per-carrier number catalogs from each carrier's API.

    uv run python scripts/costs/sync_numbers.py --provider all --env-file .env

One file per carrier: costs/twilio.json, costs/telnyx.json, costs/didww.json.
Each row is one (country, number type): monthly price, what it can do (calls,
texts, MMS) and whether the buyer must be verified first. Read-only: only GET
requests, no purchases, no account changes.

Rules the merge follows, in this order:
- A row someone verified by hand (verification_method "manual-confirmed") is
  never overwritten. Disagreements are printed.
- A row that vanished from the carrier is kept with available=false, a note
  and the date. A held number must stay billable; the picker hides it.
- A carrier without credentials is skipped with a printed reason, not an error.

Credentials (env or --env-file): TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN,
TELNYX_API_KEY, DIDWW_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from collections.abc import Callable
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
        raise RuntimeError(f"gave up after retries: {url}")


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
    if setup_usd is not None and setup_usd != "0.00":
        row["setup_usd"] = setup_usd
    if notes:
        row["notes"] = notes
    return row


# -- Twilio -------------------------------------------------------------------

TWILIO_TYPE_PATH = {
    "local": "Local",
    "mobile": "Mobile",
    "toll_free": "TollFree",
    "national": "National",
}
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
) -> tuple[list[dict], list[str]]:
    rows, skipped = [], []
    for c in countries:
        iso, name = c["country_code"], c["country"]
        prices = {
            TWILIO_PRICING_TYPE.get(p["number_type"], p["number_type"]): p[
                "current_price"
            ]
            for p in pricing_by_country.get(iso, {}).get("phone_number_prices", [])
        }
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
            caps = [n["capabilities"] for n in sample]
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
                    voice=any(x.get("voice") for x in caps),
                    sms=any(x.get("SMS") for x in caps),
                    mms=any(x.get("MMS") for x in caps),
                    verification_required=address or (iso, number_type) in regulated,
                )
            )
    return rows, skipped


def fetch_twilio(
    http: Http, account_sid: str, dial_codes: dict[str, str]
) -> tuple[list[dict], list[str]]:
    base = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/AvailablePhoneNumbers"
    countries = http.get(f"{base}.json")["countries"]
    types_by_country: dict[str, list[str]] = {}
    samples: dict[tuple[str, str], list[dict]] = {}
    pricing: dict[str, dict] = {}
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
    regulations: list[dict] = []
    url = "https://numbers.twilio.com/v2/RegulatoryCompliance/Regulations?PageSize=1000"
    while url:
        page = http.get(url)
        regulations.extend(page["results"])
        url = page.get("meta", {}).get("next_page_url")
    return map_twilio(
        countries,
        types_by_country,
        pricing,
        samples,
        twilio_regulated(regulations),
        dial_codes,
    )


# -- Telnyx -------------------------------------------------------------------


def map_telnyx(
    coverage: dict[str, dict],
    samples: dict[tuple[str, str], list[dict]],
    requirements: dict[tuple[str, str], list[dict]],
    dial_codes: dict[str, str],
) -> tuple[list[dict], list[str]]:
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
            features = {f["name"] for n in sample for f in n.get("features", [])}
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
                    voice="voice" in features,
                    sms="sms" in features,
                    mms="mms" in features,
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
            req = http.get(
                "https://api.telnyx.com/v2/requirements",
                params={
                    "filter[country_code]": iso,
                    "filter[phone_number_type]": number_type,
                    "filter[action]": "ordering",
                },
            )
            requirements[(iso, number_type)] = req.get("data", [])
    return map_telnyx(coverage, samples, requirements, dial_codes)


# -- DIDWW --------------------------------------------------------------------

DIDWW_TYPE = {
    "Local": "local",
    "Mobile": "mobile",
    "National": "national",
    "Toll-free": "toll_free",
}


def map_didww(
    countries: list[dict],
    group_types: dict[str, str],
    groups_by_country: dict[str, dict],
) -> tuple[list[dict], list[str]]:
    """groups_by_country[iso] = {"data": [...did_groups...], "included": [...]}"""
    rows, skipped = [], []
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
        for g in payload.get("data", []):
            if not g.get("meta", {}).get("is_available", True):
                continue
            gt = g["relationships"].get("did_group_type", {}).get("data") or {}
            number_type = DIDWW_TYPE.get(group_types.get(gt.get("id"), ""))
            if not number_type:
                if not gt.get("id"):
                    skipped.append(f"{iso}: group {g.get('id')} has no resolvable type")
                continue
            skus = [
                included[("stock_keeping_units", s["id"])]["attributes"]
                for s in g["relationships"]
                .get("stock_keeping_units", {})
                .get("data", [])
                if ("stock_keeping_units", s["id"]) in included
            ]
            base = [s for s in skus if s.get("channels_included_count") == 0] or skus
            if not base:
                continue
            cheapest = min(base, key=lambda s: Decimal(s["monthly_price"]))
            by_type[number_type].append(
                {
                    "monthly": Decimal(cheapest["monthly_price"]),
                    "setup": Decimal(cheapest.get("setup_price") or 0),
                    "features": set(g["attributes"].get("features", [])),
                    "needs_registration": bool(
                        g.get("meta", {}).get("needs_registration")
                    ),
                    "address": g["relationships"]
                    .get("address_requirement", {})
                    .get("data")
                    is not None,
                    "areas": 1,
                }
            )
        for number_type, groups in by_type.items():
            prices = sorted(g["monthly"] for g in groups)
            cheapest = min(groups, key=lambda g: g["monthly"])
            features = set().union(*(g["features"] for g in groups))
            notes = None
            if prices[0] != prices[-1]:
                notes = f"monthly price varies by area: {money(prices[0])} to {money(prices[-1])} USD across {len(groups)} areas"
            rows.append(
                _row(
                    country_code=iso,
                    number_type=number_type,
                    country_name=name,
                    dial_code=prefix,
                    usd_per_month=money(prices[0]),
                    setup_usd=money(cheapest["setup"]),
                    voice=any(f.startswith("voice") for f in features),
                    sms=any(f.startswith("sms") for f in features),
                    mms=False,
                    verification_required=cheapest["needs_registration"]
                    or cheapest["address"],
                    notes=notes,
                )
            )
    return rows, skipped


def fetch_didww(http: Http) -> tuple[list[dict], list[str], dict[str, str]]:
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
    rows, skipped = map_didww(countries, group_types, groups_by_country)
    return rows, skipped, dial_codes


# -- merge + write -------------------------------------------------------------


def merge(
    existing: list[dict],
    fetched: list[dict],
    today: str,
    source_url: str,
    verified_by: str,
) -> tuple[list[dict], dict]:
    """Overlay fetched rows on the current catalog. Returns (numbers, report)."""
    prev_by_key = {f"{n['country_code']}:{n['number_type']}": n for n in existing}
    report: dict[str, list[str]] = {
        "kept": [],
        "changed": [],
        "added": [],
        "vanished": [],
    }
    watched = (
        "usd_per_month",
        "voice",
        "sms",
        "mms",
        "verification_required",
        "setup_usd",
        "available",
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
            diff = [f"{k}={n.get(k)}" for k in watched if prev.get(k) != n.get(k)]
            if diff:
                report["kept"].append(
                    f"{key}: carrier says {', '.join(diff)}; kept {prev['verified_by']} {prev['last_verified']}"
                )
            numbers.append(prev)
            continue
        changed = [k for k in watched if (prev or {}).get(k) != n.get(k)]
        if prev is None:
            report["added"].append(
                f"{key}: {n['usd_per_month']} voice={n['voice']} sms={n['sms']} verify={n['verification_required']}"
            )
        elif changed:
            report["changed"].append(
                f"{key}: "
                + ", ".join(f"{k} {prev.get(k)} -> {n.get(k)}" for k in changed)
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
        kept = dict(prev)
        if kept.get("available", True):
            kept["available"] = False
            kept["notes"] = (
                f"not offered by the carrier as of {today}; kept so held numbers stay billable"
            )
        report["vanished"].append(key)
        numbers.append(kept)
    numbers.sort(key=lambda r: (r["country_code"], r["number_type"]))
    return numbers, report


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


def run_provider(
    provider: str,
    config: dict[str, str],
    costs_dir: Path,
    today: str,
    summary: list[str],
    http_factory: Callable[[requests.Session], Http] = Http,
) -> bool:
    path = costs_dir / f"{provider}.json"
    existing = load_existing(path)
    dial_codes = {n["country_code"]: n["dial_code"] for n in existing}
    # DIDWW's country list carries dial prefixes; borrow them for rows the
    # other catalogs see for the first time.
    for other in PROVIDERS:
        for n in load_existing(costs_dir / f"{other}.json"):
            dial_codes.setdefault(n["country_code"], n["dial_code"])
    session = requests.Session()
    if provider == "twilio":
        sid, tok = config.get("TWILIO_ACCOUNT_SID"), config.get("TWILIO_AUTH_TOKEN")
        if not sid or not tok:
            summary.append(
                "- twilio: skipped, TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN not set"
            )
            return False
        session.auth = (sid, tok)
        rows, skipped = fetch_twilio(http_factory(session), sid, dial_codes)
    elif provider == "telnyx":
        key = config.get("TELNYX_API_KEY")
        if not key:
            summary.append("- telnyx: skipped, TELNYX_API_KEY not set")
            return False
        session.headers["Authorization"] = f"Bearer {key}"
        rows, skipped = fetch_telnyx(http_factory(session), dial_codes)
    else:
        key = config.get("DIDWW_API_KEY")
        if not key:
            summary.append("- didww: skipped, DIDWW_API_KEY not set")
            return False
        session.headers.update({"Api-Key": key, "Accept": "application/vnd.api+json"})
        rows, skipped, _ = fetch_didww(http_factory(session))
    missing_dial = [r for r in rows if not r["dial_code"]]
    for r in missing_dial:
        skipped.append(
            f"{r['country_code']}:{r['number_type']}: no dial code known; add one by hand"
        )
    rows = [r for r in rows if r["dial_code"]]
    countries = {r["country_code"] for r in rows}
    if len(countries) < COUNTRY_FLOOR and existing:
        raise SystemExit(
            f"{provider}: only {len(countries)} countries fetched (< {COUNTRY_FLOOR}); refusing to shrink the catalog"
        )
    numbers, report = merge(
        existing, rows, today, SOURCE_URLS[provider], f"{provider}-api-sync"
    )
    write_catalog(path, provider, numbers)
    summary.append(f"## {provider}: {len(numbers)} rows, {len(countries)} countries")
    for label, key in (
        ("Kept hand-verified rows", "kept"),
        ("Changed", "changed"),
        ("Added", "added"),
        ("No longer offered (kept, noted)", "vanished"),
    ):
        if report[key]:
            summary.append(f"### {label} ({len(report[key])})")
            summary.extend(f"- {line}" for line in report[key])
    if skipped:
        summary.append(f"### Skipped by the sync ({len(skipped)})")
        summary.extend(f"- {line}" for line in skipped[:60])
        if len(skipped) > 60:
            summary.append(f"- … {len(skipped) - 60} more")
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
    summary: list[str] = [f"# Number catalog sync {today}", ""]
    providers = PROVIDERS if args.provider == "all" else (args.provider,)
    ran = 0
    for provider in providers:
        try:
            ran += run_provider(provider, config, args.costs_dir, today, summary)
        except requests.RequestException as exc:
            summary.append(
                f"- {provider}: failed, {exc.__class__.__name__} {getattr(exc.response, 'status_code', '')} (catalog left unchanged)"
            )
    text = "\n".join(summary) + "\n"
    print(text)
    if args.summary_file:
        args.summary_file.write_text(text)
    return 0 if ran else 1


if __name__ == "__main__":
    sys.exit(main())
