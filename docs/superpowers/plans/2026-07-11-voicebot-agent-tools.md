# Voicebot Agent Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the voice agent in-call tools — `send_sms`, `send_email`, `end_call`, `list_contacts` — via a channel-agnostic registry in core, executed through HMAC-signed internal API routes so the full existing compliance/billing stack runs unchanged.

**Architecture:** A livekit-free `ToolSpec` registry in `core/hailhq/core/agent_tools/`; the voicebot adapts specs to LiveKit raw function tools at call start (filtered by per-org availability and a per-call opt-out). Send tools POST to new `/internal/agent/*` routes (shared-secret HMAC, same pattern as `routes/internal/dsar.py`), which reuse the public routes' send pipeline via two small extractions (`deliver_email`, `deliver_sms`).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Pydantic v2, livekit-agents (voicebot only), aiohttp, pytest.

**Spec:** `docs/superpowers/specs/2026-07-11-voicebot-agent-tools-design.md`

## Global Constraints

- Core must stay livekit-free: nothing under `core/` may import `livekit`.
- URLs are not strings: use `hailhq.core.urls.join_url` — never f-string `{base}/{path}`.
- No sentinel values: use `Optional[T]`/`None`, never `"shared"`/nil-UUID placeholders.
- No new env vars (`HAIL_INTERNAL_SECRET`, `HAIL_API_URL` already exist in `.env.example` and compose).
- The agent never handles raw addresses: tool parameters accept only a directory name or nothing (SMS is always the call counterpart); resolution happens server-side.
- Speakable strings: every tool/route outcome returned to the LLM is a short plain sentence (no codes, no jargon); policy denials stay vague (never reveal suppression-list membership).
- Conventional Commits; ruff + black clean; run each package's pytest before its commit.
- OpenAPI regen in the same PR as any route change: dump per `docs/contributing.md`, then `pnpm exec prettier --write openapi/openapi.yaml`, then `cd cli && make codegen`.
- Per-call agent send cap: 5, a code constant (`AGENT_SEND_CAP`), not a setting.

---

### Task 1: Core — `User` mirror + recipient directory

**Files:**

- Modify: `core/hailhq/core/models.py` (add `User` after `OrganizationMember`, ~line 55)
- Create: `core/hailhq/core/directory.py`
- Test: `core/tests/test_directory.py`

**Interfaces:**

- Consumes: existing `OrganizationMember` model (`core/hailhq/core/models.py:38`), `async_session` fixture from `core/tests`.
- Produces: `User` model (`id`, `name`, `email`, `created_at`); `DirectoryEntry(name, has_email, has_phone, source)`; `async list_directory(session, organization_id) -> list[DirectoryEntry]`; `async resolve_member_emails(session, organization_id, name) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_directory.py
"""Recipient-directory lookups: org scoping is the load-bearing property."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from hailhq.core.directory import list_directory, resolve_member_emails
from hailhq.core.models import OrganizationMember, User


async def _add_member(session, org_id, name, email):
    user = User(
        id=uuid.uuid4(),
        name=name,
        email=email,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=org_id,
            role="member",
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return user


async def test_list_directory_scoped_to_org(async_session):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    await _add_member(async_session, org_a, "Alice", "alice@a.test")
    await _add_member(async_session, org_b, "Bob", "bob@b.test")

    entries = await list_directory(async_session, org_a)
    assert [e.name for e in entries] == ["Alice"]
    assert entries[0].has_email is True
    assert entries[0].has_phone is False  # users table has no phone column
    assert entries[0].source == "member"


async def test_list_directory_empty_org(async_session):
    assert await list_directory(async_session, uuid.uuid4()) == []


async def test_resolve_member_emails_case_insensitive(async_session):
    org = uuid.uuid4()
    await _add_member(async_session, org, "Sarah Chen", "sarah@x.test")
    assert await resolve_member_emails(async_session, org, "  sarah chen ") == [
        "sarah@x.test"
    ]


async def test_resolve_member_emails_never_crosses_orgs(async_session):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    await _add_member(async_session, org_a, "Alice", "alice@a.test")
    assert await resolve_member_emails(async_session, org_b, "Alice") == []


async def test_resolve_member_emails_returns_all_matches(async_session):
    org = uuid.uuid4()
    await _add_member(async_session, org, "Sam", "sam1@x.test")
    await _add_member(async_session, org, "sam", "sam2@x.test")
    assert sorted(await resolve_member_emails(async_session, org, "Sam")) == [
        "sam1@x.test",
        "sam2@x.test",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_directory.py -v`
Expected: FAIL — `ImportError: cannot import name 'list_directory'` (and `User`).

- [ ] **Step 3: Add the `User` mirror to models.py**

Insert directly after the `OrganizationMember` class (~line 55), matching its style:

```python
class User(Base):
    """Read-only mirror of the website's ``users`` table (better-auth).

    Same posture as :class:`OrganizationMember`: the website owns the
    schema; hail only reads it. Columns verified 2026-07-11 against
    hail-website's better-auth migrations (users: id / name / email /
    email_verified / image / timestamps); only the columns hail reads
    are mapped.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)
```

- [ ] **Step 4: Create `core/hailhq/core/directory.py`**

```python
"""Recipient directory for voicebot agent tools.

One lookup used by BOTH the voicebot (``list_contacts``) and the API's
internal agent-send routes (recipient resolution), so org-scoping rules
live in exactly one place.

Sources: org members today (website-mirrored ``users`` joined through
``members``); the contacts table (separate workstream) joins as a second
source when it lands.

Cross-org isolation rule (load-bearing): every query starts from the
call's ``organization_id`` — a user is reachable only via membership in
that org. Raw addresses leave this module only through
``resolve_member_emails``, which only the API service calls; the voicebot
sees names and channel presence, never addresses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import OrganizationMember, User


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    has_email: bool
    has_phone: bool
    source: str  # "member" (future: "contact")


async def list_directory(
    session: AsyncSession, organization_id: uuid.UUID
) -> list[DirectoryEntry]:
    """All directory entries for one org, name-sorted, addresses omitted."""
    stmt = (
        select(User.name)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == organization_id)
        .order_by(User.name)
    )
    names = (await session.execute(stmt)).scalars().all()
    # Members come from the better-auth users table, which has no phone
    # column — members are email-only recipients until the contacts source.
    return [
        DirectoryEntry(name=n, has_email=True, has_phone=False, source="member")
        for n in names
    ]


async def resolve_member_emails(
    session: AsyncSession, organization_id: uuid.UUID, name: str
) -> list[str]:
    """Emails of members matching ``name`` case-insensitively.

    Returns every match — the caller owns the 0-match and >1-match
    policies (the internal route refuses ambiguous sends).
    """
    stmt = (
        select(User.email)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(
            OrganizationMember.organization_id == organization_id,
            func.lower(User.name) == name.strip().lower(),
        )
    )
    return list((await session.execute(stmt)).scalars().all())


__all__ = ["DirectoryEntry", "list_directory", "resolve_member_emails"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd core && uv run pytest tests/test_directory.py -v`
Expected: 5 PASS. Also run `cd core && uv run pytest` — no regressions (the new `User` model must not break `Base.metadata.create_all` fixtures).

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/models.py core/hailhq/core/directory.py core/tests/test_directory.py
git commit -m "feat(core): users mirror + org-scoped recipient directory"
```

---

### Task 2: Core — agent_tools package: spec, client, end_call, list_contacts, registry

**Files:**

- Create: `core/hailhq/core/agent_tools/__init__.py` (empty docstring module)
- Create: `core/hailhq/core/agent_tools/spec.py`
- Create: `core/hailhq/core/agent_tools/client.py`
- Create: `core/hailhq/core/agent_tools/end_call.py`
- Create: `core/hailhq/core/agent_tools/list_contacts.py`
- Create: `core/hailhq/core/agent_tools/registry.py` (send tools added in Task 3)
- Test: `core/tests/test_agent_tools.py`

**Interfaces:**

- Consumes: `list_directory` from Task 1; `hmac_signing.sign(body: bytes, secret: str) -> str`; `urls.join_url`; `db.session_scope`.
- Produces:
  - `ToolContext(call_id: UUID, organization_id: UUID, api: AgentApiClient | None, hangup: Callable[[], Awaitable[None]] | None)`
  - `ToolSpec(name, description, parameters, risk_tier, is_available, execute)` where `is_available(organization_id: UUID, session: AsyncSession) -> bool` and `execute(ctx: ToolContext, args: dict) -> str` (a speakable sentence, never raises for expected failures)
  - `AgentApiClient(base_url, secret)` with `async post(path, payload) -> dict` and `async aclose()`
  - `registry.all_tools() -> tuple[ToolSpec, ...]`

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_agent_tools.py
"""Agent-tool registry: shape, availability, and executor behavior."""

from __future__ import annotations

import uuid

from hailhq.core.agent_tools.registry import all_tools
from hailhq.core.agent_tools.spec import ToolContext


def _ctx(**overrides):
    defaults = dict(
        call_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        api=None,
        hangup=None,
    )
    defaults.update(overrides)
    return ToolContext(**defaults)


def test_registry_names_and_tiers():
    tools = {t.name: t for t in all_tools()}
    assert set(tools) == {"end_call", "list_contacts", "send_sms", "send_email"}
    assert tools["end_call"].risk_tier == "session_control"
    assert tools["list_contacts"].risk_tier == "read_only"
    assert tools["send_sms"].risk_tier == "outbound_send"
    assert tools["send_email"].risk_tier == "outbound_send"


def test_every_parameter_schema_is_object_typed():
    for t in all_tools():
        assert t.parameters["type"] == "object"
        assert "properties" in t.parameters


def test_no_tool_schema_accepts_raw_addresses():
    # The agent must never hold a phone number or email address parameter.
    for t in all_tools():
        for prop in t.parameters["properties"]:
            assert "phone" not in prop
            assert "number" not in prop
            assert "address" not in prop
            assert prop != "email"


async def test_end_call_invokes_hangup():
    fired = []

    async def hangup():
        fired.append(True)

    tools = {t.name: t for t in all_tools()}
    spoken = await tools["end_call"].execute(_ctx(hangup=hangup), {})
    assert fired == [True]
    assert isinstance(spoken, str) and spoken


async def test_end_call_without_hangup_degrades():
    tools = {t.name: t for t in all_tools()}
    spoken = await tools["end_call"].execute(_ctx(hangup=None), {})
    assert isinstance(spoken, str) and spoken


async def test_end_call_and_list_contacts_always_available(async_session):
    tools = {t.name: t for t in all_tools()}
    org = uuid.uuid4()
    assert await tools["end_call"].is_available(org, async_session) is True
    assert await tools["list_contacts"].is_available(org, async_session) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_agent_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: hailhq.core.agent_tools`.

