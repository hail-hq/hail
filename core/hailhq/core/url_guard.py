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


def _ip_is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
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
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"base_url host does not resolve: {host}") from exc

    for info in infos:
        ip = info[4][0]
        if not _ip_is_public(ip):
            raise UnsafeUrlError(
                f"base_url host {host} resolves to non-public address {ip}"
            )
    return canonical_url(url)
