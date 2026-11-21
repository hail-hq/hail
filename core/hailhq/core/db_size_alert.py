"""Emails Hail once a day while the database is larger than
``ALERT_BYTES``.

A managed database plan can have a storage quota, and passing it can block
every request. The knowledge base is the first feature that stores large
amounts of data per workspace, so Hail wants a warning well before any
quota. Run by ``AbuseMonitorWorker`` on each of its (hourly) ticks.

At most one email per UTC day across every API process: the day of the
last alert is kept in ``platform_flags`` (key ``ALERT_FLAG``), claimed with
one upsert that matches only when the stored day is older. The claim and
the send share a transaction, so a failed send rolls the claim back and a
later tick tries again.

Fails open: a missing sender (no mail domain configured), a failed query or
a failed send only logs a warning.

The email goes from ``noreply@<HAIL_MAIL_BASE_DOMAIN>`` (``noreply`` is a
reserved prefix no workspace can take) to ``HAIL_SUPPORT_EMAIL``. It holds
the size and the threshold only, no workspace data.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from hailhq.core.config import SELF_HOSTED_ORG_ID, settings
from hailhq.core.providers.email.ses import SesEmailProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["ALERT_BYTES", "ALERT_FLAG", "Sender", "check_database_size", "ses_sender"]

logger = logging.getLogger(__name__)

ALERT_BYTES = 200 * 1024 * 1024
ALERT_FLAG = "database_size_alert_day"

# (subject, body) -> None. Raises on failure.
Sender = Callable[[str, str], Awaitable[None]]

_CLAIM = text("""
    INSERT INTO platform_flags (key, value, updated_at)
    VALUES (:key, :day, now())
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
    WHERE platform_flags.value < EXCLUDED.value
    RETURNING key
    """)


def ses_sender() -> Sender | None:
    """A sender through SES, or None when no mail domain is configured."""
    if not settings.hail_mail_base_domain or not settings.hail_support_email:
        return None

    async def send(subject: str, body: str) -> None:
        await SesEmailProvider().send_email(
            from_address=f"noreply@{settings.hail_mail_base_domain}",
            from_name="Hail",
            to_addresses=[settings.hail_support_email],
            subject=subject,
            body_text=body,
            body_html=None,
            headers={"Auto-Submitted": "auto-generated"},
            # Hail's own mail, no workspace's: tagged as the platform.
            organization_id=SELF_HOSTED_ORG_ID,
        )

    return send


def _message(size: int) -> tuple[str, str]:
    mb, limit = size / (1024 * 1024), ALERT_BYTES // (1024 * 1024)
    subject = f"Hail database is over {limit} MB"
    body = (
        f"The Hail database is {mb:.0f} MB, over the {limit} MB alert line.\n"
        "\n"
        "Check the database plan's storage quota before it is reached: a full "
        "database can refuse every request. Knowledge base data is usually "
        "the largest part; see docs/operations/knowledge-base.md for how to "
        "check sizes and lower limits.\n"
        "\n"
        "Hail sends this at most once a day while the size stays over the line.\n"
    )
    return subject, body


async def check_database_size(
    db: AsyncSession,
    send: Sender | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Email Hail when the database is over ``ALERT_BYTES`` and no alert
    went out today. Returns True when an email was sent. Never raises."""
    if send is None:
        return False
    day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    try:
        size = int(
            (
                await db.execute(text("SELECT pg_database_size(current_database())"))
            ).scalar_one()
        )
        if size <= ALERT_BYTES:
            await db.rollback()
            return False
        claimed = (
            await db.execute(_CLAIM, {"key": ALERT_FLAG, "day": day.isoformat()})
        ).first()
        if claimed is None:
            await db.rollback()
            return False
        await send(*_message(size))
        await db.commit()
        logger.warning("database size alert sent (%d bytes)", size)
        return True
    except Exception as exc:
        await db.rollback()
        logger.warning("database size check failed (%s)", type(exc).__name__)
        return False