- [ ] **Step 3: Create the package**

`core/hailhq/core/agent_tools/__init__.py`:

```python
"""Voicebot agent tools — channel-agnostic registry.

Spec: docs/superpowers/specs/2026-07-11-voicebot-agent-tools-design.md.
One module per tool; the voicebot adapts :class:`ToolSpec` entries to
LiveKit function tools. This package must stay livekit-free.
"""
```

`core/hailhq/core/agent_tools/spec.py`:

```python
"""ToolSpec / ToolContext — the contract between core tools and the voicebot.

``execute`` returns a short plain sentence the agent speaks. Expected
failures (unavailable channel, denied send) come back as speakable
sentences, not exceptions; the voicebot wrapper catches anything else.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.agent_tools.client import AgentApiClient

RiskTier = Literal["read_only", "session_control", "outbound_send"]


@dataclass
class ToolContext:
    """Capability handles the voicebot supplies per call.

    ``api`` is None when HAIL_INTERNAL_SECRET is unset (send tools are
    unavailable then); ``hangup`` is None outside a live session.
    """

    call_id: uuid.UUID
    organization_id: uuid.UUID
    api: AgentApiClient | None
    hangup: Callable[[], Awaitable[None]] | None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema, type: object
    risk_tier: RiskTier
    is_available: Callable[[uuid.UUID, AsyncSession], Awaitable[bool]]
    execute: Callable[[ToolContext, dict[str, Any]], Awaitable[str]]


__all__ = ["RiskTier", "ToolContext", "ToolSpec"]
```

`core/hailhq/core/agent_tools/client.py`:

```python
"""HMAC-signed HTTP client for voicebot → API internal agent routes.

Same signing scheme as everywhere else in this repo
(``hailhq.core.hmac_signing``): HMAC-SHA256 over the raw request body in
``X-Hail-Signature``. One retry on timeout is safe because every payload
carries its own ``tool_invocation_id`` and the routes dedupe on it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from hailhq.core import hmac_signing
from hailhq.core.urls import join_url

logger = logging.getLogger("hailhq.core.agent_tools")

_TIMEOUT_SECONDS = 10.0


class AgentApiClient:
    def __init__(self, base_url: str, secret: str) -> None:
        self._base_url = base_url
        self._secret = secret
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
            )
        return self._session

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Hail-Signature": hmac_signing.sign(body, self._secret),
        }
        url = join_url(self._base_url, path)
        last_exc: Exception = RuntimeError("unreachable")
        for _attempt in range(2):
            try:
                session = self._get_session()
                async with session.post(url, data=body, headers=headers) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except asyncio.TimeoutError as exc:
                last_exc = exc
                continue
        raise last_exc

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


__all__ = ["AgentApiClient"]
```

`core/hailhq/core/agent_tools/end_call.py`:

```python
"""end_call — hang up gracefully once the call's goal is met.

session_control tier: purely local (the voicebot's ``hangup`` handle),
no API call, affects only its own call. The voicebot wrapper waits for
the agent's pre-tool speech to finish playing before this runs.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.agent_tools.spec import ToolContext, ToolSpec


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    return True


async def _execute(ctx: ToolContext, _args: dict[str, Any]) -> str:
    if ctx.hangup is None:
        return "I can't end the call right now."
    await ctx.hangup()
    return "Call ended."


SPEC = ToolSpec(
    name="end_call",
    description=(
        "End this phone call. Use only after the call's goal is met, or when "
        "the other party asks to end the call. Say your goodbye out loud "
        "BEFORE calling this tool — it hangs up immediately after."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    risk_tier="session_control",
    is_available=_always,
    execute=_execute,
)
```

`core/hailhq/core/agent_tools/list_contacts.py`:

```python
"""list_contacts — read-only directory listing.

Returns names and channel presence only; raw addresses never reach the
LLM (they could be read aloud or leak into the stored transcript).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.db import session_scope
from hailhq.core.directory import list_directory


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    return True


async def _execute(ctx: ToolContext, _args: dict[str, Any]) -> str:
    async with session_scope() as session:
        entries = await list_directory(session, ctx.organization_id)
    if not entries:
        return "There are no contacts available."
    lines = []
    for e in entries:
        channels = [
            label
            for label, present in (("email", e.has_email), ("text", e.has_phone))
            if present
        ]
        lines.append(f"{e.name} (reachable by {' and '.join(channels)})")
    return "Available contacts: " + "; ".join(lines) + "."


SPEC = ToolSpec(
    name="list_contacts",
    description=(
        "List the people you may send messages to: members of the "
        "organization that set up this call. Shows names and whether each "
        "person can receive email or text — never their actual addresses."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    risk_tier="read_only",
    is_available=_always,
    execute=_execute,
)
```

`core/hailhq/core/agent_tools/registry.py` — create with the two tools that exist so far; Task 3 adds the send tools (the Task 2 test for all four names will still fail until Task 3, so **run only the non-registry tests now if executing tasks strictly in order — or implement Tasks 2 and 3 before running the full test file**. Recommended: treat Steps 1–2 here as covering both tasks and run the full file at the end of Task 3):

```python
"""The agent-tool registry. New modality = add one module + one line here."""

from __future__ import annotations

from hailhq.core.agent_tools import end_call, list_contacts, send_email, send_sms
from hailhq.core.agent_tools.spec import ToolSpec


def all_tools() -> tuple[ToolSpec, ...]:
    return (end_call.SPEC, list_contacts.SPEC, send_sms.SPEC, send_email.SPEC)


__all__ = ["all_tools"]
```

- [ ] **Step 4: Proceed to Task 3 (registry imports send modules; the test file passes only after Task 3). Do not commit yet.**

---

### Task 3: Core — send_sms and send_email tool specs

**Files:**

- Create: `core/hailhq/core/agent_tools/send_sms.py`
- Create: `core/hailhq/core/agent_tools/send_email.py`
- Test: append to `core/tests/test_agent_tools.py`

**Interfaces:**

- Consumes: `ToolSpec`/`ToolContext`/`AgentApiClient` (Task 2), `PhoneNumber`/`EmailDomain` models, `settings.hail_internal_secret`.
- Produces: `send_sms.SPEC`, `send_email.SPEC`. Their `execute` POSTs to `/internal/agent/send-sms` / `/internal/agent/send-email` (Task 5) with payload keys: `call_id`, `tool_invocation_id`, plus `body` (sms) or `recipient_name`/`subject`/`body_text` (email); they return the route's `spoken` string.

- [ ] **Step 1: Append failing tests**

