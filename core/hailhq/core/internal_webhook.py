"""Fire-and-forget signed POST to the hail-website's internal endpoints.

Used by voicebot (and future SMS / email senders) to notify the website
that a billable usage event has been recorded, so the website's private
rater can turn it into a dollar debit row in near-real-time.

Self-host behavior: ``HAIL_BASE_URL`` is unset → notifier returns
immediately. ``usage_events`` rows accumulate locally as analytics; nobody
rates them.

HMAC: SHA-256 of the request body, sent as ``X-Hail-Signature: sha256=<hex>``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

import aiohttp

from hailhq.core.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5

_session: aiohttp.ClientSession | None = None
_pending_tasks: set[asyncio.Task[None]] = set()


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
        )
    return _session


async def aclose() -> None:
    """Close the module-level session. Wire into the service lifespan."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def _post(path: str, payload: dict[str, Any]) -> None:
    """Send a signed POST to ``${HAIL_BASE_URL}${path}``.

    Errors are logged and swallowed — the caller is fire-and-forget.
    """
    base = settings.hail_base_url.rstrip("/")
    secret = settings.hail_internal_secret
    if not base or not secret:
        return  # self-host or unconfigured cloud; no-op
    url = f"{base}{path}"
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Hail-Signature": _sign(body, secret),
    }
    try:
        async with _get_session().post(url, data=body, headers=headers) as resp:
            if resp.status >= 400:
                text = await resp.text()
                logger.warning(
                    "[internal_webhook] %s returned %s: %s",
                    path,
                    resp.status,
                    text[:200],
                )
    except Exception:  # pragma: no cover — best-effort
        logger.warning("[internal_webhook] %s failed", path, exc_info=True)


def notify_usage_event_recorded(usage_event_id: str) -> None:
    """Fire-and-forget kick to the website's rater.

    Schedules a background task so callers don't pay HTTP RTT on their
    own commit path. The rater is batch-aware on the receiving end; the
    ``usage_event_id`` field is informational (for logs / debugging).
    """
    # Hold a reference so Python's GC doesn't drop the pending task —
    # ``asyncio.create_task`` only keeps a weak reference internally.
    task = asyncio.create_task(
        _post("/api/internal/usage-events/rate", {"usage_event_id": usage_event_id})
    )
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
