"""HTTP POST adapter for webhook deliveries.

Wraps httpx with a private-network guard so misconfigured tenants
can't aim a delivery at internal infrastructure. Self-hosters can
disable the guard via ``HAIL_WEBHOOK_ALLOW_PRIVATE_NETWORKS=true``.
"""

from __future__ import annotations

import asyncio
import ssl

import httpx
from hailhq.core.url_guard import UnsafeUrlError, assert_public_host, connect_target

__all__ = [
    "PrivateNetworkBlockedError",
    "describe_send_error",
    "httpx_post",
    "validate_webhook_target",
]


class PrivateNetworkBlockedError(RuntimeError):
    """Refused to POST: target does not resolve, or resolves to a non-public address."""


def _tls_cause(exc: BaseException) -> ssl.SSLError | None:
    """The TLS error under an httpx connect error, read from the exception
    chain (httpcore re-raises with ``from``), never from message text."""
    seen: BaseException | None = exc
    for _ in range(10):  # a chain is short; stop on a cycle
        if seen is None:
            return None
        if isinstance(seen, ssl.SSLError):
            return seen
        # Follow what Python itself would show: the cause, or a context that
        # was not hidden with ``from None``.
        seen = seen.__cause__ or (
            None if seen.__suppress_context__ else seen.__context__
        )
    return None


def describe_send_error(exc: BaseException) -> str:
    """A fixed category, never the exception text: it can name internal hosts."""
    if isinstance(exc, httpx.ConnectTimeout):
        return "Could not connect to your endpoint in time."
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "No answer from your endpoint in time."
    if isinstance(exc, PrivateNetworkBlockedError):
        return "target_url is not allowed: it does not resolve to a public address."
    if isinstance(exc, UnsafeUrlError):
        return "target_url is not a valid URL."
    if isinstance(exc, httpx.ConnectError):
        tls = _tls_cause(exc)
        if isinstance(tls, ssl.SSLCertVerificationError):
            return "The TLS certificate check failed."
        if tls is not None:
            return "The TLS handshake failed."
        return "Could not connect to your endpoint."
    if isinstance(exc, httpx.RemoteProtocolError):
        return "Your endpoint closed the connection without a valid answer."
    return "Could not reach your endpoint."


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


# Fallback addresses tried after a failed connect, and the connect budget each.
_MAX_ADDRESSES = 3
_CONNECT_SECONDS = 3.0


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
    answer after that many bytes. Raises ``TimeoutError`` when the whole
    send takes longer than twice ``timeout_seconds``.
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
        # A name with many records must not multiply the send time.
        targets = [parsed.copy_with(host=ip) for ip in ordered[:_MAX_ADDRESSES]]
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
    # One deadline for the whole send: httpx times each phase alone, so a
    # receiver that drips its answer, or several dead addresses, could hold a
    # worker slot far longer. The last address (or the only one) gets the full
    # connect budget, so a slow TCP + TLS start is not reported as a failure;
    # the short budget only applies when there is a fallback address to move
    # on to.
    full = httpx.Timeout(timeout_seconds)
    short = httpx.Timeout(
        timeout_seconds, connect=min(_CONNECT_SECONDS, timeout_seconds)
    )
    async with asyncio.timeout(timeout_seconds * 2), httpx.AsyncClient(
        timeout=full
    ) as client:
        # A certificate or TLS failure on one address is the most useful
        # reason when every address fails: a plain refuse or timeout on the
        # last one (often IPv6 with no route) would hide it. Chosen trade-off:
        # a stale record's certificate can then be named while the live
        # address refuses. A certificate failure beats other TLS failures.
        tls_failure: tuple[httpx.ConnectError, ssl.SSLError] | None = None
        for index, target in enumerate(targets):
            try:
                async with client.stream(
                    "POST",
                    target,
                    content=body,
                    headers=send_headers,
                    auth=auth,
                    extensions=extensions,
                    timeout=full if index == len(targets) - 1 else short,
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
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Nothing was sent: try the next checked address. A dead
                # address often times out instead of refusing, and a stale
                # record can serve another certificate.
                tls = _tls_cause(exc) if isinstance(exc, httpx.ConnectError) else None
                if tls is not None:
                    is_cert = isinstance(tls, ssl.SSLCertVerificationError)
                    kept_cert = tls_failure is not None and isinstance(
                        tls_failure[1], ssl.SSLCertVerificationError
                    )
                    if tls_failure is None or (is_cert and not kept_cert):
                        tls_failure = (exc, tls)
                if index == len(targets) - 1:
                    if tls_failure is not None and tls_failure[0] is not exc:
                        # The original httpx error (it keeps its request),
                        # with the ssl error as its direct cause.
                        raise tls_failure[0] from tls_failure[1]
                    raise
    raise AssertionError("unreachable: targets is never empty")
