"""Authenticated actor metadata and trace tags, scoped to one request or job."""

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import UUID

from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor
from sqlalchemy import select

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
# Tags that say what a trace is about: channel, direction, the record ids, and
# the two parties. Set with tag_context(); copied onto every span and log.
TAG_FIELDS = frozenset(
    {
        "hail.channel",
        "hail.direction",
        "hail.agent_id",
        "hail.call_id",
        "hail.sms_id",
        "hail.email_id",
        "hail.from",
        "hail.to",
        "mcp.tool.name",
    }
)
_identity: ContextVar[dict[str, str] | None] = ContextVar(
    "hail_actor_identity", default=None
)
_tags: ContextVar[dict[str, str] | None] = ContextVar("hail_trace_tags", default=None)
_root: ContextVar[Any] = ContextVar("hail_trace_root", default=None)


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


def get_tags() -> dict[str, str]:
    return dict(_tags.get() or {})


def get_telemetry_attributes() -> dict[str, str]:
    """Identity and tags: what every span and log of this scope carries."""
    return {**get_identity(), **get_tags()}


def mask_party(value: str) -> str:
    """Hide the other party: a phone keeps its country code and last 4 digits
    ("+46 ***4567"), an email keeps only its domain ("***@example.com")."""
    value = value.strip()
    if "@" in value:
        return "***@" + value.rsplit("@", 1)[1]
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) <= 4:
        return "***"
    if value.startswith("+"):
        import phonenumbers

        try:
            code = phonenumbers.parse(value).country_code
        except phonenumbers.NumberParseException:
            code = None
        if code:
            return f"+{code} ***{digits[-4:]}"
    return f"***{digits[-4:]}"


def bind_root(span: Any = None) -> None:
    """Remember the span that tag_context() also tags (the trace's root)."""
    _root.set(span if span is not None else trace.get_current_span())


def tag_context(
    *,
    channel: str | None = None,
    direction: str | None = None,
    agent_id: Any = None,
    call_id: Any = None,
    sms_id: Any = None,
    email_id: Any = None,
    from_: str | None = None,
    to: str | None = None,
    tool: str | None = None,
) -> None:
    """Tag this scope, its current span and its root span.

    Spans started later get the tags from IdentitySpanProcessor. The Hail
    side (from on outbound, to on inbound) is kept in full; the other
    party is masked with mask_party(). With no direction, both are masked.
    Telemetry only: a failure here is logged, never raised.
    """
    try:
        _tag_context(
            channel=channel,
            direction=direction,
            agent_id=agent_id,
            call_id=call_id,
            sms_id=sms_id,
            email_id=email_id,
            from_=from_,
            to=to,
            tool=tool,
        )
    except Exception:
        logging.getLogger(__name__).warning("trace tagging failed", exc_info=True)


def tag_outbound_email(email: Any) -> None:
    """tag_context() for an outbound Email row; its first To is the other party."""
    tag_context(
        channel="email",
        direction="outbound",
        email_id=email.id,
        from_=email.from_address,
        to=email.to_addresses[0] if email.to_addresses else None,
    )


def _tag_context(
    *, channel, direction, agent_id, call_id, sms_id, email_id, from_, to, tool
):
    tags = get_tags()
    if direction is None:
        direction = tags.get("hail.direction")
    new = {
        "hail.channel": channel,
        "hail.direction": direction,
        "hail.agent_id": agent_id,
        "hail.call_id": call_id,
        "hail.sms_id": sms_id,
        "hail.email_id": email_id,
        "mcp.tool.name": tool,
    }
    if from_:
        new["hail.from"] = from_ if direction == "outbound" else mask_party(from_)
    if to:
        new["hail.to"] = to if direction == "inbound" else mask_party(to)
    tags.update({k: str(v) for k, v in new.items() if v is not None and v != ""})
    _tags.set(tags)
    labels = tuple(tags[k] for k in ("hail.channel", "hail.direction") if k in tags)
    current = trace.get_current_span()
    root = _root.get()
    for span in (current, root):
        if span is not None and span.is_recording():
            span.set_attributes(tags)
    target = root if root is not None and root.is_recording() else current
    if labels and target.is_recording():
        # Logfire shows these as tags in the live view and trace lists.
        target.set_attribute("logfire.tags", labels)


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
    actor_kind: str = "agent",
) -> dict[str, str]:
    # Imported here: the MCP image has no DB driver stack (greenlet).
    from hailhq.core.models import Organization, User

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
        span.set_attributes(get_telemetry_attributes())

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        # This processor has no buffer; allow subsequent exporters to flush.
        return True


class IdentityMiddleware:
    """Reset identity at each ASGI request boundary, including errors."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        token = _identity.set({})
        tags_token = _tags.set({})
        # Runs inside the instrumented request span, so that span is the root.
        root_token = _root.set(trace.get_current_span())
        try:
            await self.app(scope, receive, send)
        finally:
            _root.reset(root_token)
            _tags.reset(tags_token)
            _identity.reset(token)


@contextmanager
def identity_scope(attributes: dict[str, Any]):
    token = _identity.set({})
    # Tags set inside the scope end with it; tags from outside stay visible.
    tags_token = _tags.set(get_tags())
    try:
        set_identity(attributes)
        yield
    finally:
        _tags.reset(tags_token)
        _identity.reset(token)
