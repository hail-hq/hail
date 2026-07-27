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

from fastapi import Depends, Header, HTTPException, Request, Response
from fastapi import status as http_status
from hailhq.api.deps import Principal, get_current_principal
from hailhq.core.db import session_scope
from hailhq.core.models import IdempotencyKey
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

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


async def cache_failure(
    idem: IdempotencyContext | None, exc: HTTPException
) -> HTTPException:
    """Cache a pre-send failure under the idempotency key (when one was
    supplied), then hand the exception back to raise.

    Without this, an early 4xx leaves the idempotency row at the in-flight
    sentinel and every same-key retry 409s "still processing" until the
    row expires — failures must be stored just like successes (module
    docstring). Usage: ``raise await cache_failure(idem, exc)``.
    """
    if idem is not None:
        await idem.store(status_code=exc.status_code, body={"detail": exc.detail})
    return exc


def replay_cached(
    idem: IdempotencyContext, response: Response, *, resource_prefix: str
) -> tuple[UUID, dict[str, Any]]:
    """Shared replay choreography for the create routes.

    Re-raises a cached failure (status >= 400) with the Idempotency-Replay
    header; otherwise sets the Idempotency-Replay + Location headers and
    returns ``(cached_id, cached_body)`` for the route to audit and
    model-validate. ``resource_prefix`` is the Location path prefix, e.g.
    ``"/sms"``.
    """
    cached = idem.cached_response or {}
    if idem.cached_status and idem.cached_status >= 400:
        raise HTTPException(
            status_code=idem.cached_status,
            detail=cached.get("detail", "cached failure"),
            headers={"Idempotency-Replay": "true"},
        )
    cached_id = UUID(cached["id"])
    response.headers["Idempotency-Replay"] = "true"
    response.headers["Location"] = f"{resource_prefix}/{cached_id}"
    return cached_id, cached


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

    async def release(self) -> None:
        """Delete the in-flight sentinel so a same-key retry can re-attempt.

        For a deliberately-uncached *transient* pre-send failure (e.g. the
        shared-pool-exhausted 503): the sentinel this context committed on
        acquire would otherwise 409 "still processing" on every retry until
        the 24h TTL. Scoped to ``response_status == _IN_FLIGHT_STATUS`` so it
        is a no-op once a real response has been stored — it can never clobber
        a cached success or failure."""
        async with session_scope() as session:
            await session.execute(
                delete(IdempotencyKey).where(
                    IdempotencyKey.key == self.storage_key,
                    IdempotencyKey.response_status == _IN_FLIGHT_STATUS,
                )
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


async def idempotency_dep(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> IdempotencyContext | None:
    """FastAPI dep that gates any POST handler on an Idempotency-Key.

    Returns ``None`` when no header is present (pass-through). On bad JSON
    we also pass through so the route's Pydantic validation surfaces the
    422 — pre-empting it here would surface a less-helpful error.

    The logic is channel-agnostic (the storage key namespaces by
    ``organization_id``, never by route), so every idempotent POST mounts
    the same dependency.
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
        ctx = IdempotencyContext(
            storage_key=storage_key,
            request_hash=request_hash,
        )
        # Stashed so the app-level RequestValidationError handler (main.py)
        # can cache a body-validation 422 — those raise after this dep has
        # claimed the slot but before the route body runs, so the route's
        # own failure caching never gets a chance.
        request.state.idempotency_ctx = ctx
        return ctx

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

    ctx = IdempotencyContext(
        storage_key=storage_key,
        request_hash=request_hash,
        cached_response=dict(existing.response_body),
        cached_status=existing.response_status,
    )
    # Also stashed on replays: a cached body-validation 422 never reaches
    # the route (the same invalid body fails validation again), so the
    # main.py handler replays it from here.
    request.state.idempotency_ctx = ctx
    return ctx


__all__ = [
    "IdempotencyContext",
    "cache_failure",
    "hash_request_body",
    "idempotency_dep",
    "replay_cached",
]