```python
# append to core/tests/test_agent_tools.py
import uuid as _uuid
from datetime import datetime, timezone

from hailhq.core.config import settings
from hailhq.core.models import EmailDomain, PhoneNumber


class FakeApi:
    """Records posts; returns a canned internal-route response."""

    def __init__(self, spoken="Done.", ok=True):
        self.posts = []
        self._resp = {"ok": ok, "spoken": spoken}

    async def post(self, path, payload):
        self.posts.append((path, payload))
        return self._resp


async def test_send_sms_available_only_with_sms_number(async_session, monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", "s3cret")
    tools = {t.name: t for t in all_tools()}
    org = _uuid.uuid4()
    assert await tools["send_sms"].is_available(org, async_session) is False

    async_session.add(
        PhoneNumber(
            organization_id=org,
            e164="+14155550100",
            capabilities=["voice", "sms"],
            provisioning_state="active",
            is_pool=False,
        )
    )
    await async_session.commit()
    assert await tools["send_sms"].is_available(org, async_session) is True


async def test_send_tools_unavailable_without_internal_secret(
    async_session, monkeypatch
):
    monkeypatch.setattr(settings, "hail_internal_secret", "")
    tools = {t.name: t for t in all_tools()}
    org = _uuid.uuid4()
    assert await tools["send_sms"].is_available(org, async_session) is False
    assert await tools["send_email"].is_available(org, async_session) is False


async def test_send_email_available_only_with_verified_domain(
    async_session, monkeypatch
):
    monkeypatch.setattr(settings, "hail_internal_secret", "s3cret")
    tools = {t.name: t for t in all_tools()}
    org = _uuid.uuid4()
    assert await tools["send_email"].is_available(org, async_session) is False

    async_session.add(
        EmailDomain(
            organization_id=org,
            kind="custom",
            domain="mail.example.test",
            verification_status="verified",
        )
    )
    await async_session.commit()
    assert await tools["send_email"].is_available(org, async_session) is True


async def test_send_sms_posts_call_scoped_payload():
    api = FakeApi(spoken="Text sent.")
    tools = {t.name: t for t in all_tools()}
    ctx = _ctx(api=api)
    spoken = await tools["send_sms"].execute(ctx, {"body": "Your code is 42."})
    assert spoken == "Text sent."
    path, payload = api.posts[0]
    assert path == "/internal/agent/send-sms"
    assert payload["call_id"] == str(ctx.call_id)
    assert payload["body"] == "Your code is 42."
    assert _uuid.UUID(payload["tool_invocation_id"])  # parseable, fresh per call


async def test_send_email_posts_recipient_name_not_address():
    api = FakeApi(spoken="Email sent.")
    tools = {t.name: t for t in all_tools()}
    ctx = _ctx(api=api)
    spoken = await tools["send_email"].execute(
        ctx,
        {"recipient_name": "Sarah Chen", "subject": "Summary", "body_text": "Hi."},
    )
    assert spoken == "Email sent."
    path, payload = api.posts[0]
    assert path == "/internal/agent/send-email"
    assert payload["recipient_name"] == "Sarah Chen"
    assert "@" not in str(payload.get("recipient_name"))


async def test_send_tools_degrade_without_api_client():
    tools = {t.name: t for t in all_tools()}
    assert "not available" in (
        await tools["send_sms"].execute(_ctx(api=None), {"body": "x"})
    )
    assert "not available" in (
        await tools["send_email"].execute(
            _ctx(api=None),
            {"recipient_name": "A", "subject": "s", "body_text": "b"},
        )
    )
```

Note: check `PhoneNumber` required columns in `core/hailhq/core/models.py:277` before running — add any additional non-nullable fields (e.g. `provider`) the constructor needs.

- [ ] **Step 2: Run to verify failure**

Run: `cd core && uv run pytest tests/test_agent_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: ...send_sms` (imported by registry).

- [ ] **Step 3: Create `send_sms.py`**

```python
"""send_sms — text the person on this call. outbound_send tier.

The only possible recipient is the call counterpart: org members carry
no phone numbers (the better-auth users table has none), and raw numbers
are never accepted as parameters. Recipient resolution, the compliance
gate, the per-call cap, and billing all run server-side in
``/internal/agent/send-sms``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.config import settings
from hailhq.core.models import PhoneNumber

MAX_BODY_CHARS = 480  # ~3 SMS segments; matches the internal route's cap

_UNAVAILABLE = "Text messaging is not available right now."


async def _is_available(organization_id: uuid.UUID, session: AsyncSession) -> bool:
    if not settings.hail_internal_secret:
        return False
    stmt = (
        select(PhoneNumber.id)
        .where(
            PhoneNumber.organization_id == organization_id,
            PhoneNumber.provisioning_state == "active",
            PhoneNumber.capabilities.any("sms"),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.api is None:
        return _UNAVAILABLE
    resp = await ctx.api.post(
        "/internal/agent/send-sms",
        {
            "call_id": str(ctx.call_id),
            "tool_invocation_id": str(uuid.uuid4()),
            "body": str(args.get("body", ""))[:MAX_BODY_CHARS],
        },
    )
    return str(resp.get("spoken", "Sorry, that didn't work."))


SPEC = ToolSpec(
    name="send_sms",
    description=(
        "Send a text message to the person on this call, at the number being "
        "called. You cannot text anyone else. Before sending, say exactly "
        "what the message will say and get their confirmation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "body": {
                "type": "string",
                "description": "The text message to send. Keep it short.",
                "maxLength": MAX_BODY_CHARS,
            }
        },
        "required": ["body"],
    },
    risk_tier="outbound_send",
    is_available=_is_available,
    execute=_execute,
)
```

- [ ] **Step 4: Create `send_email.py`**

```python
"""send_email — email an org member from the directory. outbound_send tier.

Recipients are directory names only (see ``list_contacts``); dictated
addresses are unverifiable over voice, so raw addresses are never
accepted. Resolution, gate, cap, disclosure footer, and billing run
server-side in ``/internal/agent/send-email``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.config import settings
from hailhq.core.models import EmailDomain

MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 5000

_UNAVAILABLE = "Email is not available right now."


async def _is_available(organization_id: uuid.UUID, session: AsyncSession) -> bool:
    if not settings.hail_internal_secret:
        return False
    stmt = (
        select(EmailDomain.id)
        .where(
            EmailDomain.organization_id == organization_id,
            EmailDomain.verification_status == "verified",
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.api is None:
        return _UNAVAILABLE
    resp = await ctx.api.post(
        "/internal/agent/send-email",
        {
            "call_id": str(ctx.call_id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": str(args.get("recipient_name", "")).strip(),
            "subject": str(args.get("subject", ""))[:MAX_SUBJECT_CHARS],
            "body_text": str(args.get("body_text", ""))[:MAX_BODY_CHARS],
        },
    )
    return str(resp.get("spoken", "Sorry, that didn't work."))


SPEC = ToolSpec(
    name="send_email",
    description=(
        "Send an email to a member of the organization's team directory "
        "(use list_contacts to see who). You cannot email arbitrary "
        "addresses — only directory names. Before sending, say the "
        "recipient's name and summarize the content, and get confirmation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "recipient_name": {
                "type": "string",
                "description": "The directory name of the recipient.",
            },
            "subject": {"type": "string", "maxLength": MAX_SUBJECT_CHARS},
            "body_text": {
                "type": "string",
                "description": "Plain-text email body.",
                "maxLength": MAX_BODY_CHARS,
            },
        },
        "required": ["recipient_name", "subject", "body_text"],
    },
    risk_tier="outbound_send",
    is_available=_is_available,
    execute=_execute,
)
```

- [ ] **Step 5: Run all core tests**

Run: `cd core && uv run pytest tests/test_agent_tools.py tests/test_directory.py -v && uv run pytest`
Expected: all PASS.

- [ ] **Step 6: Lint and commit Tasks 2+3 together**

```bash
cd core && uv run ruff check --fix . && uv run black .
git add core/hailhq/core/agent_tools/ core/tests/test_agent_tools.py
git commit -m "feat(core): agent-tool registry with end_call, list_contacts, send_sms, send_email"
```

---

### Task 4: API — extract `deliver_email` and publicize sender resolution in emails.py

**Files:**

- Modify: `api/hailhq/api/routes/emails.py`
- Test: existing `api/tests/test_emails_api.py` (behavior must not change; no new tests)

**Interfaces:**

- Produces (consumed by Task 5): in `hailhq.api.routes.emails` —
  - `async resolve_sender(db, organization_id, explicit_from: str | None) -> EmailDomain` (rename of `_resolve_sender`; raises HTTPException 422 when no usable domain)
  - `from_address_for(sd: EmailDomain, explicit: str | None) -> str` (rename of `_from_address_for`)
  - `async deliver_email(db: AsyncSession, email_provider: EmailProvider, email: Email) -> str | None` — wire-sends one queued `Email` row (footer + disclosure + unsubscribe header), reconciles status, writes the usage event on success. Returns `None` on success, the exception class name on transport failure. Never raises.

- [ ] **Step 1: Rename the two helpers**

In `api/hailhq/api/routes/emails.py`: rename `_resolve_sender` → `resolve_sender` and `_from_address_for` → `from_address_for` (definition + all in-module call sites; grep the repo to confirm no other callers). Add both to `__all__`.

- [ ] **Step 2: Extract `deliver_email`**

Move the block of `create_email` that starts at the `wire_text, wire_html = append_footer(...)` line and ends with the `await _write_usage_event(...)` call (currently ~lines 384–467) into a new module-level function. The moved code is unchanged except: `principal.organization_id` → `email.organization_id`, and failures return instead of raise:

