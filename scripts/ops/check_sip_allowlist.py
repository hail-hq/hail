"""Compare the carriers' published SIP signaling IPs with the committed list.

    python3 scripts/ops/check_sip_allowlist.py --report /tmp/sip-report.md

Reads ``infra/livekit/inbound-trunks.json`` and fetches each carrier's
published list:

* Telnyx: https://sip.telnyx.com/voice.json, ``signaling`` (each region's
  ``primary``, ``secondary`` and ``allowlistCidrs``). Telnyx sends inbound
  calls from these. ``uac`` is SIP Attach, which Hail does not use.
* Twilio: the "Regional signaling IP gateways" section of
  https://www.twilio.com/docs/sip-trunking/ip-addresses.md. The global media
  range above it is not signaling. A bare address (no prefix) is a /32,
  unless it is inside a range the same region lists.

Exit code 0: every list matches and no source changed its version. Exit 1:
an address was added or removed, a source changed its version, or a page
could not be fetched or read (no addresses, a changed layout). The report
says which. The weekly workflow opens or updates a GitHub issue on exit 1.

It never changes the committed file or any LiveKit trunk, and needs no keys.
Runbook: docs/operations/inbound-trunk-lockdown.md.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = ROOT / "infra" / "livekit" / "inbound-trunks.json"
USER_AGENT = "hail-sip-allowlist-check/1"
TIMEOUT_SECONDS = 30
TWILIO_SECTION = "## Regional signaling IP gateways"
# An IPv4 address with an optional prefix; a bare address is a /32.
ADDRESS = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(/\d{1,2})?(?!\.?\d)")
# A wider range than this is not a signaling gateway list: refuse it.
MIN_PREFIX = 16


class SourceError(Exception):
    """A published list could not be fetched or read."""


@dataclass
class Published:
    networks: set[str]
    version: str


@dataclass
class Result:
    carrier: str
    problems: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # Shown in the report, but no action: a page edit with the same addresses.
    notes: list[str] = field(default_factory=list)


def _network(value: str) -> str:
    try:
        net = ipaddress.ip_network(value.strip(), strict=True)
    except ValueError as exc:
        raise SourceError(f"not an IP network: {value!r}") from exc
    if net.prefixlen < MIN_PREFIX:
        raise SourceError(f"range wider than /{MIN_PREFIX}: {value}")
    return str(net)


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8")
    except Exception as exc:  # any network or decode failure is a finding
        raise SourceError(f"could not fetch {url}: {exc}") from exc


def parse_telnyx(text: str) -> Published:
    try:
        data = json.loads(text)
        regions = data["signaling"]
        version = str(data["version"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"voice.json has a new layout: {exc!r}") from exc
    if not isinstance(regions, dict) or not regions:
        raise SourceError("voice.json lists no signaling regions")
    networks: set[str] = set()
    for name, region in regions.items():
        if not isinstance(region, dict):
            raise SourceError(f"voice.json region {name} has a new layout")
        for key in ("primary", "secondary"):
            if not region.get(key):
                raise SourceError(f"voice.json region {name} has no {key} address")
            networks.add(_network(f"{region[key]}/32"))
        for cidr in region.get("allowlistCidrs") or []:
            networks.add(_network(cidr))
    return Published(networks, version)


def parse_twilio(text: str) -> Published:
    version = ""
    match = re.search(r"^dateModified:\s*(\S+)\s*$", text, re.MULTILINE)
    if match:
        version = match.group(1)
    else:
        raise SourceError("Twilio page has no dateModified line")
    start = text.find(TWILIO_SECTION)
    if start < 0:
        raise SourceError(f"Twilio page has no {TWILIO_SECTION!r} section")
    rest = text[start + len(TWILIO_SECTION) :]
    end = re.search(r"^## ", rest, re.MULTILINE)
    section = rest[: end.start()] if end else rest
    regions = re.split(r"^### ", section, flags=re.MULTILINE)[1:]
    if not regions:
        raise SourceError("Twilio signaling section lists no regions")
    networks: set[str] = set()
    for region in regions:
        name = region.splitlines()[0].strip()
        found = [
            ipaddress.ip_network(_network(ip + (prefix or "/32")))
            for ip, prefix in ADDRESS.findall(region)
        ]
        if not found:
            raise SourceError(f"Twilio region {name!r} lists no address")
        # The page lists each range, then every address in it: an address
        # inside a listed range is not a new entry.
        ranges = [n for n in found if n.prefixlen < 32]
        networks.update(
            str(n)
            for n in found
            if n.prefixlen < 32 or not any(n.subnet_of(r) for r in ranges)
        )
    return Published(networks, version)


PARSERS: dict[str, Callable[[str], Published]] = {
    "telnyx": parse_telnyx,
    "twilio": parse_twilio,
}


def compare(carrier: str, trunk: dict, fetcher: Callable[[str], str] = fetch) -> Result:
    result = Result(carrier)
    source = trunk["source"]
    try:
        published = PARSERS[carrier](fetcher(source["url"]))
        committed = {_network(a) for a in trunk["allowed_addresses"]}
    except SourceError as exc:
        result.problems.append(str(exc))
        return result
    if not published.networks:
        result.problems.append("no addresses found in the published list")
        return result
    result.added = sorted(published.networks - committed)
    result.removed = sorted(committed - published.networks)
    if published.version != source["version"]:
        result.notes.append(
            f"source version changed: committed {source['version']}, "
            f"published {published.version}"
        )
    return result


def report(results: list[Result]) -> tuple[str, bool]:
    """Markdown for the issue, and whether any action is needed."""
    lines: list[str] = []
    changed = False
    for r in results:
        if not (r.problems or r.added or r.removed):
            lines.append(f"- {r.carrier}: matches the published list.")
            lines.extend(f"  - note: {n}" for n in r.notes)
            continue
        changed = True
        lines.append(f"- {r.carrier}: action needed.")
        lines.extend(f"  - {p}" for p in r.problems)
        lines.extend(f"  - note: {n}" for n in r.notes)
        lines.extend(f"  - published, not in our list (add): `{a}`" for a in r.added)
        lines.extend(
            f"  - in our list, no longer published (review): `{a}`" for a in r.removed
        )
    return "\n".join(lines) + "\n", changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--settings", type=Path, default=SETTINGS)
    parser.add_argument("--report", type=Path, help="also write the report here")
    args = parser.parse_args(argv)
    trunks = json.loads(args.settings.read_text())["trunks"]
    results = [
        compare(carrier, trunk)
        for carrier, trunk in trunks.items()
        if carrier in PARSERS
    ]
    text, changed = report(results)
    print(text, end="")
    if args.report:
        args.report.write_text(text)
    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
