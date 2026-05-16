"""Vocabulary for ``calls.end_reason``.

The DB owns the canonical list as the ``call_end_reason`` Postgres ENUM
(see ``hail/api/migrations/versions/0003_call_end_reason_enum.py``). This
module is the Python view of the same vocabulary — a :class:`StrEnum` for
type-safe writes, plus a pre-built :class:`sqlalchemy.dialects.postgresql.ENUM`
the ORM column binds to.

Keep these in sync when adding values: extend the migration AND the enum
below, otherwise INSERTs will fail at the DB boundary.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy.dialects.postgresql import ENUM as PgEnum


class CallEndReason(StrEnum):
    """Reasons a call row transitions to a terminal status."""

    # happy path
    NORMAL_HANGUP = "normal_hangup"
    SOFT_CAP_REACHED = "soft_cap_reached"

    # SIP outcomes (callee side)
    USER_UNAVAILABLE = "user_unavailable"  # → status=no_answer
    USER_REJECTED = "user_rejected"  # → status=busy

    # SIP failures (transport / media / trunk)
    SIP_TRUNK_FAILURE = "sip_trunk_failure"
    CONNECTION_TIMEOUT = "connection_timeout"
    MEDIA_FAILURE = "media_failure"

    # API-side dispatch failures
    ROOM_CREATE_FAILED = "room_create_failed"
    AGENT_DISPATCH_FAILED = "agent_dispatch_failed"
    SIP_PARTICIPANT_FAILED = "sip_participant_failed"

    # voicebot-side anomalies
    AGENT_ERROR = "agent_error"
    WORKER_SHUTDOWN = "worker_shutdown"

    # backstop (sweeper force-released the call after max_duration + grace)
    SWEEPER_TIMEOUT = "sweeper_timeout"

    # catch-all
    UNKNOWN = "unknown"


# SQLAlchemy column type. ``create_type=True`` so ``Base.metadata.create_all``
# (used in tests against a fresh DB) emits the CREATE TYPE before the table
# CREATE; in production we never run create_all, so the migration's explicit
# CREATE TYPE is what actually owns this in real environments.
CallEndReasonDB = PgEnum(
    *(v.value for v in CallEndReason),
    name="call_end_reason",
    create_type=True,
)


__all__ = ["CallEndReason", "CallEndReasonDB"]