```python
async def deliver_email(
    db: AsyncSession,
    email_provider: EmailProvider,
    email: Email,
) -> str | None:
    """Wire-send one queued Email row and reconcile its status.

    Shared by POST /emails and the internal agent-send route (spec:
    docs/superpowers/specs/2026-07-11-voicebot-agent-tools-design.md).
    Appends the branding footer + AI disclosure, mints the one-click
    unsubscribe header, sends, and writes sent/failed back. Bills the
    flat 1¢ usage event on success. Returns None on success or the
    exception class name on transport failure — the caller owns HTTP
    semantics (502 + audit for the public route, a spoken sentence for
    the agent route). Never raises for provider errors.
    """
    wire_text, wire_html = append_footer(
        email.body_text, email.body_html, label=FOOTER_SENT
    )
    wire_text, wire_html = append_disclosure(wire_text, wire_html)
    unsubscribe_url = build_unsubscribe_url(
        email.to_addresses[0], email.organization_id
    )
    try:
        result = await email_provider.send_email(
            from_address=email.from_address,
            to_addresses=email.to_addresses,
            subject=email.subject,
            body_text=wire_text,
            body_html=wire_html,
            cc=email.cc_addresses,
            bcc=email.bcc_addresses,
            reply_to=email.reply_to,
            headers={
                "List-Unsubscribe": f"<{unsubscribe_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        )
    except Exception as exc:
        logger.warning(
            "ses send_email failed for email_id=%s", email.id, exc_info=True
        )
        now = datetime.now(timezone.utc)
        await db.execute(
            update(Email)
            .where(Email.id == email.id)
            .values(status="failed", end_reason=type(exc).__name__, failed_at=now)
        )
        await db.commit()
        return type(exc).__name__

    now = datetime.now(timezone.utc)
    await db.execute(
        update(Email)
        .where(Email.id == email.id)
        .values(
            status="sent",
            provider_message_id=result.provider_message_id,
            sent_at=now,
        )
    )
    record_sent_event(
        db, email_id=email.id, organization_id=email.organization_id, occurred_at=now
    )
    await db.commit()
    await db.refresh(email)

    # Flat 1¢ per send regardless of recipient count.
    await _write_usage_event(
        organization_id=email.organization_id,
        units=1,
        ref=f"email:{email.id}",
    )
    return None
```

Then rewrite the tail of `create_email` to delegate:

```python
    err = await deliver_email(db, email_provider, email)
    if err is not None:
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="email.send_failed",
            resource_type="email",
            resource_id=email.id,
            payload={"end_reason": err},
        )
        if idem is not None:
            await idem.store(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                body={"detail": _SEND_FAILED_DETAIL},
            )
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=_SEND_FAILED_DETAIL,
        )

    response.headers["Location"] = f"/emails/{email.id}"
    email_response = EmailResponse.model_validate(email)

    if idem is not None:
        await idem.store(
            status_code=http_status.HTTP_201_CREATED,
            body=email_response.model_dump(mode="json"),
        )

    return email_response
```

Compare the moved code line-by-line against the original before deleting it — the only intended differences are the two listed above (`principal.organization_id` → `email.organization_id`; return-instead-of-raise). Preserve the `raise ... from exc` chain's audit/idempotency behavior exactly as shown.

- [ ] **Step 3: Run the email API tests**

Run: `cd api && uv run pytest tests/test_emails_api.py -v`
Expected: all PASS unchanged (pure refactor). If a test asserted on the 502 path's audit payload, the behavior is identical.

- [ ] **Step 4: Commit**

```bash
git add api/hailhq/api/routes/emails.py
git commit -m "refactor(api): extract deliver_email for reuse by the internal agent route"
```

---

### Task 5: API — extract `deliver_sms` in sms.py

**Files:**

- Modify: `api/hailhq/api/routes/sms.py`
- Test: existing `api/tests/test_sms_api.py` (pure refactor; no new tests)

**Interfaces:**

- Produces (consumed by Task 6): `async deliver_sms(db: AsyncSession, provider: SmsProvider, sms: Sms) -> str | None` — provider send + status reconciliation + SmsEvent + usage billing. Returns `None` when the carrier accepted; `"provider_error"` on transport failure; the carrier `error_code` (or `"carrier_rejected"`) on carrier rejection. Never raises.

- [ ] **Step 1: Extract the function**

Move the block of `create_sms` from `try: result = await provider.send_sms(...)` through the `write_usage_event(...)` call into:

```python
async def deliver_sms(db: AsyncSession, provider: SmsProvider, sms: Sms) -> str | None:
    """Wire-send one queued Sms row and reconcile its status.

    Shared by POST /sms and the internal agent-send route. Returns None
    when the carrier accepted (status='sent', usage billed),
    'provider_error' on transport failure, or the carrier error code on
    rejection. Never raises — the caller owns HTTP semantics.
    """
    try:
        result = await provider.send_sms(
            from_e164=sms.from_e164, to_e164=sms.to_e164, body=sms.body
        )
    except Exception:
        logger.warning("sms send failed for sms_id=%s", sms.id, exc_info=True)
        sms.status = "failed"
        db.add(
            SmsEvent(
                sms_id=sms.id,
                organization_id=sms.organization_id,
                kind="state_change",
                payload={
                    "from": "queued",
                    "to": "failed",
                    "reason": "provider_error",
                },
            )
        )
        await db.commit()
        return "provider_error"

    # (moved verbatim from create_sms: carrier_rejected computation, status
    # + sid/segment/error_code assignment, sent_at stamp, SmsEvent append,
    # commit)
    ...

    if carrier_rejected:
        return result.error_code or "carrier_rejected"

    await write_usage_event(
        organization_id=sms.organization_id,
        channel="sms",
        units=sms.segment_count,
        ref=f"sms:{sms.id}",
    )
    return None
```

(The `...` above stands for the existing lines moved without edits — the plan elides them because they move verbatim from `create_sms` at `api/hailhq/api/routes/sms.py:176-230`; `principal.organization_id` → `sms.organization_id` in the usage call is the only substitution.)

`create_sms` then delegates:

```python
    err = await deliver_sms(db, provider, sms)
    if err == "provider_error":
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail=_SMS_SEND_FAILED_DETAIL,
            ),
        )
    # carrier rejection: row already reconciled to failed; fall through and
    # return the SmsResponse exactly as before.
```

Note the original transport-failure path raised `... from exc`; the exception no longer crosses the boundary, so the `from exc` clause is dropped — that is the only observable-behavior-adjacent change and it only affects traceback chaining, not responses.

- [ ] **Step 2: Run the SMS API tests**

Run: `cd api && uv run pytest tests/test_sms_api.py -v`
Expected: all PASS unchanged.

- [ ] **Step 3: Commit**

```bash
git add api/hailhq/api/routes/sms.py
git commit -m "refactor(api): extract deliver_sms for reuse by the internal agent route"
```

---

### Task 6: API — internal agent-send routes

**Files:**

