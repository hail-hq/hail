"""Sync carrier call rates (USD per minute) into costs/voice.json.

    uv run python scripts/costs/sync_voice.py --env-file .env

One row per carrier rate:
- outbound: a call Hail places through the carrier, by destination prefix.
  Some rows apply only to calls from certain caller ID prefixes
  (`origination_prefixes`, for example the "From EEA" rates); ["ALL"] means
  any caller ID.
- inbound: a call the carrier delivers to Hail, by number type.

Read-only: only GET requests, no account changes.

Carriers:
- Twilio: Elastic SIP Trunking rates from the Pricing API
  (pricing.twilio.com/v2/Trunking/Countries).
- Telnyx and DIDWW: no rate API is known yet. They are reported as not
  synced, and any rows they already have stay as they are.

Merge rules:
- A synced carrier's rows are replaced by what the carrier returns. A row
  whose price did not change keeps its last_changed_at. The other carriers'
  rows are kept as they are.
- A fetch that returns far fewer countries than the file already holds for
  that carrier is refused, and the file is left unchanged.

Credentials (env or --env-file): TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
VOICE_FILE = ROOT / "costs" / "voice.json"
PROVIDERS = ("twilio", "telnyx", "didww")
NO_RATE_SOURCE = {
    "telnyx": "no rate API known yet; the rate deck is a file in Mission Control",
    "didww": "no rate API known yet",
}
# Refuse a fetch with fewer countries than this share of the existing ones.
SHRINK_FLOOR = Decimal("0.8")

TWILIO_COUNTRIES_URL = "https://pricing.twilio.com/v2/Trunking/Countries?PageSize=1000"
TWILIO_INBOUND_TYPE = {"local": "local", "mobile": "mobile", "tollfree": "toll_free"}


class RatesShrunk(Exception):
    """A fetch returned too few countries; the file is left unchanged."""


def progress(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def money(value: str) -> str:
    """Carrier price -> decimal string without float noise ("0.01700" -> "0.017")."""
    text = format(Decimal(value).normalize(), "f")
    return text if "." in text else f"{text}.0"


class Http:
    """GET with retries. Timeouts, 429 and 5xx back off; anything else raises."""

    def __init__(self, session: requests.Session, rps: float = 8.0) -> None:
        self.session = session
        self.min_gap = 1.0 / rps
        self._last = 0.0

    def get(self, url: str) -> Any:
        for attempt in range(6):
            gap = self.min_gap - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, timeout=60)
            except (requests.Timeout, requests.ConnectionError):
                time.sleep(min(2**attempt, 30))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(min(2**attempt, 30))
                continue
            resp.raise_for_status()
            return resp.json()
        raise requests.exceptions.RetryError(f"gave up after retries: {url}")


# -- Twilio -------------------------------------------------------------------


def twilio_line_type(name: str) -> str:
    """Line type from Twilio's rate label, e.g.
    "Trunking Outbound Minute - Spain - Mobile - From Eea" -> "mobile".

    "landline" is the plain country rate. "other" is a named zone (a city,
    a region, "Other"); the destination prefixes say which numbers it covers.
    The label's first part is the country, but not always spelled like the
    API's country name ("Antigua & Barbuda"), so it is dropped by position.
    """
    rest = name.removeprefix("Trunking Outbound Minute - ").partition(" - ")[2]
    rest = re.sub(r"\s*-?\s*From [\w/-]+$", "", rest).strip(" -")
    lower = rest.lower()
    if "mobile" in lower:
        return "mobile"
    if "toll free" in lower:
        return "toll_free"
    if any(w in lower for w in ("special", "premium", "shared cost")):
        return "special"
    return "landline" if not rest else "other"


def map_twilio(answer: dict, today: str) -> list[dict]:
    """One Pricing API country answer -> voice.json rows."""
    cc = answer["iso_country"]
    common = {
        "provider": "twilio",
        "country_code": cc,
        "last_verified": today,
        "verification_method": "carrier-sync",
        "verified_by": "twilio-api-sync",
        "source_url": f"https://www.twilio.com/en-us/sip-trunking/pricing/{cc.lower()}",
    }
    rows = []
    for p in answer.get("originating_call_prices") or []:
        number_type = TWILIO_INBOUND_TYPE.get(p["number_type"])
        if number_type is None or p.get("current_price") is None:
            continue
        rows.append(
            {
                **common,
                "direction": "inbound",
                "number_type": number_type,
                "usd_per_minute": money(p["current_price"]),
            }
        )
    for p in answer.get("terminating_prefix_prices") or []:
        if p.get("current_price") is None:
            continue
        rows.append(
            {
                **common,
                "direction": "outbound",
                "name": p["friendly_name"],
                "line_type": twilio_line_type(p["friendly_name"]),
                "destination_prefixes": p["destination_prefixes"],
                "origination_prefixes": p["origination_prefixes"],
                "usd_per_minute": money(p["current_price"]),
            }
        )
    return rows


def fetch_twilio(http: Http, today: str) -> list[dict]:
    url: str | None = TWILIO_COUNTRIES_URL
    countries: list[dict] = []
    while url:
        page = http.get(url)
        countries += page["countries"]
        url = page["meta"].get("next_page_url")
    rows: list[dict] = []
    for i, country in enumerate(countries, 1):
        progress(f"twilio {i}/{len(countries)} {country['iso_country']}")
        rows += map_twilio(http.get(country["url"]), today)
    return rows


# -- merge --------------------------------------------------------------------


def row_key(row: dict) -> tuple:
    """One row per key. A carrier can price one label at two rates, so the
    exact-match flag and the lowest destination prefix are part of the key."""
    return (
        row["provider"],
        row["country_code"],
        row["direction"],
        row.get("number_type") or "",
        row.get("name") or "",
        tuple(row.get("origination_prefixes") or ()),
        bool(row.get("exact_match")),
        min(row.get("destination_prefixes") or [""]),
    )


def merge(
    existing: list[dict], provider: str, fresh: list[dict], today: str
) -> tuple[list[dict], list[str]]:
    """Replace one carrier's rows. Returns the rows and one line per change."""
    old = {row_key(r): r for r in existing if r["provider"] == provider}
    old_countries = {r["country_code"] for r in old.values()}
    new_countries = {r["country_code"] for r in fresh}
    if old_countries and len(new_countries) < len(old_countries) * SHRINK_FLOOR:
        raise RatesShrunk(
            f"{len(new_countries)} countries, the file has {len(old_countries)}"
        )
    changes: list[str] = []
    rows: list[dict] = []
    for row in fresh:
        key = row_key(row)
        prev = old.pop(key, None)
        label = f"{row['country_code']} {row.get('name') or row['direction'] + ' ' + row['number_type']}"
        if prev is None:
            changes.append(f"added {label}: ${row['usd_per_minute']}/min")
            rows.append({**row, "last_changed_at": today})
        elif prev["usd_per_minute"] != row["usd_per_minute"] or prev.get(
            "destination_prefixes"
        ) != row.get("destination_prefixes"):
            if prev["usd_per_minute"] != row["usd_per_minute"]:
                changes.append(
                    f"{label}: ${prev['usd_per_minute']} -> ${row['usd_per_minute']}/min"
                )
            else:
                changes.append(f"{label}: destination prefixes changed")
            rows.append({**row, "last_changed_at": today})
        else:
            rows.append({**row, "last_changed_at": prev["last_changed_at"]})
    for prev in old.values():
        changes.append(
            f"removed {prev['country_code']} {prev.get('name') or prev['direction'] + ' ' + prev['number_type']}"
        )
    others = [r for r in existing if r["provider"] != provider]
    return others + rows, changes


def sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=row_key)


def dump(rows: list[dict]) -> str:
    """JSON with each prefix list on one line, so a row stays short in a diff."""
    text = json.dumps(
        {"version": 1, "license": "CC-BY-4.0", "rates": sort_rows(rows)},
        indent=2,
        ensure_ascii=False,
    )
    return (
        re.sub(
            r"\[\s+((?:\"[^\"]*\",?\s+)+)\]",
            lambda m: "[" + ", ".join(re.findall(r"\"[^\"]*\"", m.group(1))) + "]",
            text,
        )
        + "\n"
    )


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())["rates"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--provider", choices=(*PROVIDERS, "all"), default="all")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--file", type=Path, default=VOICE_FILE)
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

    rows = load(args.file)
    not_synced: list[str] = []
    body: list[str] = []
    ran = 0
    providers = PROVIDERS if args.provider == "all" else (args.provider,)
    for provider in providers:
        if provider in NO_RATE_SOURCE:
            not_synced.append(f"{provider}: {NO_RATE_SOURCE[provider]}")
            continue
        sid, token = config.get("TWILIO_ACCOUNT_SID"), config.get("TWILIO_AUTH_TOKEN")
        if not (sid and token):
            not_synced.append(
                f"{provider}: skipped, TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is not set"
            )
            continue
        session = requests.Session()
        session.auth = (sid, token)
        try:
            fresh = fetch_twilio(Http(session), today)
            rows, changes = merge(rows, provider, fresh, today)
        except requests.RequestException as exc:
            not_synced.append(
                f"{provider}: failed, {exc.__class__.__name__} {getattr(exc.response, 'status_code', '')} (rates left unchanged)"
            )
            continue
        except RatesShrunk as exc:
            not_synced.append(f"{provider}: failed, {exc} (rates left unchanged)")
            continue
        ran += 1
        countries = len({r["country_code"] for r in fresh})
        body += [
            f"## {provider}",
            "",
            f"{len(fresh)} rates in {countries} countries.",
            "",
        ]
        body += [f"- {line}" for line in changes[:200]] or ["- no change"]
        if len(changes) > 200:
            body.append(f"- … {len(changes) - 200} more. See the file diff.")
        body.append("")
    if ran:
        args.file.write_text(dump(rows))

    top = (
        ["## Carriers not synced", *(f"- {line}" for line in not_synced), ""]
        if not_synced
        else []
    )
    text = "\n".join([f"# Call rate sync {today}", "", *top, *body]) + "\n"
    print(text)
    if args.summary_file:
        args.summary_file.write_text(text)
    return 0 if ran else 1


if __name__ == "__main__":
    sys.exit(main())
