"""Authenticated actor metadata, scoped to one request or background job."""

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import UUID

from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

IDENTITY_FIELDS = frozenset(
    {
        "organization_id",
        "organization_name",
        "user_id",
        "user_email",
        "actor_kind",
        "auth_kind",
        "api_key_id",
    }
)
_identity: ContextVar[dict[str, str] | None] = ContextVar(
    "hail_actor_identity", default=None
)


_CACHE_TTL_SECONDS = 300
_CACHE_MAX = 1024
_cache: dict[tuple, tuple[float, dict[str, str]]] = {}


def _cache_get(key: tuple) -> dict[str, str] | None:
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return dict(hit[1])
    return None


def _cache_put(key: tuple, identity: dict[str, str]) -> None:
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, dict(identity))


def get_identity() -> dict[str, str]:
    return dict(_identity.get() or {})


def set_identity(attributes: dict[str, Any]) -> None:
    identity = {
        key: str(value)
        for key, value in attributes.items()
        if key in IDENTITY_FIELDS and value is not None and value != ""
    }
    _identity.set(identity)
    trace.get_current_span().set_attributes(identity)


async def resolve_identity(
    db: "AsyncSession",
    organization_id: UUID,
    user_id: UUID | None = None,
    *,
    actor_kind: str = "agent"
) -> dict[str, str]:
    # Imported here: the MCP image has no DB driver stack (greenlet).
    from hailhq.core.models import Organization, User
    from sqlalchemy import select

    key = (organization_id, user_id, actor_kind)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    identity = {"organization_id": str(organization_id), "actor_kind": actor_kind}
    if user_id is not None:
        identity["user_id"] = str(user_id)
    try:
        # A failed telemetry lookup must not poison the business transaction.
        async with db.begin_nested():
            if organization_id.int == 0:
                identity["organization_name"] = "Self-hosted"
            else:
                name = await db.scalar(
                    select(Organization.name).where(Organization.id == organization_id)
                )
                if name:
                    identity["organization_name"] = name
            if user_id is not None:
                identity["user_id"] = str(user_id)
                email = await db.scalar(select(User.email).where(User.id == user_id))
                if email:
                    identity["user_email"] = email
    except Exception:
        logging.getLogger(__name__).warning("Actor telemetry lookup unavailable")
        return identity
    _cache_put(key, identity)
    return identity


class IdentitySpanProcessor(SpanProcessor):
    def on_start(self, span, parent_context=None) -> None:
        span.set_attributes(get_identity())

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        # This processor has no buffer; allow subsequent exporters to flush.
        return True


class IdentityMiddleware:
    """Reset identity at each ASGI request boundary, including errors."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        token = _identity.set({})
        try:
            await self.app(scope, receive, send)
        finally:
            _identity.reset(token)


@contextmanager
def identity_scope(attributes: dict[str, Any]):
    token = _identity.set({})
    try:
        set_identity(attributes)
        yield
    finally:
        _identity.reset(token)