- Create: `api/hailhq/api/routes/internal/agent.py`
- Modify: `core/hailhq/core/billing.py` (add `CALL_META_BILLED = "billed"` constant + `__all__` entry)
- Modify: `api/hailhq/api/main.py` (include the router next to the existing internal routers — copy how `routes/internal/dsar.py`'s router is registered, including any `include_in_schema` posture; check whether `/internal/dsar` appears in `openapi/openapi.yaml` and match)
- Test: `api/tests/test_internal_agent_send.py`

**Interfaces:**

- Consumes: `verify_internal_request` (`routes/internal/auth.py`), `check_sms_allowed`/`check_email_allowed`, `has_funds` (`api/hailhq/api/funds.py`), `resolve_org_number` (`api/hailhq/api/numbers.py`), `resolve_sender`/`from_address_for`/`deliver_email` (Task 4), `deliver_sms` (Task 5), `resolve_member_emails` (Task 1), `write_audit_log`, `get_sms_provider` (`routes/sms.py`), the email-provider dependency used by `create_email` (imported from wherever `get_email_provider` lives — see `routes/emails.py` imports).
- Produces: `POST /internal/agent/send-sms` and `POST /internal/agent/send-email`, both always HTTP 200 (except 401/503 auth) with body `{"ok": bool, "spoken": str}`. Request payload shapes match Task 3's clients exactly.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_internal_agent_send.py
"""Internal agent-send routes: auth, call gating, cap, dedupe, org scoping."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from hailhq.core import hmac_signing
from hailhq.core.billing import CALL_META_BILLED
from hailhq.core.config import settings
from hailhq.core.models import (
    Call,
    Email,
    EmailDomain,
    OrganizationMember,
    PhoneNumber,
    Sms,
    User,
)

HMAC_SECRET = "test-internal-secret"


def _signed(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Hail-Signature": hmac_signing.sign(body, HMAC_SECRET),
    }


@pytest.fixture(autouse=True)
def _internal_secret(monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", HMAC_SECRET)


async def _insert_live_call(db, org_id, *, to_e164="+14155550123", billed=False):
    # Follow test_calls_api.py's Call-row insertion helpers for required
    # columns (from_number_id needs a PhoneNumber row).
    number = PhoneNumber(
        organization_id=org_id,
        e164="+14155550100",
        capabilities=["voice", "sms"],
        provisioning_state="active",
        is_pool=False,
    )
    db.add(number)
    await db.flush()
    call = Call(
        organization_id=org_id,
        from_number_id=number.id,
        from_e164=number.e164,
        to_e164=to_e164,
        status="in_progress",
        voice_config={},
        metadata_={CALL_META_BILLED: billed},
    )
    db.add(call)
    await db.commit()
    return call


def _sms_payload(call_id, body="hello"):
    return json.dumps(
        {
            "call_id": str(call_id),
            "tool_invocation_id": str(uuid.uuid4()),
            "body": body,
        }
    ).encode()


async def test_send_sms_rejects_bad_signature(client: httpx.AsyncClient, db_session):
    body = _sms_payload(uuid.uuid4())
    resp = await client.post(
        "/internal/agent/send-sms",
        content=body,
        headers={"X-Hail-Signature": "sha256=deadbeef"},
    )
    assert resp.status_code == 401


async def test_send_sms_unknown_call_is_spoken_denial(client, db_session):
    body = _sms_payload(uuid.uuid4())
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["spoken"]


async def test_send_sms_ended_call_is_denied(client, db_session):
    org = uuid.uuid4()
    call = await _insert_live_call(db_session, org)
    call.status = "completed"
    call.end_reason = "normal_hangup"
    await db_session.commit()
    body = _sms_payload(call.id)
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["ok"] is False


async def test_send_sms_happy_path_targets_counterpart(
    client, db_session, fake_sms_provider
):
    org = uuid.uuid4()
    call = await _insert_live_call(db_session, org, to_e164="+14155550123")
    body = _sms_payload(call.id, body="Your code is 42.")
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    data = resp.json()
    assert data["ok"] is True

    rows = (await db_session.execute(Sms.__table__.select())).fetchall()
    assert len(rows) == 1
    assert rows[0].to_e164 == "+14155550123"  # always the counterpart
    meta = rows[0].metadata
    assert meta["call_id"] == str(call.id)


async def test_send_sms_replays_same_invocation_without_double_send(
    client, db_session, fake_sms_provider
):
    org = uuid.uuid4()
    call = await _insert_live_call(db_session, org)
    payload = {
        "call_id": str(call.id),
        "tool_invocation_id": str(uuid.uuid4()),
        "body": "hi",
    }
    body = json.dumps(payload).encode()
    r1 = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    r2 = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert r1.json()["ok"] is True and r2.json()["ok"] is True
    count = len((await db_session.execute(Sms.__table__.select())).fetchall())
    assert count == 1


async def test_send_sms_cap_blocks_sixth_send(client, db_session, fake_sms_provider):
    org = uuid.uuid4()
    call = await _insert_live_call(db_session, org)
    for _ in range(5):
        body = _sms_payload(call.id)
        assert (
            await client.post(
                "/internal/agent/send-sms", content=body, headers=_signed(body)
            )
        ).json()["ok"] is True
    body = _sms_payload(call.id)
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["ok"] is False


async def test_send_email_resolves_member_and_never_crosses_orgs(
    client, db_session, fake_email_provider
):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    call = await _insert_live_call(db_session, org_a)
    db_session.add(
        EmailDomain(
            organization_id=org_a,
            kind="custom",
            domain="mail.a.test",
            verification_status="verified",
        )
    )
    user = User(
        id=uuid.uuid4(),
        name="Sarah Chen",
        email="sarah@b.test",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    # Sarah is a member of org B only — org A's call must NOT reach her.
    db_session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=org_b,
            role="member",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    payload = json.dumps(
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "Sarah Chen",
            "subject": "Call summary",
            "body_text": "Hello.",
        }
    ).encode()
    resp = await client.post(
        "/internal/agent/send-email", content=payload, headers=_signed(payload)
    )
    data = resp.json()
    assert data["ok"] is False  # not found in org A's directory
    assert "sarah@b.test" not in data["spoken"]  # never leak the address
    rows = (await db_session.execute(Email.__table__.select())).fetchall()
    assert rows == []


async def test_send_email_happy_path_stamps_call_id(
    client, db_session, fake_email_provider
):
    org = uuid.uuid4()
    call = await _insert_live_call(db_session, org)
    db_session.add(
        EmailDomain(
            organization_id=org,
            kind="custom",
            domain="mail.a.test",
            verification_status="verified",
        )
    )
    user = User(
        id=uuid.uuid4(),
        name="Sarah Chen",
        email="sarah@a.test",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=org,
            role="member",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    payload = json.dumps(
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "Sarah Chen",
            "subject": "Call summary",
            "body_text": "Hello from the call.",
        }
    ).encode()
    resp = await client.post(
        "/internal/agent/send-email", content=payload, headers=_signed(payload)
    )
    assert resp.json()["ok"] is True
    rows = (await db_session.execute(Email.__table__.select())).fetchall()
    assert len(rows) == 1
    assert rows[0].to_addresses == ["sarah@a.test"]
    assert rows[0].metadata["call_id"] == str(call.id)
```

Fixture notes for the implementer: `client` and the DB-session fixture come from `api/tests/conftest.py` (see its actual session fixture name — adjust `db_session` to match). `fake_sms_provider` / `fake_email_provider` mean "override the provider dependency with a stub that returns a successful `Provider*Result`" — copy the exact override pattern from `api/tests/test_sms_api.py` and `test_emails_api.py` (via `app.dependency_overrides[get_sms_provider]` or the equivalent those files use), and reuse their fake result classes rather than inventing new ones. The `Call`/`PhoneNumber`/`EmailDomain` constructors above may need extra non-nullable columns — fill from the models file and from how `test_calls_api.py` inserts rows.

- [ ] **Step 2: Run to verify failure**

Run: `cd api && uv run pytest tests/test_internal_agent_send.py -v`
Expected: FAIL — 404s (routes don't exist).

- [ ] **Step 3: Add the `CALL_META_BILLED` constant**

In `core/hailhq/core/billing.py`:

```python
# Key stamped into calls.metadata at create time: True when the call was
# created by a billed principal (org API key), False for the self-host
# shared-key path. The internal agent-send routes read it to decide
# whether to run the funds gate mid-call — same posture as
# require_funds' api_key_id check. Same visible-metadata precedent as
# pool.CALL_META_FROM_POOL.
CALL_META_BILLED = "billed"
```

Add to `__all__`. (The write side lands in Task 7; the route treats a missing key as `False` so pre-existing calls keep working.)

- [ ] **Step 4: Implement the routes**

`api/hailhq/api/routes/internal/agent.py`:

```python
"""Voicebot → API agent-send routes.

The voice agent's send tools execute here so the full existing outbound
stack — suppression/velocity gate, funds, audit, disclosure footer,
billing — runs unchanged (spec: docs/superpowers/specs/
2026-07-11-voicebot-agent-tools-design.md). Auth is the shared
HAIL_INTERNAL_SECRET HMAC (routes/internal/auth.py).

Responses are always HTTP 200 with ``{ok, spoken}`` — ``spoken`` is a
short plain sentence the agent says on the call. Policy denials are
data, not HTTP errors, and stay deliberately vague: never reveal
suppression-list membership or a member's address to the callee.

The agent never supplies addresses: SMS always targets the call's
counterpart (``calls.to_e164``); email targets a directory name resolved
here, scoped to the call's org.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.audit import write_audit_log
from hailhq.api.deps import get_session
from hailhq.api.funds import has_funds
from hailhq.api.numbers import resolve_org_number
from hailhq.api.routes.emails import (
    deliver_email,
    from_address_for,
    get_email_provider,
    resolve_sender,
)
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.api.routes.sms import deliver_sms, get_sms_provider
from hailhq.core.billing import CALL_META_BILLED
from hailhq.core.compliance_gate import check_email_allowed, check_sms_allowed
from hailhq.core.directory import resolve_member_emails
from hailhq.core.models import Call, Email, Sms
from hailhq.core.providers.email.base import EmailProvider
from hailhq.core.providers.sms.base import SmsProvider

router = APIRouter(
    prefix="/internal/agent",
    dependencies=[Depends(verify_internal_request)],
    include_in_schema=False,
)

AGENT_SEND_CAP = 5  # total agent-initiated sends (sms + email) per call

_SPOKEN_CALL_UNAVAILABLE = "This call can no longer send messages."
_SPOKEN_NOT_ALLOWED = "I'm not able to send that message."
_SPOKEN_CAP = "I've reached the limit of messages I can send on this call."
_SPOKEN_SMS_SENT = "Text message sent to the number on this call."
_SPOKEN_SMS_FAILED = "I couldn't send the text message."
_SPOKEN_SMS_UNCONFIGURED = "Text messaging isn't set up for this account."
_SPOKEN_EMAIL_SENT = "Email sent."
_SPOKEN_EMAIL_FAILED = "I couldn't send the email."
_SPOKEN_EMAIL_UNCONFIGURED = "Email isn't set up for this account."


class AgentSendBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: UUID
    tool_invocation_id: UUID


class AgentSendSmsRequest(AgentSendBase):
    body: str = Field(min_length=1, max_length=480)


class AgentSendEmailRequest(AgentSendBase):
    recipient_name: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=200)
    body_text: str = Field(min_length=1, max_length=5000)


