"""Fire-and-forget signed POST to the hail-website's internal endpoints.

Used by voicebot (and future SMS / email senders) to notify the website
that a billable usage event has been recorded, so the website's private
rater can turn it into a dollar debit row in near-real-time.

Self-host behavior: ``HAIL_BASE_URL`` is unset → notifier returns
immediately. ``usage_events`` rows accumulate locally as analytics; nobody
rates them.

HMAC: SHA-256 of the request body, sent as ``X-Hail-Signature: sha256=<hex>``.

Also hosts ``fetch_organization_name`` — the one request/response (not
fire-and-forget) call in this module: the API service resolves an org's
display name at call-creation time for the spoken TCPA disclosure, on a
tight budget, failing safe to ``None``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from hailhq.core.config import settings
from hailhq.core.hmac_signing import sign
from hailhq.core.urls import join_url

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5

# Org-name lookup budget: tight enough not to noticeably slow POST /calls,
# and every failure just degrades the spoken disclosure to generic wording.
_ORG_NAME_TIMEOUT_SECONDS = 1.0

_session: aiohttp.ClientSession | None = None
_pending_tasks: set[asyncio.Task[None]] = set()


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
        )
    return _session


async def aclose() -> None:
    """Drain in-flight notifier tasks, then close the module-level session.

    Wire into the service lifespan. Draining first matters: the
    fire-and-forget tasks in ``_pending_tasks`` are not tied to any
    request/response cycle, so they can still be mid-POST when the
    lifespan shuts down — closing the session under them would abort the
    send, and a task that had not yet touched the session would lazily
    create a fresh one that nothing ever closes.
    """
    global _session
    if _pending_tasks:
        await asyncio.wait(set(_pending_tasks), timeout=_TIMEOUT_SECONDS)
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _signed_body(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]] | None:
    """Encode + sign a payload for the website's internal endpoints.

    Returns ``(body, headers)``, or ``None`` when ``HAIL_BASE_URL`` /
    ``HAIL_INTERNAL_SECRET`` are unset (self-host or unconfigured cloud) —
    the shared no-op guard for every caller in this module.
    """
    if not settings.hail_base_url or not settings.hail_internal_secret:
        return None
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Hail-Signature": sign(body, settings.hail_internal_secret),
    }
    return body, headers


async def _post(path: str, payload: dict[str, Any]) -> None:
    """Send a signed POST to ``HAIL_BASE_URL``-relative ``path``.

    Errors are logged and swallowed — the caller is fire-and-forget.
    """
    parts = _signed_body(payload)
    if parts is None:
        return  # self-host or unconfigured cloud; no-op
    body, headers = parts
    url = join_url(settings.hail_base_url, path)
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


async def fetch_organization_name(organization_id: str) -> str | None:
    """Resolve an organization's display name from hail-website.

    Fail-safe by design: unset config (self-host — same posture as the
    fire-and-forget notifier), timeout, non-200, connection error,
    malformed body, or a blank name all return ``None``. Never raises —
    the call-creation path must not be able to fail on this.
    """
    parts = _signed_body({"organization_id": organization_id})
    if parts is None:
        return None
    body, headers = parts
    url = join_url(settings.hail_base_url, "api/internal/organizations/lookup")
    try:
        async with _get_session().post(
            url,
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=_ORG_NAME_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    "[internal_webhook] org-name lookup returned %s", resp.status
                )
                return None
            payload = await resp.json()
    except Exception:
        logger.warning("[internal_webhook] org-name lookup failed", exc_info=True)
        return None
    name = payload.get("name") if isinstance(payload, dict) else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


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
