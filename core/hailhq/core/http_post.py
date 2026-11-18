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
    fails synchronously at write time.

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
        raise ValueError(
            f"target_url must not point at a private or unresolvable address: {exc}"
        ) from exc


async def httpx_post(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    allow_private_networks: bool,
    timeout_seconds: float = 10.0,
) -> tuple[int, str]:
    """POST and return ``(status_code, response_text)``.

    Raises ``UnsafeUrlError`` (a ValueError) for a URL httpx would not send
    (no host, bad port, control characters). Raises
    ``PrivateNetworkBlockedError`` when ``allow_private_networks`` is False
    and the host does not resolve or has a non-public address.
    """
    _, host, port = connect_target(url)
    if not allow_private_networks:
        try:
            await asyncio.to_thread(assert_public_host, host, port)
        except UnsafeUrlError as exc:
            raise PrivateNetworkBlockedError(str(exc)) from exc
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.post(url, content=body, headers=headers)
        return resp.status_code, resp.text