class AgentSendResponse(BaseModel):
    ok: bool
    spoken: str


def _meta(req: AgentSendBase) -> dict[str, str]:
    return {
        "call_id": str(req.call_id),
        "tool_invocation_id": str(req.tool_invocation_id),
    }


async def _load_live_call(db: AsyncSession, call_id: UUID) -> Call | None:
    call = (
        await db.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()
    if call is None or call.status != "in_progress":
        return None
    return call


async def _sends_this_call(db: AsyncSession, org_id: UUID, call_id: UUID) -> int:
    key = str(call_id)
    emails = (
        await db.execute(
            select(func.count())
            .select_from(Email)
            .where(
                Email.organization_id == org_id,
                Email.metadata_["call_id"].astext == key,
            )
        )
    ).scalar_one()
    sms = (
        await db.execute(
            select(func.count())
            .select_from(Sms)
            .where(
                Sms.organization_id == org_id,
                Sms.metadata_["call_id"].astext == key,
            )
        )
    ).scalar_one()
    return int(emails) + int(sms)


async def _shared_denial(db: AsyncSession, call: Call) -> str | None:
    """Cap + funds checks shared by both send routes.

    Returns a spoken denial or None when the send may proceed.
    """
    if await _sends_this_call(db, call.organization_id, call.id) >= AGENT_SEND_CAP:
        return _SPOKEN_CAP
    if call.metadata_.get(CALL_META_BILLED) and not await has_funds(
        db, call.organization_id
    ):
        return _SPOKEN_NOT_ALLOWED
    return None


@router.post("/send-sms", response_model=AgentSendResponse)
async def agent_send_sms(
    body: AgentSendSmsRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SmsProvider, Depends(get_sms_provider)],
) -> AgentSendResponse:
    call = await _load_live_call(db, body.call_id)
    if call is None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_CALL_UNAVAILABLE)
    org = call.organization_id

    # Idempotent replay: the voicebot retries timeouts with the same id.
    prior = (
        await db.execute(
            select(Sms).where(
                Sms.organization_id == org,
                Sms.metadata_["tool_invocation_id"].astext
                == str(body.tool_invocation_id),
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        ok = prior.status in ("sent", "delivered")
        return AgentSendResponse(
            ok=ok, spoken=_SPOKEN_SMS_SENT if ok else _SPOKEN_SMS_FAILED
        )

    denial = await _shared_denial(db, call)
    if denial is not None:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.sms.blocked",
            resource_type="sms",
            resource_id=None,
            payload={**_meta(body), "reason": "cap_or_funds"},
        )
        return AgentSendResponse(ok=False, spoken=denial)

    gate = await check_sms_allowed(db, org, call.to_e164)
    if not gate.allowed:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.sms.blocked",
            resource_type="sms",
            resource_id=None,
            payload={**_meta(body), "reason": gate.reason, "checks": gate.checks},
        )
        return AgentSendResponse(ok=False, spoken=_SPOKEN_NOT_ALLOWED)

    from_number = await resolve_org_number(db, org, None, capability="sms")
    if from_number is None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_SMS_UNCONFIGURED)

    sms = Sms(
        organization_id=org,
        from_number_id=from_number.id,
        from_e164=from_number.e164,
        to_e164=call.to_e164,  # counterpart only — never a parameter
        direction="outbound",
        status="queued",
        body=body.body,
        metadata_=_meta(body),
    )
    db.add(sms)
    await db.commit()

    await write_audit_log(
        organization_id=org,
        api_key_id=None,
        action="agent.sms.send",
        resource_type="sms",
        resource_id=sms.id,
        payload={
            **_meta(body),
            "to": sms.to_e164,
            "consent_source": "voice_call",
            "message_type": "transactional",
            "compliance": gate.checks,
        },
    )

    err = await deliver_sms(db, provider, sms)
    if err is not None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_SMS_FAILED)
    return AgentSendResponse(ok=True, spoken=_SPOKEN_SMS_SENT)


@router.post("/send-email", response_model=AgentSendResponse)
async def agent_send_email(
    body: AgentSendEmailRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> AgentSendResponse:
    call = await _load_live_call(db, body.call_id)
    if call is None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_CALL_UNAVAILABLE)
    org = call.organization_id

    prior = (
        await db.execute(
            select(Email).where(
                Email.organization_id == org,
                Email.metadata_["tool_invocation_id"].astext
                == str(body.tool_invocation_id),
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        ok = prior.status == "sent"
        return AgentSendResponse(
            ok=ok, spoken=_SPOKEN_EMAIL_SENT if ok else _SPOKEN_EMAIL_FAILED
        )

    denial = await _shared_denial(db, call)
    if denial is not None:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.email.blocked",
            resource_type="email",
            resource_id=None,
            payload={**_meta(body), "reason": "cap_or_funds"},
        )
        return AgentSendResponse(ok=False, spoken=denial)

    matches = await resolve_member_emails(db, org, body.recipient_name)
    if not matches:
        return AgentSendResponse(
            ok=False,
            spoken=(
                f"I couldn't find {body.recipient_name} in the directory."
            ),
        )
    if len(matches) > 1:
        return AgentSendResponse(
            ok=False,
            spoken=(
                f"More than one person is named {body.recipient_name}, so I "
                "can't pick a recipient."
            ),
        )
    recipient = matches[0]

    gate = await check_email_allowed(db, org, [recipient])
    if not gate.allowed:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.email.blocked",
            resource_type="email",
            resource_id=None,
            payload={**_meta(body), "reason": gate.reason, "checks": gate.checks},
        )
        return AgentSendResponse(ok=False, spoken=_SPOKEN_NOT_ALLOWED)

    try:
        sd = await resolve_sender(db, org, None)
    except HTTPException:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_EMAIL_UNCONFIGURED)

    email = Email(
        organization_id=org,
        email_domain_id=sd.id,
        from_address=from_address_for(sd, None),
        to_addresses=[recipient],
        subject=body.subject,
        body_text=body.body_text,
        status="queued",
        provider="ses",
        metadata_=_meta(body),
    )
    db.add(email)
    await db.commit()
    await db.refresh(email)

    await write_audit_log(
        organization_id=org,
        api_key_id=None,
        action="agent.email.send",
        resource_type="email",
        resource_id=email.id,
        payload={
            **_meta(body),
            "to": email.to_addresses,
            "subject": email.subject,
            "consent_source": "voice_call",
            "message_type": "transactional",
            "compliance": gate.checks,
        },
    )

    err = await deliver_email(db, email_provider, email)
    if err is not None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_EMAIL_FAILED)
    return AgentSendResponse(ok=True, spoken=_SPOKEN_EMAIL_SENT)


__all__ = ["router", "AGENT_SEND_CAP"]
```

Implementation notes:

- The `Sms` provider-config import path: check how `routes/sms.py` names its provider base (`from hailhq.core.providers.sms.base import SmsProvider` — verify the actual path; use whatever `routes/sms.py` imports).
- `get_email_provider`: import from where `routes/emails.py` gets it (check its imports; adjust the import in the code above accordingly).
- If `Email` requires `conversation_id`/other non-nullable kwargs, mirror `create_email`'s constructor call.
- Register in `api/hailhq/api/main.py` exactly like the existing internal routers.

- [ ] **Step 5: Run the tests**

Run: `cd api && uv run pytest tests/test_internal_agent_send.py -v && uv run pytest`
Expected: new tests PASS; full API suite green.

- [ ] **Step 6: Commit**

```bash
git add api/hailhq/api/routes/internal/agent.py api/hailhq/api/main.py core/hailhq/core/billing.py api/tests/test_internal_agent_send.py
git commit -m "feat(api): internal agent-send routes for voicebot sms/email tools"
```

---

### Task 7: API — `tools` field on POST /calls + billed stamp

**Files:**

- Modify: `core/hailhq/core/schemas.py` (`CallCreate`, ~line 173)
- Modify: `api/hailhq/api/routes/calls.py` (`create_call`)
- Test: append to `api/tests/test_calls_api.py`

**Interfaces:**

- Consumes: `all_tools()` (Task 2/3), `CALL_META_BILLED` (Task 6).
- Produces: `CallCreate.tools: list[str] | None`; dispatch metadata gains `"tools": body.tools`; `calls.metadata` gains `CALL_META_BILLED: bool`.

- [ ] **Step 1: Append failing tests**

Follow the existing `test_calls_api.py` style (mocked LiveKit client; `org_and_key` fixture). Three tests:

```python
async def test_post_calls_rejects_unknown_tool(client, org_and_key):
    _, _, plain = org_and_key
    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "system_prompt": "hi",
            "recipient_consent": True,
            "tools": ["send_sms", "launch_rocket"],
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422
    assert "launch_rocket" in resp.text


