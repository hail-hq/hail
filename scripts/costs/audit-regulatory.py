"""Read-only Twilio regulatory audit. Run with the Hail workspace Python.

Example: .venv/bin/python scripts/costs/audit-regulatory.py --env-file .env --output /tmp/hail-regulatory-audit
Only GET requests; never provisions numbers or changes bundles. Outputs omit
bundle/account SIDs, emails, end-user records and uploaded documents.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import dotenv_values

BASE = "https://numbers.twilio.com/v2/RegulatoryCompliance/"


def has_requirements(regulation: dict) -> bool:
    return any(regulation.get("requirements", {}).values())


def build_matrix(numbers: list[dict], regulations: list[dict]) -> list[dict]:
    matrix = []
    for number in numbers:
        matches = [
            r
            for r in regulations
            if r["iso_country"] == number["country_code"]
            and r["number_type"].replace("-", "_") == number["number_type"]
        ]
        matrix.append(
            {
                "country_code": number["country_code"],
                "number_type": number["number_type"],
                "bundle_required_for": sorted(
                    {r["end_user_type"] for r in matches if has_requirements(r)}
                ),
                "regulation_found": bool(matches),
                "source_url": f"https://www.twilio.com/en-us/guidelines/{number['country_code'].lower()}/regulatory",
                "requirements": [
                    {
                        "end_user_type": r["end_user_type"],
                        "requirements": r["requirements"],
                    }
                    for r in matches
                    if has_requirements(r)
                ],
            }
        )
    return matrix


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = {**(dotenv_values(args.env_file) if args.env_file else {}), **os.environ}
    account = config.get("TWILIO_ACCOUNT_SID")
    token = config.get("TWILIO_AUTH_TOKEN")
    if not account or not token:
        parser.error("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are required")
    session = requests.Session()
    session.auth = (account, token)

    def get(url: str) -> dict:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "numbers.twilio.com",
            "api.twilio.com",
        }:
            raise ValueError("Unexpected Twilio pagination host")
        response = session.get(url, timeout=40, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f"Twilio audit GET failed (HTTP {response.status_code})")
        return response.json()

    def pages(resource: str) -> list[dict]:
        rows = []
        url = BASE + resource + "?PageSize=1000"
        while url:
            data = get(url)
            rows.extend(data["results"])
            url = data["meta"].get("next_page_url")
        return rows

    account_data = get(f"https://api.twilio.com/2010-04-01/Accounts/{account}.json")
    regulations = pages("Regulations")
    bundles = pages("Bundles")
    bundle_summary = []
    for bundle in bundles:
        regulation = get(BASE + "Regulations/" + bundle["regulation_sid"])
        bundle_summary.append(
            {
                key: regulation.get(key)
                for key in ("iso_country", "number_type", "end_user_type")
            }
            | {"status": bundle["status"], "valid_until": bundle["valid_until"]}
        )
    addresses = []
    address_url = f"https://api.twilio.com/2010-04-01/Accounts/{account}/Addresses.json?PageSize=1000"
    while address_url:
        address_data = get(address_url)
        addresses.extend(address_data["addresses"])
        next_uri = address_data.get("next_page_uri")
        address_url = "https://api.twilio.com" + next_uri if next_uri else None
    address_summary = dict(Counter(a["iso_country"] for a in addresses))
    global_requirements = [
        {
            key: r[key]
            for key in ("iso_country", "number_type", "end_user_type", "requirements")
        }
        for r in regulations
        if has_requirements(r)
    ]
    catalog = json.loads(
        (Path(__file__).resolve().parents[2] / "costs/twilio.json").read_text()
    )
    matrix = build_matrix(catalog["numbers"], regulations)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "account_name": account_data["friendly_name"],
        "regulation_count": len(regulations),
        "address_country_counts": address_summary,
        "global_requirements": global_requirements,
        "bundles": bundle_summary,
        "catalog": matrix,
        "limitations": [
            "Requirements apply to the actual end user; platform approval is not customer approval.",
            "Empty bundle requirements do not rule out address requirements, SMS registration or inventory restrictions.",
            "Hail's current acquire adapter does not attach a bundle or address.",
        ],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "audit.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# Twilio regulatory audit",
        "",
        f"Checked: {report['checked_at']}",
        "",
        f"Account: {report['account_name']}",
        "",
        "## Account bundles",
        "",
        "| Country | Type | End user | Status |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {b['iso_country']} | {b['number_type']} | {b['end_user_type']} | {b['status']} |"
        for b in bundle_summary
    ]
    lines += [
        "",
        f"Existing address records by country: {address_summary}. These are not a compliance or reuse guarantee.",
    ]
    lines += [
        "",
        "## Current Hail catalog",
        "",
        "No listed bundle requirement is not a purchase-readiness guarantee.",
        "",
        "| Country | Type | Bundle required for |",
        "|---|---|---|",
    ]
    lines += [
        f"| [{r['country_code']}]({r['source_url']}) | {r['number_type']} | {', '.join(r['bundle_required_for']) or ('None listed' if r['regulation_found'] else 'Unknown')} |"
        for r in matrix
    ]
    lines += ["", "## Limits", "", *[f"- {x}" for x in report["limitations"]]]
    (args.output / "audit.md").write_text("\n".join(lines) + "\n")
    print(
        json.dumps(
            {
                "regulations": len(regulations),
                "catalog_rows": len(matrix),
                "bundle_required_rows": sum(
                    bool(r["bundle_required_for"]) for r in matrix
                ),
                "bundle_statuses": dict(Counter(b["status"] for b in bundles)),
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
