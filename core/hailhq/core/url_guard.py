"""SSRF guard for customer-supplied URLs (BYO openai-compatible base_url).

A BYO LLM base_url is fetched by the hail server both at validation time
(``provider_validation``) and at call time (voicebot LLM build). Without a
guard, an org admin could point it at cloud metadata (169.254.169.254),
loopback, or RFC1918 hosts and turn the server into an SSRF proxy. This
enforces https-only and resolves the host, rejecting any address in a
private / loopback / link-local / unspecified range. No operator allowlist
(decision 2026-07-08): any public HTTPS endpoint is allowed.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from hailhq.core.urls import canonical_url

__all__ = ["UnsafeUrlError", "assert_public_https_url"]


class UnsafeUrlError(ValueError):
    """A customer URL is not a public HTTPS endpoint."""


# RFC 6598 carrier-grade NAT space. Not private/loopback/link-local/reserved/
# unspecified per ipaddress's own classification, but not internet-routable
# either — the same kind of provider-internal address the metadata/RFC1918
# checks below exist to block.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


def _ip_is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    # Judge IPv4-mapped IPv6 (::ffff:a.b.c.d) on the embedded IPv4 address:
    # whether CPython's is_* checks see through the mapping is patch-level
    # dependent, so normalize it ourselves.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_V4:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_public_https_url(url: str) -> str:
    """Return the canonicalized URL if it is a public HTTPS endpoint.

    Raises UnsafeUrlError for non-https schemes, missing hosts, unresolvable
    hosts, or any host that resolves to a private/loopback/link-local/
    reserved/unspecified address (every resolved address must be public).
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnsafeUrlError(f"base_url must use https, got '{parts.scheme or url}'")
    host = parts.hostname
    if not host:
        raise UnsafeUrlError("base_url has no host")

    try:
        port = parts.port or 443
    except ValueError as exc:
        raise UnsafeUrlError(f"base_url has an invalid port: {exc}") from exc

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"base_url host does not resolve: {host}") from exc

    for info in infos:
        ip = info[4][0]
        if not _ip_is_public(ip):
            raise UnsafeUrlError(
                f"base_url host {host} resolves to non-public address {ip}"
            )
    return canonical_url(url)