async def test_post_calls_forwards_tools_in_dispatch_metadata(
    client, org_and_key, mock_livekit
):
    # `mock_livekit` = however this file fakes the LiveKit client; assert on
    # the dispatch_agent call's metadata kwarg.
    _, _, plain = org_and_key
    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "system_prompt": "hi",
            "recipient_consent": True,
            "tools": ["end_call"],
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201
    metadata = mock_livekit.dispatch_agent.call_args.kwargs["metadata"]
    assert metadata["tools"] == ["end_call"]


async def test_post_calls_stamps_billed_flag(client, org_and_key, db_session):
    from hailhq.core.billing import CALL_META_BILLED

    _, _, plain = org_and_key
    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi", "recipient_consent": True},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201
    call_id = resp.json()["id"]
    row = await db_session.get(Call, uuid.UUID(call_id))
    assert row.metadata_[CALL_META_BILLED] is True  # org API key ⇒ billed
```

(Adjust fixture names — `mock_livekit`, `db_session` — to what `test_calls_api.py` actually uses.)

- [ ] **Step 2: Run to verify failure**

Run: `cd api && uv run pytest tests/test_calls_api.py -k "tool or billed" -v`
Expected: FAIL — 422 `extra="forbid"` on `tools` for the first two; missing metadata key for the third.

- [ ] **Step 3: Implement**

`core/hailhq/core/schemas.py`, inside `CallCreate`:

```python
    tools: list[str] | None = Field(
        default=None,
        description=(
            "Agent tools to allow on this call. Omitted: every tool the "
            "organization's configured channels support (new channels appear "
            "automatically). Empty list: no tools. Tool names are validated "
            "against the server's registry."
        ),
    )
```

`api/hailhq/api/routes/calls.py`, in `create_call` before any DB work (next to the other 422 validations):

```python
    if body.tools is not None:
        known = {t.name for t in all_tools()}
        unknown = sorted(set(body.tools) - known)
        if unknown:
            raise await cache_failure(
                idem,
                unprocessable(
                    f"unknown tools: {', '.join(unknown)}", loc=["body", "tools"]
                ),
            )
```

(import `from hailhq.core.agent_tools.registry import all_tools`; match how this route raises its other 422s — if it uses plain `unprocessable(...)` without `cache_failure`, do the same.)

Where `call_metadata` is assembled (~line 258):

```python
    call_metadata[CALL_META_BILLED] = principal.api_key_id is not None
```

(import `CALL_META_BILLED` from `hailhq.core.billing`.) And in the `dispatch_agent(metadata={...})` dict add:

```python
                "tools": body.tools,
```

- [ ] **Step 4: Run tests**

Run: `cd api && uv run pytest tests/test_calls_api.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/schemas.py api/hailhq/api/routes/calls.py api/tests/test_calls_api.py
git commit -m "feat(api): per-call agent tools opt-out + billed stamp on POST /calls"
```

---

### Task 8: Voicebot — registry → LiveKit adaptation

**Files:**

- Create: `voicebot/hailhq/voicebot/tools.py`
- Test: `voicebot/tests/test_tools.py`

**Interfaces:**

- Consumes: `all_tools()`, `ToolSpec`, `ToolContext`, `AgentApiClient`; `settings.hail_api_url` / `settings.hail_internal_secret`; `session_scope`.
- Produces: `async build_agent_tools(metadata: dict, *, call_id: UUID, hangup) -> tuple[list, AgentApiClient | None]` — the list goes to `Agent(tools=...)`; the client (or None) must be `aclose()`d at shutdown. Also `SPOKEN_TOOL_FAILURE: str`.

- [ ] **Step 1: Write the failing tests**

```python
# voicebot/tests/test_tools.py
"""Registry → LiveKit adaptation: opt-out, degradation, error isolation."""

from __future__ import annotations

import uuid

import pytest

from hailhq.voicebot.tools import (
    SPOKEN_TOOL_FAILURE,
    _make_handler,
    build_agent_tools,
)


class FakeRunContext:
    def __init__(self):
        self.waited = False

    async def wait_for_playout(self):
        self.waited = True


def _spec(name="boom", tier="read_only", execute=None):
    from hailhq.core.agent_tools.spec import ToolSpec

    async def _avail(_org, _session):
        return True

    async def _default_execute(_ctx, _args):
        return "ok"

    return ToolSpec(
        name=name,
        description="d",
        parameters={"type": "object", "properties": {}, "required": []},
        risk_tier=tier,
        is_available=_avail,
        execute=execute or _default_execute,
    )


def _tctx():
    from hailhq.core.agent_tools.spec import ToolContext

    return ToolContext(
        call_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        api=None,
        hangup=None,
    )


async def test_empty_opt_out_disables_all_tools():
    tools, api = await build_agent_tools(
        {"organization_id": str(uuid.uuid4()), "tools": []},
        call_id=uuid.uuid4(),
        hangup=None,
    )
    assert tools == []
    assert api is None


async def test_missing_organization_id_disables_tools():
    tools, api = await build_agent_tools({}, call_id=uuid.uuid4(), hangup=None)
    assert tools == []


async def test_wrapper_isolates_tool_exceptions():
    async def explode(_ctx, _args):
        raise RuntimeError("kaboom")

    handler = _make_handler(_spec(execute=explode), _tctx())
    result = await handler({}, FakeRunContext())
    assert result == SPOKEN_TOOL_FAILURE


async def test_session_control_waits_for_playout():
    seen = []

    async def record(_ctx, _args):
        seen.append("executed")
        return "bye"

    rc = FakeRunContext()
    handler = _make_handler(_spec(tier="session_control", execute=record), _tctx())
    result = await handler({}, rc)
    assert rc.waited is True
    assert result == "bye"


async def test_read_only_does_not_wait():
    rc = FakeRunContext()
    handler = _make_handler(_spec(tier="read_only"), _tctx())
    await handler({}, rc)
    assert rc.waited is False
