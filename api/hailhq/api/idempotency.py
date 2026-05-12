"""Idempotency support for ``POST /calls``.

The ``idempotency_keys`` table has ``key TEXT PRIMARY KEY`` — globally
unique. To prevent two organizations colliding on the same supplied header
value, we compose the stored key as ``f"{organization_id}:{supplied_key}"``;
:func:`_storage_key` is the single point of truth for that convention.

Concurrency: two concurrent requests with the same key race on a single
``INSERT ... ON CONFLICT (key) DO NOTHING RETURNING key``. Whichever
statement actually inserts the row owns the slot and runs the handler; the
other observes the existing row and either replays the cached response or
returns 409. The insert is the lock — no separate locking primitive needed.

Failures are cached just like successes: a retry with the same key replays
the failure rather than re-attempting. Clients who want a fresh attempt
must mint a new key (Stripe-style).

TODO(v1.x): expired-key garbage collection. The ``expires_at`` column
defaults to ``now() + interval '24 hours'`` but no process currently
sweeps stale rows. Add either a periodic worker (apscheduler / dramatiq)
or a ``pg_cron`` job before scaling.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request
from fastapi import status as http_status
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hailhq.core.db import session_scope
from hailhq.api.deps import Principal, get_current_principal
from hailhq.core.models import IdempotencyKey

# Sentinel `response_status` for an in-flight handler. Real HTTP responses
# are always >= 100, so 0 unambiguously means "another worker is running".
_IN_FLIGHT_STATUS = 0

_TTL = timedelta(hours=24)


def _storage_key(organization_id: UUID, supplied_key: str) -> str:
    return f"{organization_id}:{supplied_key}"


def hash_request_body(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON encoding of ``payload``.

    Sorted keys + tight separators keep the digest stable regardless of
    client formatting.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyContext:
    """Per-request state. ``cached_response`` is set only on a replay."""

    def __init__(
        self,
        storage_key: str,
        request_hash: str,
        cached_response: dict[str, Any] | None = None,
        cached_status: int | None = None,
    ) -> None:
        self.storage_key = storage_key
        self.request_hash = request_hash
        self.cached_response = cached_response
        self.cached_status = cached_status

    @property
    def is_replay(self) -> bool:
        return self.cached_response is not None

    async def store(self, status_code: int, body: dict[str, Any]) -> None:
        """Persist the final response so future requests replay it."""
        # TODO(idempotency): fold into the route's db session to save one
        # connection checkout + commit per request. Defer until pool pressure
        # is measurable.
        async with session_scope() as session:
            await session.execute(
                update(IdempotencyKey)
                .where(IdempotencyKey.key == self.storage_key)
                .values(response_status=status_code, response_body=body)
            )
            await session.commit()


async def _try_acquire_or_load(
    storage_key: str,
    organization_id: UUID,
    request_hash: str,
) -> IdempotencyKey | None:
    """Atomically claim the slot, or return the existing row.

    ``None`` means we inserted the in-flight sentinel and own the slot. A
    non-None return is the row another request already wrote; the caller
    decides whether to replay, return 409 in-flight, or 409 hash-mismatch.
    """
    expires_at = datetime.now(timezone.utc) + _TTL
    async with session_scope() as session:
        stmt = (
            pg_insert(IdempotencyKey)
            .values(
                key=storage_key,
                organization_id=organization_id,
                request_hash=request_hash,
                response_status=_IN_FLIGHT_STATUS,
                response_body={},
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=["key"])
            .returning(IdempotencyKey.key)
        )
        result = await session.execute(stmt)
        if result.scalar_one_or_none() is not None:
            await session.commit()
            return None

        existing = (
            await session.execute(
                select(IdempotencyKey).where(IdempotencyKey.key == storage_key)
            )
        ).scalar_one()
        session.expunge(existing)
        return existing


async def idempotency_for_post_calls(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> IdempotencyContext | None:
    """FastAPI dep that gates ``POST /calls`` on an Idempotency-Key.

    Returns ``None`` when no header is present (pass-through). On bad JSON
    we also pass through so the route's Pydantic validation surfaces the
    422 — pre-empting it here would surface a less-helpful error.
    """
    if idempotency_key is None:
        return None

    raw = await request.body()
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    request_hash = hash_request_body(parsed)
    storage_key = _storage_key(principal.organization_id, idempotency_key)

    existing = await _try_acquire_or_load(
        storage_key=storage_key,
        organization_id=principal.organization_id,
        request_hash=request_hash,
    )

    if existing is None:
        return IdempotencyContext(
            storage_key=storage_key,
            request_hash=request_hash,
        )

    if existing.request_hash != request_hash:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="idempotency key reused with a different request body",
        )

    if existing.response_status == _IN_FLIGHT_STATUS:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="request with this idempotency key is still processing",
        )

    return IdempotencyContext(
        storage_key=storage_key,
        request_hash=request_hash,
        cached_response=dict(existing.response_body),
        cached_status=existing.response_status,
    )


__all__ = [
    "IdempotencyContext",
    "hash_request_body",
    "idempotency_for_post_calls",
]
