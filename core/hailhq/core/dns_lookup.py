"""DNS lookups over DNS-over-HTTPS (no resolver dependency).

Used to (a) detect whether a domain already receives mail elsewhere
(Google/Outlook) so onboarding can pick apex vs a prefix, (b) confirm a
domain's receive MX points at SES inbound after the user publishes DNS, and
(c) power the guided DNS-check route: find the customer's DNS host, and
report which of the records we asked them to publish are already visible in
public DNS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx
from hailhq.core.config import settings
from hailhq.core.providers.email import DkimRecord

_DOH_URL = "https://dns.google/resolve"

# Google DoH numeric record types. https://developers.google.com/speed/public-dns/docs/doh/json
_TYPE_NUMBERS: dict[str, int] = {"NS": 2, "CNAME": 5, "MX": 15, "TXT": 16}


def ses_inbound_host(region: str) -> str:
    return f"inbound-smtp.{region}.amazonaws.com"


class DnsLookupError(Exception):
    """A DoH lookup failed (transport error or unparseable response).

    Raised by ``_resolve`` to every caller except ``resolve_mx``, so a
    lookup failure can be told apart from "no answer": ``resolve_zone_ns``
    aborts its zone walk on this instead of treating the failure as an
    empty NS answer and climbing to the wrong zone, and
    ``dns_check_email_domain`` (the only external catcher) turns it into a
    degraded response (``lookup_ok=False``) instead of a 5xx. ``resolve_mx``
    is unaffected — it still raises the raw
    ``httpx.HTTPError``/``ValueError`` via ``raise_on_error=True``.
    """


async def _resolve(name: str, rtype: str, *, raise_on_error: bool = False) -> list[str]:
    """Shared DoH call. Returns raw ``Answer[].data`` strings for ``rtype``.

    ``resolve_mx`` passes ``raise_on_error=True`` to keep its pre-existing
    behaviour: on any httpx/JSON error it re-raises the raw error unchanged
    — its callers (``check_domain``, which lets the error 500, and
    ``verify``, which catches ``Exception`` itself to degrade
    ``receive_ready`` to ``None``) already depend on that. Every other
    caller gets ``DnsLookupError`` instead of a silently swallowed ``[]`` —
    swallowing a transient failure as "no answer" is what let
    ``resolve_zone_ns`` mistake a failed NS query for an empty one and keep
    climbing to the wrong zone.

    The same treatment applies to a DoH ``Status`` other than ``0``
    (NOERROR) or ``3`` (NXDOMAIN) — SERVFAIL and friends arrive as HTTP 200
    with an empty ``Answer``, which reads exactly like "no answer" unless
    ``Status`` is checked. That's the same bug for ``resolve_zone_ns`` as a
    transport error: only checked for ``raise_on_error=False`` callers, so
    ``resolve_mx`` keeps returning ``[]`` on a bad ``Status`` unchanged, as
    it always has (it never inspected ``Status`` before this).
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_DOH_URL, params={"name": name, "type": rtype})
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        if raise_on_error:
            raise
        raise DnsLookupError(f"DoH lookup failed for {rtype} {name}") from exc
    status = data.get("Status", 0)
    if status not in (0, 3) and not raise_on_error:
        raise DnsLookupError(f"DoH lookup returned Status={status} for {rtype} {name}")
    type_number = _TYPE_NUMBERS.get(rtype.upper())
    return [
        str(answer.get("data", ""))
        for answer in data.get("Answer", [])
        if type_number is None or answer.get("type") == type_number
    ]


def _norm_host(value: str) -> str:
    """Lowercase, and strip a trailing dot and surrounding quotes."""
    return value.strip().strip('"').rstrip(".").lower()


def _unquote_txt(raw: str) -> str:
    """Join a (possibly chunked) double-quoted DoH TXT ``data`` string.

    Long TXT values come back as several quoted chunks, e.g.
    ``'"part1" "part2"'``; join their contents. A bare, unquoted value is
    returned unchanged.
    """
    chunks = re.findall(r'"([^"]*)"', raw)
    if chunks:
        return "".join(chunks)
    return raw


def _mx_hosts(raw: list[str]) -> list[str]:
    """Parse ``"<priority> <host>."`` MX answer strings into lowercased hosts."""
    hosts: list[str] = []
    for item in raw:
        parts = item.split()
        if parts:
            hosts.append(_norm_host(parts[-1]))
    return hosts


async def resolve_mx(domain: str) -> list[str]:
    """Return the MX target hosts for ``domain`` (lowercased, no trailing dot)."""
    raw = await _resolve(domain, "MX", raise_on_error=True)
    return _mx_hosts(raw)