```

Note: the tests target `_make_handler` (the raw async handler) rather than the `RawFunctionTool` wrapper, so no LiveKit invocation machinery is needed.

- [ ] **Step 2: Run to verify failure**

Run: `cd voicebot && uv run pytest tests/test_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: hailhq.voicebot.tools`.

- [ ] **Step 3: Implement `voicebot/hailhq/voicebot/tools.py`**

```python
"""Adapt the core agent-tool registry to LiveKit function tools.

Per-call filtering: the dispatch metadata's ``tools`` list (None ⇒ all,
[] ⇒ none) then each spec's per-org ``is_available`` check. Send tools
require HAIL_INTERNAL_SECRET (they call the API's internal agent routes);
without it only local/read-only tools remain.

A tool failure never kills the call: the wrapper catches everything and
returns a speakable apology to the LLM.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from livekit.agents.llm import function_tool
from livekit.agents.voice import RunContext

from hailhq.core.agent_tools.client import AgentApiClient
from hailhq.core.agent_tools.registry import all_tools
from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.config import settings
from hailhq.core.db import session_scope

logger = logging.getLogger("hailhq.voicebot")

SPOKEN_TOOL_FAILURE = "Sorry, that didn't work."


def _make_handler(spec: ToolSpec, tctx: ToolContext):
    async def handler(raw_arguments: dict[str, Any], context: RunContext) -> str:
        try:
            # session_control tools (end_call) must not cut off the agent's
            # own goodbye: wait for the pre-tool speech to finish playing.
            if spec.risk_tier == "session_control":
                await context.wait_for_playout()
            return await spec.execute(tctx, raw_arguments)
        except Exception:
            logger.exception(
                "agent tool %s failed for call_id=%s", spec.name, tctx.call_id
            )
            return SPOKEN_TOOL_FAILURE

    return handler


def _wrap(spec: ToolSpec, tctx: ToolContext):
    return function_tool(
        _make_handler(spec, tctx),
        raw_schema={
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    )


async def build_agent_tools(
    metadata: dict[str, Any], *, call_id: UUID, hangup
) -> tuple[list, AgentApiClient | None]:
    """Build this call's LiveKit tools. Returns (tools, api_client).

    The caller must ``aclose()`` the client at shutdown. Returns no tools
    when the dispatch predates the ``organization_id`` field (rolling
    deploy) — no tools beats wrong-org tools.
    """
    raw_org = metadata.get("organization_id")
    if raw_org is None:
        logger.warning(
            "dispatch metadata has no organization_id; agent tools disabled"
        )
        return [], None
    organization_id = UUID(str(raw_org))

    allowed = metadata.get("tools")  # None ⇒ all available
    specs = [s for s in all_tools() if allowed is None or s.name in allowed]
    if not specs:
        return [], None

    api: AgentApiClient | None = None
    if settings.hail_internal_secret:
        api = AgentApiClient(settings.hail_api_url, settings.hail_internal_secret)

    tctx = ToolContext(
        call_id=call_id, organization_id=organization_id, api=api, hangup=hangup
    )

    available: list[ToolSpec] = []
    async with session_scope() as session:
        for spec in specs:
            try:
                if await spec.is_available(organization_id, session):
                    available.append(spec)
            except Exception:
                logger.exception("availability check failed for tool %s", spec.name)

    return [_wrap(s, tctx) for s in available], api


__all__ = ["SPOKEN_TOOL_FAILURE", "build_agent_tools"]
```

- [ ] **Step 4: Run tests**

Run: `cd voicebot && uv run pytest tests/test_tools.py -v`
Expected: PASS. (`test_empty_opt_out_disables_all_tools` and the missing-org test never touch the DB — the short-circuits return first.)

- [ ] **Step 5: Commit**

```bash
git add voicebot/hailhq/voicebot/tools.py voicebot/tests/test_tools.py
git commit -m "feat(voicebot): adapt core agent-tool registry to livekit function tools"
```

---

### Task 9: Voicebot — wire tools into the entrypoint + preamble guardrail

**Files:**

- Modify: `voicebot/hailhq/voicebot/agent.py`
- Test: append to `voicebot/tests/test_agent.py`

**Interfaces:**

- Consumes: `build_agent_tools` (Task 8), `CallEndReason.NORMAL_HANGUP`.
- Produces: `Agent(instructions=..., tools=...)`; a `_hangup` closure that stamps `captured["end_reason"]` before `ctx.shutdown()` so agent-ended calls finalize as `completed`/`normal_hangup`, not `failed`/`worker_shutdown`.

- [ ] **Step 1: Write the failing test**

```python
# append to voicebot/tests/test_agent.py
def test_voice_preamble_requires_confirmation_before_sending():
    from hailhq.voicebot.agent import VOICE_PREAMBLE

    lowered = VOICE_PREAMBLE.lower()
    assert "before sending" in lowered
    assert "confirmation" in lowered
```

Plus one behavioral test for the hangup closure. The closure lives inside `entrypoint`, so extract it as a module-level factory to make it testable:

```python
def test_agent_hangup_marks_normal_hangup():
    from hailhq.core.call_end_reasons import CallEndReason
    from hailhq.voicebot.agent import make_agent_hangup

    from .._fakes import FakeJobContext  # adjust import to this file's style

    captured = {"status": None, "end_reason": None}
    ctx = FakeJobContext()
    hangup = make_agent_hangup(ctx, captured)

    import asyncio

    asyncio.get_event_loop().run_until_complete(hangup())
    assert captured["end_reason"] == CallEndReason.NORMAL_HANGUP.value
    assert captured["status"] is None  # on_call_end default ⇒ "completed"
    assert ctx.shutdown_called  # match FakeJobContext's actual attribute
```

(Match the test file's existing async style — if it uses `async def` tests, write it that way; check `FakeJobContext`'s shutdown-recording attribute name in `voicebot/tests/_fakes.py:145`.)

- [ ] **Step 2: Run to verify failure**

Run: `cd voicebot && uv run pytest tests/test_agent.py -k "preamble_requires or hangup_marks" -v`
Expected: FAIL — assertion on preamble text; `ImportError: make_agent_hangup`.

- [ ] **Step 3: Implement**

**Preamble** — append one bullet to the `# Guardrails` section of `VOICE_PREAMBLE`. The preamble may have drifted since this plan was written (parallel workstream) — add the rule to whatever the Guardrails section now contains; do not diff against old text:

```
- Before sending any text message or email, say exactly what you will send \
and to whom, and wait for the other party's confirmation.
```

**Hangup factory** — module level, near `speak_greeting`:

```python
def make_agent_hangup(ctx: JobContext, captured: dict[str, str | None]):
    """Hangup handle for the end_call agent tool.

    Stamps ``end_reason`` BEFORE ``ctx.shutdown()``: the session-close
    handler maps a bare ``job_shutdown`` to ``worker_shutdown``/``failed``,
    which would mis-record a deliberate, successful agent hangup. Status
    stays None so ``on_call_end`` falls back to ``"completed"``.
    """

    async def _hangup() -> None:
        captured["end_reason"] = CallEndReason.NORMAL_HANGUP.value
        ctx.shutdown(reason="agent_end_call")

    return _hangup
```

**Entrypoint wiring** — in `entrypoint`, after `session = build_session(...)` and before `agent = Agent(...)`:

```python
    agent_tools, agent_api = await build_agent_tools(
        metadata, call_id=call_id, hangup=make_agent_hangup(ctx, captured)
    )
    if agent_tools:
        logger.info(
            "call_id=%s agent tools enabled: %s",
            call_id,
            [t.info.name for t in agent_tools],
        )
```

Change the Agent construction:

```python
    agent = Agent(
        instructions=build_instructions(metadata.get("system_prompt")),
        tools=agent_tools,
    )
```

(If `RawFunctionTool.info` has no `.name`, log `len(agent_tools)` instead — check `t.info` attributes.)

And in `_shutdown`, after the existing gathers:

```python
        if agent_api is not None:
            await agent_api.aclose()
```

Add `make_agent_hangup` to `__all__`. Import `build_agent_tools` from `hailhq.voicebot.tools`.

- [ ] **Step 4: Run the full voicebot suite**

Run: `cd voicebot && uv run pytest`
Expected: all PASS (existing entrypoint tests must survive — `build_agent_tools` returns `([], None)` in tests whose fake metadata lacks `organization_id`, which is why the degradation path matters).

- [ ] **Step 5: Commit**

```bash
git add voicebot/hailhq/voicebot/agent.py voicebot/tests/test_agent.py
git commit -m "feat(voicebot): wire agent tools into the call session + confirm-before-send guardrail"
```

---

### Task 10: Contracts — OpenAPI, CLI, MCP

**Files:**

- Modify: `openapi/openapi.yaml` (regenerated)
- Modify: CLI generated client (via `make codegen`)
- Modify: `mcp/hailhq/mcp/tools.py` (`place_call` ~line 102 and the `@mcp_app.tool` registration ~line 461) and `mcp/hailhq/mcp/hail_client.py` (`place_call`)
- Test: existing MCP tests (`mcp/tests/`) — extend the place_call test with the `tools` param if one exists.

**Interfaces:**

- Consumes: Task 7's `CallCreate.tools`.
- Produces: `tools` in the public OpenAPI contract, CLI client, and MCP `place_call`.

- [ ] **Step 1: MCP passthrough**

In `mcp/hailhq/mcp/hail_client.py` `place_call`: add keyword `tools: list[str] | None = None`, include `"tools": tools` in the request body only when not None (match how the function handles other optional fields). In `mcp/hailhq/mcp/tools.py`: thread the same parameter through `place_call` (~line 102) and the `place_call_tool` registration (~line 461), description: "Agent tools to allow on this call. Omit for all available; pass [] to disable."

- [ ] **Step 2: Run MCP tests**

Run: `cd mcp && uv run pytest`
Expected: PASS (extend the place_call test to assert `tools` reaches the request body, following the file's existing fake-client pattern).

- [ ] **Step 3: Regenerate OpenAPI**

Start the stack (per CLAUDE.md dev commands: postgres up, then `cd api && uv run uvicorn hailhq.api.main:app --port 8080`), then per `docs/contributing.md`:

```bash
curl -s http://localhost:8080/openapi.json \
  | python -c "import json, sys, yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" \
  > openapi/openapi.yaml
pnpm exec prettier --write openapi/openapi.yaml
```

Verify the diff: `tools` appears under `CallCreate`; no `/internal/agent/*` paths appear (the router sets `include_in_schema=False`).

- [ ] **Step 4: CLI codegen**

```bash
cd cli && make codegen && go build ./...
```

Expected: build succeeds; generated client includes the `tools` field.

- [ ] **Step 5: Commit**

```bash
git add openapi/openapi.yaml cli/ mcp/
git commit -m "feat(contracts): tools field on place_call across openapi, cli, and mcp"
```

---

### Task 11: Full verification sweep

**Files:** none (verification only)

- [ ] **Step 1: Run every suite**

```bash
cd core && uv run pytest && uv run ruff check . && uv run black --check .
cd ../api && uv run pytest && uv run ruff check . && uv run black --check .
cd ../voicebot && uv run pytest && uv run ruff check . && uv run black --check .
cd ../mcp && uv run pytest
cd ../cli && go build ./... && go vet ./...
```

Expected: everything green.

- [ ] **Step 2: mypy (matches CI)**

```bash
cd core && uv run mypy . ; cd ../api && uv run mypy . ; cd ../voicebot && uv run mypy .
```

Expected: no new errors relative to main.

- [ ] **Step 3: Manual smoke (optional but recommended)**

With the local stack up and `HAIL_INTERNAL_SECRET` set in `.env`: place a real call to a test number, ask the agent to "text me a confirmation", verify: the SMS arrives at the called number, `sms.metadata` carries `call_id` + `tool_invocation_id`, `audit_log` has `agent.sms.send`, and `call_events` has the `tool_call` row. Then say "we're done, you can hang up" and verify the call row ends `completed` / `normal_hangup`.
