"""Shared audit-log writer.

Used by every mutating route (calls, emails, email-domains). The write
runs in its own session (``session_scope()``) so a logging failure can
never roll back the user-facing operation — the audit trail is a
best-effort safety net, never a correctness guarantee.

Each entry pins:

* ``organization_id`` — scopes the trail; org admins read their own.
* ``api_key_id`` — the caller's key. ``None`` on self-host
  (``HAIL_API_KEY`` path) where no row exists in the auth backend's
  ``api_keys`` table.
* ``action`` — dotted verb (``call.create``, ``email.create``,
  ``email_domain.patch``). Use the same vocabulary as the
  ``resource_type`` so callers can filter on either.
* ``resource_type`` / ``resource_id`` — the row touched. ``resource_id``
  is ``None`` for denials logged before any row exists (e.g. the
  compliance-gate blocks — ``call.blocked`` / ``email.blocked`` — run
  before the Call/Email row is created).
* ``payload`` — arbitrary JSON; keep it small and grep-able. Don't
  dump message bodies or DKIM secrets here.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from hailhq.core.db import session_scope
from hailhq.core.models import AuditLog

logger = logging.getLogger(__name__)


def actor_of(principal: Any) -> tuple[UUID | None, str]:
    """(actor_user_id, actor_kind) for an authenticated principal."""
    if getattr(principal, "superadmin", False):
        return principal.user_id, "superadmin"
    if principal.api_key_id is not None:
        return principal.user_id, "api_key"
    if principal.user_id is not None:
        return principal.user_id, "user"
    return None, "system"


async def write_audit_log(
    organization_id: UUID,
    api_key_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: UUID | None,
    payload: dict[str, Any],
    *,
    actor_user_id: UUID | None = None,
    actor_kind: str | None = None,
) -> None:
    """Append an ``audit_log`` row in a fresh session. Never re-raises."""
    try:
        async with session_scope() as session:
            session.add(
                AuditLog(
                    organization_id=organization_id,
                    api_key_id=api_key_id,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    payload=payload,
                    actor_user_id=actor_user_id,
                    actor_kind=actor_kind,
                )
            )
            await session.commit()
    except Exception:  # pragma: no cover - logged, never re-raised
        logger.warning(
            "audit_log write failed for action=%s resource_id=%s",
            action,
            resource_id,
            exc_info=True,
        )


__all__ = ["actor_of", "write_audit_log"]
