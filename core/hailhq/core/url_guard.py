"""SSRF guard for customer-supplied URLs.

Three callers: the BYO openai-compatible LLM ``base_url``
(``assert_public_https_url``; fetched at validation time and at call time),
agent HTTP tool urls (``assert_public_https_url`` when saved;
``agent_tools/http_tool.py`` checks each address again at connect time) and
webhook deliveries (``core/hailhq/core/http_post.py``). Without a
guard, a customer could point any of them at cloud metadata (169.254.169.254),
loopback or internal hosts and turn the server into an SSRF proxy.

``assert_public_host`` resolves the host and refuses it unless every
address is public. Not public (``ip_is_public``): private (RFC 1918,
IPv6 ULA fc00::/7), loopback, link-local (incl. 169.254.169.254),
CGNAT 100.64.0.0/10 (Tailscale), IPv6 site-local fec0::/10, multicast,
reserved and unspecified, 6to4 2002::/16 and local-use NAT64
64:ff9b:1::/48. IPv4-mapped and well-known NAT64 (64:ff9b::/96) addresses
are judged on the IPv4 address they wrap.
A host that does not resolve is refused too, and so are ``localhost`` and
``*.localhost`` (RFC 6761), before any lookup. No operator allowlist for
``base_url`` (decision 2026-07-08); webhooks have
``HAIL_WEBHOOK_ALLOW_PRIVATE_NETWORKS``.

The host is read with httpx's own URL parser (``connect_target``), so the
guard resolves exactly the host httpx (and httpx2, used by the openai SDK;
same parser) will connect to. The stdlib ``urlsplit`` + ``getaddrinfo``
pair converts non-ASCII names with IDNA 2003, which maps ``faß.example``
to ``fass.example``; httpx uses IDNA 2008 (``xn--fa-hia.example``), a
different host. Control characters (C0, DEL and C1) are refused before
parsing: httpx itself refuses only the ASCII ones.

HTTP tools are the exception: they connect with aiohttp, which parses the
url with yarl, not httpx. yarl gives the same IDNA 2008 host for names like
``faß.example``, but its parser differs: it accepts some hosts httpx
refuses (it drops a zero-width joiner and maps ``Ⅸ`` to ``ix``). So for
tools the save check here is a first filter, and the real guard is the
resolver check in ``agent_tools/http_tool.py``, which judges every address
aiohttp is about to connect to.

Not covered: httpx resolves the host again when it connects, so a DNS
rebind between the two lookups is still possible.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import unicodedata

import httpx
from hailhq.core.urls import canonical_url

__all__ = [
    "NOT_PUBLIC",
    "UnsafeUrlError",
    "assert_public_host",
    "assert_public_https_url",
    "connect_target",
    "httpx_url",
    "ip_is_public",
]


logger = logging.getLogger(__name__)

# The one customer-facing text for a refused host. It does not say which case
# applied (private address or no DNS answer), so the error text does not tell
# a customer what our resolver answers for internal names. Details go to the
# server log.
NOT_PUBLIC = "URL host is private or could not be resolved"


class UnsafeUrlError(ValueError):
    """A customer URL is not a public HTTPS endpoint."""


# RFC 6598 carrier-grade NAT space. Not private/loopback/link-local/reserved/
# unspecified per ipaddress's own classification, but not internet-routable
# either — the same kind of provider-internal address the metadata/RFC1918
# checks below exist to block.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
# RFC 6052 well-known NAT64 prefix: the IPv4 address is the low 32 bits.
_NAT64_WKP_V6 = ipaddress.ip_network("64:ff9b::/96")
# 6to4 (2002::/16): its gateway forwards to any host behind it, and CPython
# lists it as private only from 3.11.10 / 3.12.4, so refuse it ourselves.
_SIXTOFOUR_V6 = ipaddress.ip_network("2002::/16")
# RFC 8215 local-use NAT64: the IPv4 position depends on the operator's
# prefix length, which we cannot know, so the whole range is refused.
_NAT64_LOCAL_V6 = ipaddress.ip_network("64:ff9b:1::/48")


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


def connect_target(url: str) -> tuple[httpx.URL, str, int]:
    """Return ``(parsed, host, port)`` for the connection httpx will open.

    ``host`` is ASCII: IDNA 2008 for non-ASCII names, lowercased, IPv6
    without brackets. Raises UnsafeUrlError for the cases in ``httpx_url``,
    a URL with no host, or a port outside 1-65535.
    """
    parsed = httpx_url(url)
    host = parsed.raw_host.decode("ascii")
    if not host:
        raise UnsafeUrlError("URL has no host")
    port = parsed.port
    if port is None:
        port = 80 if parsed.scheme == "http" else 443
    if not 0 < port <= 65535:
        raise UnsafeUrlError(f"URL has an invalid port: {port}")
    return parsed, host, port


def ip_is_public(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """False for every range listed in the module docstring."""
    addr = ipaddress.ip_address(ip)
    # Judge IPv4-mapped IPv6 (::ffff:a.b.c.d) on the embedded IPv4 address:
    # whether CPython's is_* checks see through the mapping is patch-level
    # dependent, so normalize it ourselves.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if isinstance(addr, ipaddress.IPv6Address):
        if addr in _NAT64_LOCAL_V6:
            return False
        if addr in _NAT64_WKP_V6:
            # ipaddress calls this range reserved, which also refuses public
            # targets: judge the embedded IPv4 address instead.
            return ip_is_public(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
        if addr in _SIXTOFOUR_V6:
            return False
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_V4:
        return False
    # Deprecated IPv6 site-local (fec0::/10): ipaddress counts it neither
    # private nor reserved, but it is never internet-routable.
    if isinstance(addr, ipaddress.IPv6Address) and addr.is_site_local:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_public_host(host: str, port: int) -> None:
    """Raise UnsafeUrlError unless ``host`` resolves and every address is public.

    ``localhost`` and ``*.localhost`` are refused before any lookup.
    ``host`` is the ASCII host from ``connect_target``. Blocking (DNS):
    call it off the event loop.
    """
    # RFC 6761: localhost and *.localhost are loopback by definition. Refuse
    # them before DNS, so for these names the result does not depend on
    # /etc/hosts or search domains. Other names (``ip6-localhost``) go to DNS
    # and the address check below. Strip trailing dots so ``localhost.`` is
    # caught too.
    name = host.rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        logger.info("url guard: refused %s (localhost)", host)
        raise UnsafeUrlError(NOT_PUBLIC)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        # OSError: socket.gaierror, or a plain OSError for a resolver system
        # error (too many open files). UnicodeError: the stdlib idna codec
        # refuses an empty or too-long label (``a..b.com``) before any DNS
        # lookup.
        logger.info("url guard: %s does not resolve: %s", host, exc)
        raise UnsafeUrlError(NOT_PUBLIC) from exc
    if not infos:
        logger.info("url guard: %s resolved to no addresses", host)
        raise UnsafeUrlError(NOT_PUBLIC)
    # Check EVERY resolved address (A + AAAA): one private address is enough
    # to refuse.
    for info in infos:
        ip = info[4][0]
        try:
            public = ip_is_public(ip)
        except ValueError:
            public = False  # an address ipaddress cannot read: fail closed
        if not public:
            # The address stays in the server log: customer-facing errors
            # must not reveal what our resolver answers for internal names.
            logger.info("url guard: %s resolves to non-public %s", host, ip)
            raise UnsafeUrlError(NOT_PUBLIC)


def assert_public_https_url(url: str) -> str:
    """Return the canonicalized URL if it is a public HTTPS endpoint.

    Raises UnsafeUrlError for non-https schemes, a missing host or bad port,
    an unresolvable host, or any host with a non-public address (see the
    module docstring for the ranges).
    """
    parsed, host, port = connect_target(url)
    if parsed.scheme != "https":
        raise UnsafeUrlError(f"base_url must use https, got '{parsed.scheme or url}'")
    assert_public_host(host, port)
    return canonical_url(url)
