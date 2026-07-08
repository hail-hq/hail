"""Shared consent-attestation gate for outbound sends (calls, emails).

Every outbound send must carry an explicit ``recipient_consent`` attestation
before any resource row is created — call ``enforce_consent`` first thing in
the route, before touching the DB. ``marketing`` sends are held to a
stricter bar (TCPA/CAN-SPAM): a bare boolean isn't enough, they also need a
documented ``consent_source``.
"""

from __future__ import annotations

from datetime import datetime

from hailhq.api.errors import unprocessable


def isoformat_or_none(value: datetime | None) -> str | None:
    """``value.isoformat()``, or ``None`` — for the audit-log payload's
    optional ``consent_obtained_at``, shared by calls.py and emails.py."""
    return value.isoformat() if value is not None else None


def enforce_consent(
    *,
    recipient_consent: bool,
    consent_source: str | None,
    message_type: str,
) -> None:
    """Raise 422 if the consent attestation doesn't meet the bar for ``message_type``.

    Call before any Call/Email row is created — reject, don't insert-then-roll-back.
    """
    if recipient_consent is not True:
        raise unprocessable(
            "recipient_consent must be true to send",
            loc=["body", "recipient_consent"],
        )
    if message_type == "marketing" and not (
        consent_source is not None and consent_source.strip()
    ):
        raise unprocessable(
            "marketing sends require a non-empty consent_source",
            loc=["body", "consent_source"],
        )


__all__ = ["enforce_consent", "isoformat_or_none"]