async def resolve_zone_ns(domain: str) -> tuple[str, list[str]]:
    """Find the zone apex for ``domain`` and its nameservers.

    An NS query on a non-apex name returns no ``Answer`` (only an
    ``Authority`` SOA), so this strips the left label and retries until an
    NS answer comes back. Stops before querying a bare TLD (a name with no
    dot). Returns ``("", [])`` if every label up to the TLD gave a genuine
    "no answer".

    Raises ``DnsLookupError`` (propagated from ``_resolve``, uncaught here)
    the moment any single NS query fails — the walk aborts immediately
    rather than treating the failure as "no answer" and climbing to the
    next label up, which would report the wrong zone (e.g. a failed lookup
    on ``acme.co.uk`` must never fall through to ``co.uk``).
    """
    name = domain.strip().lower().rstrip(".")
    while "." in name:
        answers = await _resolve(name, "NS")
        if answers:
            nameservers = sorted({_norm_host(a) for a in answers})
            return name, nameservers
        name = name.split(".", 1)[1]
    return "", []


@dataclass(frozen=True)
class DnsProvider:
    id: str
    name: str
    dns_url: str
    note: str | None


# Suffix table per docs/superpowers/specs/2026-09-19-email-unbranded-react-domains-tracking-design.md
# section A3, matched against each (lowercased, trailing-dot-stripped)
# nameserver. Two distinct match kinds, kept in separate fields so a suffix
# can never silently be checked as a substring:
#   - suffixes: the nameserver must equal the token or end with "." + token
#     (a true DNS-label suffix, e.g. "domaincontrol.com").
#   - fragments: the token is checked as a raw substring. Only for hosts
#     whose nameservers vary the characters *after* the token — Route 53
#     ("ns-123.awsdns-45.org") and IONOS ("ns1234.ui-dns.com/.de/.org/.biz").
#     Both fragments are written with a leading/trailing dot so they can
#     only match on a label boundary, not mid-label.
#
# No entry for Google: legacy Google Domains and Google Cloud DNS both hand
# out ns-cloud-*.googledomains.com nameservers, so the suffix can't tell
# them apart, and a Google Cloud DNS customer manages DNS in GCP, not
# Squarespace. See test_detect_dns_provider_google_cloud_dns_returns_none.
_PROVIDER_TABLE: list[tuple[DnsProvider, tuple[str, ...], tuple[str, ...]]] = [
    (
        DnsProvider(
            id="cloudflare",
            name="Cloudflare",
            dns_url="https://dash.cloudflare.com/?to=/:account/:zone/dns/records",
            note="Set Proxy status to DNS only for every CNAME.",
        ),
        ("ns.cloudflare.com",),
        (),
    ),
    (
        DnsProvider(
            id="godaddy",
            name="GoDaddy",
            dns_url="https://sso.godaddy.com/",
            note=None,
        ),
        ("domaincontrol.com",),
        (),
    ),
    (
        DnsProvider(
            id="namecheap",
            name="Namecheap",
            dns_url="https://www.namecheap.com/myaccount/login.aspx",
            note=None,
        ),
        ("registrar-servers.com",),
        (),
    ),
    (
        DnsProvider(
            id="route53",
            name="Route 53",
            dns_url="https://console.aws.amazon.com/route53/",
            note=None,
        ),
        (),
        (".awsdns-",),
    ),
    (
        DnsProvider(
            id="squarespace",
            name="Squarespace",
            dns_url="https://account.squarespace.com/domains",
            note=None,
        ),
        ("squarespacedns.com",),
        (),
    ),
    (
        DnsProvider(
            id="vercel",
            name="Vercel",
            # Verified: https://vercel.com/docs/domains/managing-dns-records
            # links this exact URL for "manage nameservers from the domains
            # page" — a real Vercel DNS-records destination, not a generic
            # dashboard landing page.
            dns_url="https://vercel.com/dashboard/domains",
            note=None,
        ),
        ("vercel-dns.com",),
        (),
    ),
    (
        DnsProvider(
            id="digitalocean",
            name="DigitalOcean",
            dns_url="https://cloud.digitalocean.com/networking/domains",
            note=None,
        ),
        ("digitalocean.com",),
        (),
    ),
    (
        DnsProvider(
            id="ionos",
            name="IONOS",
            dns_url="https://my.ionos.com/domains",
            note=None,
        ),
        (),
        (".ui-dns.",),
    ),
    (
        DnsProvider(
            id="hover",
            name="Hover",
            dns_url="https://hover.com/signin",
            note=None,
        ),
        ("hover.com",),
        (),
    ),
    (
        DnsProvider(
            id="namecom",
            name="Name.com",
            dns_url="https://www.name.com/account/login",
            note=None,
        ),
        ("name.com",),
        (),
    ),
    (
        DnsProvider(
            id="porkbun",
            name="Porkbun",
            # porkbun.com/account/domainsSpeedy could not be verified
            # against Porkbun's own docs/KB (kb.porkbun.com), and fetching
            # it directly only redirects to login — so this points at the
            # login page instead of an unverified deep link.
            dns_url="https://porkbun.com/account/login",
            note=None,
        ),
        ("porkbun.com",),
        (),
    ),
    (
        DnsProvider(
            id="gandi",
            name="Gandi",
            dns_url="https://admin.gandi.net/",
            note=None,
        ),
        ("gandi.net",),
        (),
    ),
    (
        DnsProvider(
            id="ovh",
            name="OVH",
            dns_url="https://www.ovh.com/manager/",
            note=None,
        ),
        ("ovh.net",),
        (),
    ),
]


