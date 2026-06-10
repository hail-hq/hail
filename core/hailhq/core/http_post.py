"""HTTP POST adapter for webhook deliveries.

Wraps httpx with a private-network guard so misconfigured tenants
can't aim a delivery at internal infrastructure. Self-hosters can
disable the guard via ``HAIL_WEBHOOK_ALLOW_PRIVATE_NETWORKS=true``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

__all__ = [
    "PrivateNetworkBlockedError",
    "httpx_post",
    "is_private_url",
    "validate_webhook_target",
]


class PrivateNetworkBlockedError(RuntimeError):
    """Refused to POST: target resolves to a private / local address."""


def _ip_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def _resolve_all(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        try:
            out.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return out


def _is_private_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return _ip_is_private(ipaddress.ip_address(host))
    except ValueError:
        pass
    # Check EVERY resolved address (A + AAAA) — an AAAA-only host pointing
    # at ::1/ULA space must not slip through an IPv4-only lookup.
    ips = _resolve_all(host)
    return any(_ip_is_private(ip) for ip in ips)


def is_private_url(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.hostname:
        return False
    return _is_private_host(parsed.hostname)


def validate_webhook_target(url: str, *, allow_private_networks: bool) -> None:
    """Raise ValueError if ``url`` is not a deliverable webhook target.

    Requires https; rejects private/local targets unless the deployment
    has opted in (self-host escape hatch). Mirrors the delivery-time guard
    in ``httpx_post`` so misconfig fails synchronously at write time.

    When ``allow_private_networks=True``, plain HTTP (not only HTTPS) is
    accepted to ANY host — the escape hatch is a blanket relaxation, not
    restricted to private-network addresses only.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        if not (allow_private_networks and parsed.scheme == "http"):
            raise ValueError("target_url must use https")
    if not parsed.hostname:
        raise ValueError("target_url must include a host")
    if not allow_private_networks and is_private_url(url):
        raise ValueError("target_url must not point at a private address")


async def httpx_post(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    allow_private_networks: bool,
    timeout_seconds: float = 10.0,
) -> tuple[int, str]:
    """POST and return ``(status_code, response_text)``.

    Raises ``PrivateNetworkBlockedError`` when the target resolves to a
    private/local address and ``allow_private_networks`` is False.
    """
    if not allow_private_networks and await asyncio.to_thread(is_private_url, url):
        raise PrivateNetworkBlockedError(url)
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.post(url, content=body, headers=headers)
        return resp.status_code, resp.text
