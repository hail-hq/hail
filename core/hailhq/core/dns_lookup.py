"""DNS lookups over DNS-over-HTTPS (no resolver dependency).

Used to (a) detect whether a domain already receives mail elsewhere
(Google/Outlook) so onboarding can pick apex vs a prefix, and (b) confirm a
domain's receive MX points at SES inbound after the user publishes DNS.
"""

from __future__ import annotations

import httpx

_DOH_URL = "https://dns.google/resolve"
_MX_TYPE = 15


def ses_inbound_host(region: str) -> str:
    return f"inbound-smtp.{region}.amazonaws.com"


async def resolve_mx(domain: str) -> list[str]:
    """Return the MX target hosts for ``domain`` (lowercased, no trailing dot)."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(_DOH_URL, params={"name": domain, "type": "MX"})
        resp.raise_for_status()
        data = resp.json()
    hosts: list[str] = []
    for answer in data.get("Answer", []):
        if answer.get("type") != _MX_TYPE:
            continue
        # data looks like "10 inbound-smtp.eu-west-1.amazonaws.com."
        parts = str(answer.get("data", "")).split()
        if parts:
            hosts.append(parts[-1].rstrip(".").lower())
    return hosts
