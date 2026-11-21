"""HTTP POST adapter for webhook deliveries.

Wraps httpx with a private-network guard so misconfigured tenants
can't aim a delivery at internal infrastructure. Self-hosters can
disable the guard via ``HAIL_WEBHOOK_ALLOW_PRIVATE_NETWORKS=true``.
"""

from __future__ import annotations

import asyncio

import httpx
from hailhq.core.url_guard import UnsafeUrlError, assert_public_host, connect_target

__all__ = [
    "PrivateNetworkBlockedError",
    "httpx_post",
    "validate_webhook_target",
]


class PrivateNetworkBlockedError(RuntimeError):
    """Refused to POST: target does not resolve, or resolves to a non-public address."""


def validate_webhook_target(url: str, *, allow_private_networks: bool) -> None:
    """Raise ValueError if ``url`` is not a deliverable webhook target.

    Requires https; rejects non-public or unresolvable targets (ranges in
    ``url_guard``) unless the deployment has opted in (self-host escape
    hatch). Same check as ``httpx_post`` at delivery time, so misconfig
    fails synchronously at write time. Blocking (DNS): call it off the
    event loop.

    When ``allow_private_networks=True``, plain HTTP (not only HTTPS) is
    accepted to ANY host — the escape hatch is a blanket relaxation, not
    restricted to private-network addresses only.
    """
    try:
        parsed, host, port = connect_target(url)
    except UnsafeUrlError as exc:
        raise ValueError(f"target_url is not valid: {exc}") from exc
    if parsed.scheme != "https" and not (
        allow_private_networks and parsed.scheme == "http"
    ):
        raise ValueError("target_url must use https")
    if allow_private_networks:
        return
    try:
        assert_public_host(host, port)
    except UnsafeUrlError as exc:
        raise ValueError(f"target_url is not allowed: {exc}") from exc


async def httpx_post(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    allow_private_networks: bool,
    timeout_seconds: float = 10.0,
    max_body_bytes: int | None = None,
) -> tuple[int, str]:
    """POST and return ``(status_code, response_text)``.

    Raises ``UnsafeUrlError`` (a ValueError) for a URL httpx would not send
    (no host, bad port, control characters). Raises
    ``PrivateNetworkBlockedError`` when ``allow_private_networks`` is False
    and the host does not resolve or has a non-public address.

    With the guard on, the request goes to the address that passed the
    check, not to a second DNS answer: the Host header and the TLS name
    check keep the original host. ``max_body_bytes`` stops reading the
    answer after that many bytes.
    """
    parsed, host, port = connect_target(url)
    targets = [parsed]
    extensions: dict[str, str] = {}
    send_headers = dict(headers)
    if not allow_private_networks:
        try:
            addresses = await asyncio.to_thread(assert_public_host, host, port)
        except UnsafeUrlError as exc:
            raise PrivateNetworkBlockedError(str(exc)) from exc
        # IPv4 first: a host without an IPv6 route fails every IPv6 connect.
        # The rest stay as fallbacks, as httpx's own connect would try them.
        ordered = sorted(dict.fromkeys(addresses), key=lambda ip: ":" in ip)
        targets = [parsed.copy_with(host=ip) for ip in ordered]
        send_headers["Host"] = parsed.netloc.decode("ascii")
        if parsed.scheme == "https":
            extensions["sni_hostname"] = host
    # An explicit receiver header takes precedence over Basic credentials
    # embedded in a legacy target URL. HTTPX otherwise overwrites it.
    auth = (
        httpx.Auth()
        if any(name.lower() == "authorization" for name in send_headers)
        else httpx.USE_CLIENT_DEFAULT
    )
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for index, target in enumerate(targets):
            try:
                async with client.stream(
                    "POST",
                    target,
                    content=body,
                    headers=send_headers,
                    auth=auth,
                    extensions=extensions,
                ) as resp:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if max_body_bytes is not None and size >= max_body_bytes:
                            break
                    raw = b"".join(chunks)
                    if max_body_bytes is not None:
                        raw = raw[:max_body_bytes]
                    text = raw.decode(resp.encoding or "utf-8", "replace")
                    return resp.status_code, text
            except httpx.ConnectError:
                # Nothing was sent: try the next checked address.
                if index == len(targets) - 1:
                    raise
    raise AssertionError("unreachable: targets is never empty")