def _matches_suffix(ns: str, suffix: str) -> bool:
    """True DNS-label suffix match: exact, or preceded by a label boundary."""
    return ns == suffix or ns.endswith("." + suffix)


def detect_dns_provider(nameservers: list[str]) -> DnsProvider | None:
    """Match a zone's nameservers against the known-host suffix table.

    Case-insensitive, ignoring a trailing dot. Returns ``None`` when no
    nameserver matches any known host.
    """
    normalized = [_norm_host(ns) for ns in nameservers]
    for provider, suffixes, fragments in _PROVIDER_TABLE:
        for ns in normalized:
            if any(_matches_suffix(ns, suffix) for suffix in suffixes) or any(
                fragment in ns for fragment in fragments
            ):
                return provider
    return None


async def observe_record(record: dict[str, Any]) -> bool:
    """Does public DNS already show this record?

    ``record`` carries ``type`` (``CNAME`` | ``MX`` | ``TXT``), ``name`` and
    ``value``. CNAME matches when the answer target equals ``value``; MX
    when ``value`` is among the MX hosts; TXT when ``value`` equals one of
    the (unquoted, chunk-joined) TXT strings. Host comparisons are
    case-insensitive and ignore trailing dots and surrounding quotes.

    Raises ``DnsLookupError`` (propagated from ``_resolve``, uncaught here)
    on a DoH failure — the caller (the dns-check route) decides how to
    degrade, rather than this function silently reporting "not observed"
    for what may actually be a transient lookup failure.
    """
    rtype = str(record.get("type", "")).upper()
    name = str(record.get("name", ""))
    value = str(record.get("value", ""))

    if rtype == "CNAME":
        answers = await _resolve(name, "CNAME")
        target = _norm_host(value)
        return any(_norm_host(a) == target for a in answers)
    if rtype == "MX":
        answers = await _resolve(name, "MX")
        return _norm_host(value) in _mx_hosts(answers)
    if rtype == "TXT":
        answers = await _resolve(name, "TXT")
        wanted = _unquote_txt(value.strip())
        return any(wanted == _unquote_txt(a) for a in answers)
    return False


async def dmarc_present(name: str) -> bool:
    """Is there a TXT record at ``_dmarc.<name>`` starting ``v=DMARC1``?

    ``name`` is either the sending domain or its zone apex — per RFC 7489
    §6.6.3 a receiver checks ``_dmarc.<sending domain>`` before falling back
    to the organisational domain, so the dns-check route calls this once
    for each and treats DMARC as present if either answers. Raises
    ``DnsLookupError`` (propagated from ``_resolve``, uncaught here) on a
    DoH failure.
    """
    answers = await _resolve(f"_dmarc.{name}", "TXT")
    return any(_unquote_txt(a).startswith("v=DMARC1") for a in answers)


def custom_dns_records(domain: str, dkim_records: list[DkimRecord]) -> list[dict]:
    """Build the full DNS-record list for a custom domain.

    Every DKIM CNAME + MAIL FROM MX/TXT returned by the provider, plus the
    SES inbound-receipt MX the tenant adds at the apex so mail addressed to
    the domain lands in SES Receiving. Shared by the ``POST /verify`` route
    and the background verification worker so both persist the same records.
    """
    records: list[dict] = [r.model_dump() for r in dkim_records]
    records.append(
        {
            "type": "MX",
            "name": domain,
            "value": ses_inbound_host(settings.aws_region),
            "priority": 10,
        }
    )
    return records
