"""SSRF guard for customer-supplied URLs (BYO openai-compatible base_url).

A BYO LLM base_url is fetched by the hail server both at validation time
(``provider_validation``) and at call time (voicebot LLM build). Without a
guard, an org admin could point it at cloud metadata (169.254.169.254),
loopback, or RFC1918 hosts and turn the server into an SSRF proxy. This
enforces https-only and resolves the host, rejecting any address in a
private / loopback / link-local / unspecified range. No operator allowlist
(decision 2026-07-08): any public HTTPS endpoint is allowed.

The host is read with httpx's own URL parser (``httpx_url``), so the guard
resolves exactly the host httpx (and httpx2, used by the openai SDK; same
parser) will connect to. The stdlib ``urlsplit`` + ``getaddrinfo`` pair
converts non-ASCII names with IDNA 2003, which maps ``faß.example`` to
``fass.example``; httpx uses IDNA 2008 (``xn--fa-hia.example``), a
different host. Control characters (C0, DEL and C1) are refused before
parsing: httpx itself refuses only the ASCII ones.
"""

from __future__ import annotations

import ipaddress
import socket
import unicodedata

import httpx
from hailhq.core.urls import canonical_url

__all__ = ["UnsafeUrlError", "assert_public_https_url", "httpx_url", "ip_is_public"]


class UnsafeUrlError(ValueError):
    """A customer URL is not a public HTTPS endpoint."""


# RFC 6598 carrier-grade NAT space. Not private/loopback/link-local/reserved/
# unspecified per ipaddress's own classification, but not internet-routable
# either — the same kind of provider-internal address the metadata/RFC1918
# checks below exist to block.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


def httpx_url(url: str) -> httpx.URL:
    """Parse ``url`` exactly as httpx will when it sends the request.

    ``.raw_host`` of the result is the ASCII (IDNA 2008) host httpx
    connects to. Raises UnsafeUrlError for any control character (C0, DEL,
    C1) anywhere in the URL, or for a URL httpx cannot parse.
    """
    for idx, char in enumerate(url):
        if unicodedata.category(char) == "Cc":
            raise UnsafeUrlError(
                f"URL contains a control character {char!r} at position {idx}"
            )
    try:
        return httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise UnsafeUrlError(f"URL is not valid: {exc}") from exc


def ip_is_public(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
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
    parts = httpx_url(url)
    if parts.scheme != "https":
        raise UnsafeUrlError(f"base_url must use https, got '{parts.scheme or url}'")
    # The ASCII host httpx connects to: IDNA 2008 for non-ASCII names,
    # lowercased, IPv6 without brackets.
    host = parts.raw_host.decode("ascii")
    if not host:
        raise UnsafeUrlError("base_url has no host")

    port = parts.port or 443
    if not 0 < port <= 65535:
        raise UnsafeUrlError(f"base_url has an invalid port: {port}")

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        # UnicodeError: the stdlib idna codec refuses an empty or too-long
        # label (``a..b.com``) before any DNS lookup.
        raise UnsafeUrlError(f"base_url host does not resolve: {host}") from exc

    for info in infos:
        ip = info[4][0]
        if not ip_is_public(ip):
            raise UnsafeUrlError(
                f"base_url host {host} resolves to non-public address {ip}"
            )
    return canonical_url(url)
