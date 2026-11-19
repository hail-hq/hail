"""Tests for scripts/ops/check_sip_allowlist.py. No network: pages are fakes."""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "check_sip_allowlist", HERE / "check_sip_allowlist.py"
)
check = importlib.util.module_from_spec(spec)
sys.modules["check_sip_allowlist"] = check
spec.loader.exec_module(check)

TELNYX = {
    "version": "2026-01-01T00:00:00Z",
    "signaling": {
        "US": {"primary": "192.0.2.10", "secondary": "192.0.2.11"},
        "ME": {
            "primary": "198.51.100.1",
            "secondary": "198.51.100.2",
            "allowlistCidrs": ["203.0.113.0/24"],
        },
    },
    "uac": {"US": {"ip": "192.0.2.99"}},
    "media": {"globalCidrs": ["198.18.0.0/15"]},
}

TWILIO = """---
dateModified: 2026-01-01T00:00:00.000Z
---

## Global media IP gateways

| roaming | UDP | ANY | ANY | 198.18.0.0/18 | 10,000 - 60,000 |

## Regional signaling IP gateways

Allow all of the following.

### North America Virginia gateways

    192.0.2.0/30 (individual IP addresses below):
    192.0.2.0

### Europe Ireland gateways

    198.51.100.0/30 which translates to:

## Looking for more inspiration?

    203.0.113.0/24
"""


def trunk(addresses, url="https://example.test", version="2026-01-01T00:00:00Z"):
    return {
        "allowed_addresses": addresses,
        "source": {"url": url, "version": version},
    }


def test_telnyx_reads_signaling_only() -> None:
    got = check.parse_telnyx(json.dumps(TELNYX))
    assert got.networks == {
        "192.0.2.10/32",
        "192.0.2.11/32",
        "198.51.100.1/32",
        "198.51.100.2/32",
        "203.0.113.0/24",
    }
    assert got.version == "2026-01-01T00:00:00Z"


def test_twilio_reads_the_signaling_section_only() -> None:
    got = check.parse_twilio(TWILIO)
    assert got.networks == {"192.0.2.0/30", "198.51.100.0/30"}
    assert got.version == "2026-01-01T00:00:00.000Z"


def test_twilio_reads_bare_addresses_as_single_hosts() -> None:
    """A region may list addresses with no range: each is a /32. An address
    inside a range the region lists is part of that range."""
    page = TWILIO.replace(
        "### Europe Ireland gateways",
        "### Asia-Pacific Tokyo gateways\n\n    203.0.113.7\n    203.0.113.9.\n"
        "\n### Europe Ireland gateways",
    ).replace("    192.0.2.0\n", "    192.0.2.0\n    192.0.2.200\n")
    got = check.parse_twilio(page)
    assert got.networks == {
        "192.0.2.0/30",
        "192.0.2.200/32",
        "198.51.100.0/30",
        "203.0.113.7/32",
        "203.0.113.9/32",
    }


def test_match_is_clean() -> None:
    result = check.compare(
        "twilio",
        trunk(["198.51.100.0/30", "192.0.2.0/30"], version="2026-01-01T00:00:00.000Z"),
        fetcher=lambda _url: TWILIO,
    )
    text, changed = check.report([result])
    assert not changed
    assert "matches" in text


def test_new_and_dropped_ranges_are_reported() -> None:
    result = check.compare(
        "twilio",
        trunk(["192.0.2.0/30", "192.0.2.128/30"]),
        fetcher=lambda _url: TWILIO,
    )
    assert result.added == ["198.51.100.0/30"]
    assert result.removed == ["192.0.2.128/30"]
    _, changed = check.report([result])
    assert changed


def test_version_change_alone_is_a_note_not_an_alert() -> None:
    """A page edit with the same addresses needs no action."""
    result = check.compare(
        "telnyx",
        trunk(sorted(check.parse_telnyx(json.dumps(TELNYX)).networks), version="old"),
        fetcher=lambda _url: json.dumps(TELNYX),
    )
    assert not result.problems and "version" in result.notes[0]
    text, changed = check.report([result])
    assert not changed and "note:" in text


def test_new_layout_or_empty_page_is_reported() -> None:
    for page in ("{}", "not json", json.dumps({**TELNYX, "signaling": {}})):
        result = check.compare(
            "telnyx", trunk([]), fetcher=lambda _url, page=page: page
        )
        assert result.problems, page
    no_section = TWILIO.replace("## Regional signaling IP gateways", "## Other")
    result = check.compare("twilio", trunk([]), fetcher=lambda _url: no_section)
    assert result.problems
    empty_region = TWILIO.replace("198.51.100.0/30 which", "which")
    result = check.compare("twilio", trunk([]), fetcher=lambda _url: empty_region)
    assert "lists no address" in result.problems[0]


def test_fetch_failure_is_reported() -> None:
    def broken(_url):
        raise check.SourceError("could not fetch")

    result = check.compare("twilio", trunk([]), fetcher=broken)
    assert result.problems == ["could not fetch"]


def test_committed_file_is_valid() -> None:
    """Every carrier with a source has addresses that parse; names and
    placeholders are unique, so two trunks never list the same entry."""
    data = json.loads(check.SETTINGS.read_text())
    trunks = data["trunks"]
    assert set(trunks) == {"twilio", "telnyx", "didww"}
    names = [t["name"] for t in trunks.values()]
    entries = [t["placeholder"] for t in trunks.values()] + [data["shared_placeholder"]]
    assert len(set(names)) == len(names)
    assert len(set(entries)) == len(entries)
    for carrier, t in trunks.items():
        for address in t["allowed_addresses"]:
            ipaddress.ip_network(address, strict=True)
        if carrier in check.PARSERS:
            assert t["allowed_addresses"], carrier
            assert t["source"]["url"].startswith("https://")
    assert trunks["didww"]["auth_username"]
    assert not trunks["didww"]["allowed_addresses"]
    # Public repo: no trunk id (ST_...) and no secret.
    text = check.SETTINGS.read_text()
    assert "password" not in text.lower()
    assert not re.search(r"\bST_[A-Za-z0-9]", text)
