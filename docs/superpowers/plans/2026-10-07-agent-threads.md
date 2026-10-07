# Agent Threads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An agent sees one shared history of calls and texts per caller, on calls and on texts, and learns about a text the caller sends during a call at once.

**Architecture:** A thread is a query over `sms` rows and `call_events` turns keyed by (org, agent, caller number). No new table. A core module renders the thread. The voice agent gets it in its prompt plus a `thread_history` tool. The text agent gets it as chat history. A voicebot poller feeds texts that arrive mid-call into the live session.

**Tech Stack:** Python 3.12, SQLAlchemy async, Alembic, FastAPI, LiveKit Agents (`livekit-agents>=1.5,<2`), pytest (`async_session` fixture from `hailhq.core.testing.fixtures`).

**Spec:** `docs/superpowers/specs/2026-10-07-agent-threads-design.md`

## Global Constraints

- No new table. A thread is a query (spec §1).
- Thread window: last 30 items, last 7 days (`THREAD_LIMIT = 30`, `THREAD_WINDOW = timedelta(days=7)`).
- Texts longer than 500 characters are cut in the voice prompt (`CUT_CHARS = 500`). The text agent gets full text.
- No tool or API takes a caller number to read. The caller is always found from the call (voice) or the inbound text (text agent).
- Never take an SMS number bound to another agent.
- Python: ruff + black, type hints, pydantic v2. Commits: Conventional Commits, no AI trailer.
- `core/` holds shared logic. `api/` and `voicebot/` do not duplicate it.
- Public OpenAPI does not change (only `/internal/*` routes change). Confirm with `cd api && uv run pytest tests/test_openapi_descriptions.py -q`.

## Review Focus

- Caller B's texts or calls never appear in caller A's thread, even for the same agent: test in Task 2.
- Agent X's items never appear in agent Y's thread for the same caller: test in Task 2.
- A mid-call text must not show twice (the `sms` row and the `user_turn` event the injected message creates): marker filter, test in Task 2.
- A text from a caller with no active call still queues a reply as before: test in Task 5.
- An org whose only SMS numbers are bound to other agents: `send_sms` says it cannot text and changes no routing: test in Task 4.
- Thread read fails (DB error) at call start: the call still starts with no history: test in Task 6.
- Caller text is long or has odd characters when injected into the live call: cut to 1000 characters, test in Task 7.

## File Structure

- Create `core/hailhq/core/threads.py`: thread query, rendering, active-call lookup. One responsibility: read the thread.
- Create `core/hailhq/core/agent_tools/thread_history.py`: the tool.
- Create `api/migrations/versions/0052_thread_indexes.py`.
- Modify `core/hailhq/core/models.py` (indexes, `Sms.agent_id` comment).
- Modify `core/hailhq/core/sms_ingest.py` (set `agent_id`, skip reply during active call).
- Modify `core/hailhq/core/text_agent.py` (use thread items).
- Modify `core/hailhq/core/prompts.py` (history section).
- Modify `core/hailhq/core/agent_tools/registry.py`.
- Modify `api/hailhq/api/numbers.py` (`resolve_sms_number`).
- Modify `api/hailhq/api/routes/internal/agent.py` (send-sms number choice, `agent_id`).
- Modify `voicebot/hailhq/voicebot/textbot.py`, `voicebot/hailhq/voicebot/agent.py`.
- Create `voicebot/hailhq/voicebot/text_watch.py`: mid-call text poller.
- Modify `docs/public/agents.md`.
- Tests: `core/tests/test_threads.py`, `core/tests/test_text_agent.py`, `core/tests/test_sms_ingest.py`, `core/tests/test_agent_tools.py`, `core/tests/test_prompts.py`, `api/tests/test_internal_agent_send.py`, `voicebot/tests/test_text_watch.py`, `voicebot/tests/test_agent_inbound.py`.

Run all work in this worktree. Python commands run from the repo root with `uv run --all-packages --all-extras` is not needed for tests: run `cd core && uv run pytest ...`, `cd api && uv run pytest ...`, `cd voicebot && uv run pytest ...`. Never run `uv sync --extra dev` inside a subpackage (it prunes the shared venv).

---

### Task 1: Indexes and model comment

**Files:**

- Create: `api/migrations/versions/0052_thread_indexes.py`
- Modify: `core/hailhq/core/models.py` (`Call.__table_args__`, `Sms.__table_args__`, `Sms.agent_id` comment at ~line 741)
- Test: `api/tests/test_migrations.py` (existing; must still pass)

**Interfaces:**

- Produces: indexes `calls_thread_from_idx`, `calls_thread_to_idx`, `sms_thread_from_idx`, `sms_thread_to_idx`. The caller sits in `from_e164` on inbound rows and `to_e164` on outbound rows, so each table gets two indexes.

- [ ] **Step 1: Add indexes to the models**

In `Call.__table_args__` and `Sms.__table_args__` (add to the existing tuples):

```python
Index("calls_thread_from_idx", "organization_id", "agent_id", "from_e164"),
Index("calls_thread_to_idx", "organization_id", "agent_id", "to_e164"),
```

```python
Index("sms_thread_from_idx", "organization_id", "agent_id", "from_e164", "requested_at"),
Index("sms_thread_to_idx", "organization_id", "agent_id", "to_e164", "requested_at"),
```

- [ ] **Step 2: Fix the stale comment on `Sms.agent_id`**

Replace `# Outbound: the agent that wrote this reply. Inbound: NULL.` with:

```python
    # The agent this text belongs to: the number's text agent on inbound rows,
    # the agent that wrote it on outbound rows. NULL when no agent is involved.
```

- [ ] **Step 3: Write the migration**

```python
"""Thread lookup indexes on calls and sms.

A thread is (organization_id, agent_id, caller number). The caller is in
from_e164 on inbound rows and to_e164 on outbound rows, so each table gets
one index per column.

Revision ID: 0052
Revises: 0051
"""

from __future__ import annotations

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "calls_thread_from_idx", "calls", ["organization_id", "agent_id", "from_e164"]
    )
    op.create_index(
        "calls_thread_to_idx", "calls", ["organization_id", "agent_id", "to_e164"]
    )
    op.create_index(
        "sms_thread_from_idx",
        "sms",
        ["organization_id", "agent_id", "from_e164", "requested_at"],
    )
    op.create_index(
        "sms_thread_to_idx",
        "sms",
        ["organization_id", "agent_id", "to_e164", "requested_at"],
    )


def downgrade() -> None:
    op.drop_index("sms_thread_to_idx", table_name="sms")
    op.drop_index("sms_thread_from_idx", table_name="sms")
    op.drop_index("calls_thread_to_idx", table_name="calls")
    op.drop_index("calls_thread_from_idx", table_name="calls")
```

- [ ] **Step 4: Run migration tests**

Run: `cd api && uv run pytest tests/test_migrations.py -q`
Expected: PASS (head is `0052`, models and migrations agree).

- [ ] **Step 5: Commit**

```bash
git add api/migrations/versions/0052_thread_indexes.py core/hailhq/core/models.py
git commit -m "feat(threads): index calls and sms by agent and caller"
```

---

### Task 2: Thread query and rendering (`core/hailhq/core/threads.py`)

**Files:**

- Create: `core/hailhq/core/threads.py`
- Test: `core/tests/test_threads.py`

**Interfaces:**

- Produces:
  - `THREAD_LIMIT: int = 30`, `THREAD_WINDOW: timedelta`, `CUT_CHARS: int = 500`, `TEXT_MARKER: str = "[text message from caller] "`
  - `@dataclass(frozen=True) class ThreadItem: id: str; at: datetime; kind: Literal["text_in","text_out","call_caller","call_agent"]; text: str`
    - `id` is `"sms:<uuid>"` or `"event:<uuid>"`.
  - `async def thread_items(db, organization_id: UUID, agent_id: UUID, caller_e164: str, *, limit: int = THREAD_LIMIT, before: str | None = None, until: datetime | None = None) -> list[ThreadItem]` (oldest first; last `limit` items inside `THREAD_WINDOW`; `before` is an item id, only older items are returned; `until` drops items after that time).
  - `async def thread_item(db, organization_id, agent_id, caller_e164, item_id: str) -> ThreadItem | None` (one item, scoped to the thread).
  - `def render_thread(items: list[ThreadItem], *, cut: int | None = CUT_CHARS) -> str`
  - `async def call_thread_key(db, call_id: UUID) -> tuple[UUID, UUID, str] | None` returns `(organization_id, agent_id, caller_e164)` from a `Call` row, or None when the call has no agent.
  - `async def active_call_for_thread(db, organization_id, agent_id, caller_e164) -> Call | None` (status `ringing` or `in_progress`).

- [ ] **Step 1: Write the failing tests**

`core/tests/test_threads.py`:

```python
"""Threads: one history per (org, agent, caller) over texts and call turns."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from hailhq.core import threads
from hailhq.core.models import Agent, Call, CallEvent, Sms

ORG_NUMBER = "+14155550100"
PERSON = "+33612345678"
OTHER = "+33699999999"
NOW = datetime.now(timezone.utc)


async def _agent(session, org, name="a"):
    agent = Agent(organization_id=org, name=name, system_prompt="x")
    session.add(agent)
    await session.flush()
    return agent


def _sms(org, agent_id, *, inbound, person=PERSON, body="hi", at=NOW):
    return Sms(
        organization_id=org,
        agent_id=agent_id,
        provider="twilio",
        from_e164=person if inbound else ORG_NUMBER,
        to_e164=ORG_NUMBER if inbound else person,
        direction="inbound" if inbound else "outbound",
        status="received" if inbound else "sent",
        body=body,
        requested_at=at,
    )


async def _call(session, org, agent_id, *, person=PERSON, turns=(), status="completed"):
    call = Call(
        organization_id=org,
        agent_id=agent_id,
        from_e164=person,
        to_e164=ORG_NUMBER,
        direction="inbound",
        status=status,
        provider="twilio",
    )
    session.add(call)
    await session.flush()
    for i, (role, text) in enumerate(turns):
        session.add(
            CallEvent(
                call_id=call.id,
                kind="user_turn" if role == "user" else "agent_turn",
                payload={"role": role, "text": text},
                occurred_at=NOW + timedelta(seconds=i),
            )
        )
    await session.flush()
    return call


async def test_merges_texts_and_call_turns_in_time_order(async_session):
    org = uuid.uuid4()
    agent = await _agent(async_session, org)
    async_session.add(_sms(org, agent.id, inbound=True, body="first", at=NOW - timedelta(minutes=5)))
    await _call(async_session, org, agent.id, turns=[("user", "hello"), ("assistant", "hi there")])
    async_session.add(_sms(org, agent.id, inbound=False, body="last", at=NOW + timedelta(minutes=5)))
    await async_session.commit()

    items = await threads.thread_items(async_session, org, agent.id, PERSON)

    assert [(i.kind, i.text) for i in items] == [
        ("text_in", "first"),
        ("call_caller", "hello"),
        ("call_agent", "hi there"),
        ("text_out", "last"),
    ]


async def test_other_caller_and_other_agent_never_appear(async_session):
    org = uuid.uuid4()
    a = await _agent(async_session, org, "a")
    b = await _agent(async_session, org, "b")
    async_session.add(_sms(org, a.id, inbound=True, body="mine"))
    async_session.add(_sms(org, a.id, inbound=True, person=OTHER, body="other caller"))
    async_session.add(_sms(org, b.id, inbound=True, body="other agent"))
    await _call(async_session, org, a.id, person=OTHER, turns=[("user", "secret call")])
    await _call(async_session, org, b.id, turns=[("user", "other agent call")])
    await async_session.commit()

    items = await threads.thread_items(async_session, org, a.id, PERSON)

    assert [i.text for i in items] == ["mine"]


async def test_other_org_never_appears(async_session):
    org, other_org = uuid.uuid4(), uuid.uuid4()
    agent = await _agent(async_session, org)
    async_session.add(_sms(other_org, agent.id, inbound=True, body="other org"))
    await async_session.commit()

    assert await threads.thread_items(async_session, org, agent.id, PERSON) == []


async def test_window_and_limit(async_session):
    org = uuid.uuid4()
    agent = await _agent(async_session, org)
    async_session.add(_sms(org, agent.id, inbound=True, body="old", at=NOW - timedelta(days=8)))
    for n in range(35):
        async_session.add(
            _sms(org, agent.id, inbound=True, body=f"m{n}", at=NOW - timedelta(minutes=40 - n))
        )
    await async_session.commit()

    items = await threads.thread_items(async_session, org, agent.id, PERSON)

    assert len(items) == threads.THREAD_LIMIT
    assert items[-1].text == "m34"
    assert all(i.text != "old" for i in items)


async def test_before_cursor_returns_older_items_only(async_session):
    org = uuid.uuid4()
    agent = await _agent(async_session, org)
    for n in range(5):
        async_session.add(_sms(org, agent.id, inbound=True, body=f"m{n}", at=NOW - timedelta(minutes=10 - n)))
    await async_session.commit()
    items = await threads.thread_items(async_session, org, agent.id, PERSON, limit=2)
    assert [i.text for i in items] == ["m3", "m4"]

    older = await threads.thread_items(async_session, org, agent.id, PERSON, limit=2, before=items[0].id)

    assert [i.text for i in older] == ["m1", "m2"]


async def test_injected_text_turn_is_not_duplicated(async_session):
    org = uuid.uuid4()
    agent = await _agent(async_session, org)
    async_session.add(_sms(org, agent.id, inbound=True, body="my address is 5 Rue X"))
    await _call(
        async_session,
        org,
        agent.id,
        turns=[("user", threads.TEXT_MARKER + "my address is 5 Rue X"), ("user", "did you get it")],
    )
    await async_session.commit()

    items = await threads.thread_items(async_session, org, agent.id, PERSON)

    assert [i.text for i in items] == ["my address is 5 Rue X", "did you get it"]


async def test_thread_item_is_scoped(async_session):
    org = uuid.uuid4()
    a = await _agent(async_session, org, "a")
    b = await _agent(async_session, org, "b")
    row = _sms(org, b.id, inbound=True, body="private")
    async_session.add(row)
    await async_session.commit()

    assert await threads.thread_item(async_session, org, a.id, PERSON, f"sms:{row.id}") is None
    assert (await threads.thread_item(async_session, org, b.id, PERSON, f"sms:{row.id}")).text == "private"
    assert await threads.thread_item(async_session, org, b.id, PERSON, "garbage") is None


async def test_call_thread_key_and_active_call(async_session):
    org = uuid.uuid4()
    agent = await _agent(async_session, org)
    live = await _call(async_session, org, agent.id, status="in_progress")
    done = await _call(async_session, org, agent.id, person=OTHER, status="completed")
    await async_session.commit()

    assert await threads.call_thread_key(async_session, live.id) == (org, agent.id, PERSON)
    assert (await threads.active_call_for_thread(async_session, org, agent.id, PERSON)).id == live.id
    assert await threads.active_call_for_thread(async_session, org, agent.id, OTHER) is None
    assert done.id != live.id


def test_render_cuts_long_text_and_labels_channels():
    long = "x" * 600
    items = [
        threads.ThreadItem("sms:1", NOW, "text_in", long),
        threads.ThreadItem("event:2", NOW, "call_caller", "hello"),
        threads.ThreadItem("event:3", NOW, "call_agent", "hi"),
        threads.ThreadItem("sms:4", NOW, "text_out", "sent"),
    ]

    out = threads.render_thread(items)

    assert "x" * 500 + "..." in out and "x" * 501 not in out
    assert "thread_history" in out  # the cut note names the tool
    assert "caller: hello" in out and "you: hi" in out
    assert "text from caller" in out and "text from you" in out
    assert threads.render_thread(items, cut=None).count("x") == 600
    assert threads.render_thread([]) == ""
```

- [ ] **Step 2: Run to verify failure**

Run: `cd core && uv run pytest tests/test_threads.py -q`
Expected: FAIL (`ModuleNotFoundError: hailhq.core.threads`).

- [ ] **Step 3: Implement `core/hailhq/core/threads.py`**

```python
"""Agent threads: one history per (organization, agent, caller number).

A thread is a query, not a table. It merges the agent's texts with the caller
(``sms``) and the spoken turns of its calls (``call_events``). The caller is
``from_e164`` on inbound rows and ``to_e164`` on outbound rows. Every read here
takes the organization, the agent and the caller from the server; nothing reads
a thread by a number the model supplied.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from hailhq.core.models import Call, CallEvent, Sms
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CUT_CHARS",
    "TEXT_MARKER",
    "THREAD_LIMIT",
    "THREAD_WINDOW",
    "ThreadItem",
    "active_call_for_thread",
    "call_thread_key",
    "render_thread",
    "thread_item",
    "thread_items",
]

THREAD_LIMIT = 30
THREAD_WINDOW = timedelta(days=7)
CUT_CHARS = 500
# Prefix of the conversation item the voicebot adds when the caller texts
# during a call. That text is already an ``sms`` row, so the matching
# ``user_turn`` event is left out of the thread.
TEXT_MARKER = "[text message from caller] "

ItemKind = Literal["text_in", "text_out", "call_caller", "call_agent"]


@dataclass(frozen=True)
class ThreadItem:
    id: str  # "sms:<uuid>" or "event:<uuid>"
    at: datetime
    kind: ItemKind
    text: str


def _sms_filter(organization_id, agent_id, caller):
    return (
        Sms.organization_id == organization_id,
        Sms.agent_id == agent_id,
        or_(
            and_(Sms.direction == "inbound", Sms.from_e164 == caller),
            and_(Sms.direction == "outbound", Sms.to_e164 == caller),
        ),
    )


def _call_filter(organization_id, agent_id, caller):
    return (
        Call.organization_id == organization_id,
        Call.agent_id == agent_id,
        or_(
            and_(Call.direction == "inbound", Call.from_e164 == caller),
            and_(Call.direction == "outbound", Call.to_e164 == caller),
        ),
    )


def _sms_item(row: Sms) -> ThreadItem:
    return ThreadItem(
        id=f"sms:{row.id}",
        at=row.requested_at,
        kind="text_in" if row.direction == "inbound" else "text_out",
        text=row.body or "",
    )


def _event_item(ev: CallEvent) -> ThreadItem | None:
    text = str((ev.payload or {}).get("text", ""))
    if ev.kind == "user_turn" and text.startswith(TEXT_MARKER):
        return None
    if not text.strip():
        return None
    return ThreadItem(
        id=f"event:{ev.id}",
        at=ev.occurred_at,
        kind="call_caller" if ev.kind == "user_turn" else "call_agent",
        text=text,
    )


async def thread_items(
    db: AsyncSession,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller_e164: str,
    *,
    limit: int = THREAD_LIMIT,
    before: str | None = None,
    until: datetime | None = None,
) -> list[ThreadItem]:
    """The last ``limit`` items of the thread inside ``THREAD_WINDOW``, oldest
    first. ``before`` is an item id: only items older than it come back.
    ``until`` drops items after that time."""
    since = datetime.now(timezone.utc) - THREAD_WINDOW
    cutoff = None
    if before is not None:
        anchor = await thread_item(db, organization_id, agent_id, caller_e164, before)
        if anchor is None:
            return []
        cutoff = anchor.at

    sms_stmt = (
        select(Sms)
        .where(*_sms_filter(organization_id, agent_id, caller_e164))
        .where(Sms.requested_at >= since)
        .order_by(Sms.requested_at.desc())
        .limit(limit)
    )
    # Over-fetch events: injected-text turns are dropped after the query.
    ev_stmt = (
        select(CallEvent)
        .join(Call, Call.id == CallEvent.call_id)
        .where(*_call_filter(organization_id, agent_id, caller_e164))
        .where(CallEvent.kind.in_(("user_turn", "agent_turn")))
        .where(CallEvent.occurred_at >= since)
        .order_by(CallEvent.occurred_at.desc())
        .limit(limit * 2)
    )
    items = [_sms_item(r) for r in (await db.execute(sms_stmt)).scalars()]
    for ev in (await db.execute(ev_stmt)).scalars():
        item = _event_item(ev)
        if item is not None:
            items.append(item)
    if cutoff is not None:
        items = [i for i in items if i.at < cutoff]
    if until is not None:
        items = [i for i in items if i.at <= until]
    items.sort(key=lambda i: (i.at, i.id))
    return items[-limit:]


async def thread_item(
    db: AsyncSession,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller_e164: str,
    item_id: str,
) -> ThreadItem | None:
    """One item, only if it belongs to this thread."""
    kind, _, raw = item_id.partition(":")
    try:
        key = uuid.UUID(raw)
    except ValueError:
        return None
    if kind == "sms":
        row = (
            await db.execute(
                select(Sms).where(
                    Sms.id == key, *_sms_filter(organization_id, agent_id, caller_e164)
                )
            )
        ).scalar_one_or_none()
        return _sms_item(row) if row is not None else None
    if kind == "event":
        ev = (
            await db.execute(
                select(CallEvent)
                .join(Call, Call.id == CallEvent.call_id)
                .where(
                    CallEvent.id == key,
                    CallEvent.kind.in_(("user_turn", "agent_turn")),
                    *_call_filter(organization_id, agent_id, caller_e164),
                )
            )
        ).scalar_one_or_none()
        return _event_item(ev) if ev is not None else None
    return None


async def call_thread_key(
    db: AsyncSession, call_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, str] | None:
    """``(organization_id, agent_id, caller_e164)`` for a call, or None when no
    agent is on it. The caller is the person on the line."""
    call = await db.get(Call, call_id)
    if call is None or call.agent_id is None:
        return None
    caller = call.from_e164 if call.direction == "inbound" else call.to_e164
    return call.organization_id, call.agent_id, caller


async def active_call_for_thread(
    db: AsyncSession,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller_e164: str,
) -> Call | None:
    stmt = (
        select(Call)
        .where(*_call_filter(organization_id, agent_id, caller_e164))
        .where(Call.status.in_(("ringing", "in_progress")))
        .order_by(Call.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


_LABEL: dict[str, str] = {
    "text_in": "text from caller",
    "text_out": "text from you",
    "call_caller": "caller",
    "call_agent": "you",
}


def render_thread(items: list[ThreadItem], *, cut: int | None = CUT_CHARS) -> str:
    """Plain-text lines for a prompt. ``cut`` caps one item's text."""
    lines: list[str] = []
    for item in items:
        text = item.text.strip()
        if cut is not None and len(text) > cut:
            text = (
                f"{text[:cut]}... (cut, ask thread_history with "
                f'item_id "{item.id}" for the full text)'
            )
        stamp = item.at.strftime("%Y-%m-%d %H:%M")
        if item.kind.startswith("text"):
            lines.append(f"[{stamp}] {_LABEL[item.kind]}: {text}")
        else:
            lines.append(f"[{stamp}] on a call, {_LABEL[item.kind]}: {text}")
    return "\n".join(lines)
```

Note: the test asserts `"caller: hello"` and `"you: hi"`; the call lines read `on a call, caller: hello` and satisfy it. `"text from caller"` and `"text from you"` match the text labels.

- [ ] **Step 4: Run tests**

Run: `cd core && uv run pytest tests/test_threads.py -q`
Expected: PASS. If `Call.created_at` does not exist, run `grep -n "created_at" core/hailhq/core/models.py` around the `Call` class and use the column that exists (`started_at` is also set on every call row).

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/threads.py core/tests/test_threads.py
git commit -m "feat(threads): thread query over texts and call turns"
```

---

### Task 3: Set `sms.agent_id` on every agent-routed text

**Files:**

- Modify: `core/hailhq/core/sms_ingest.py:~241`
- Modify: `api/hailhq/api/routes/internal/agent.py:307` (the in-call `send_sms` `Sms(...)`)
- Test: `core/tests/test_sms_ingest.py`, `api/tests/test_internal_agent_send.py`

**Interfaces:**

- Consumes: `PhoneNumber.sms_agent_id`, `Call.agent_id`.
- Produces: inbound `Sms.agent_id = number.sms_agent_id` (even when the agent is paused); outbound in-call text `Sms.agent_id = call.agent_id`.

- [ ] **Step 1: Failing tests**

Append to `core/tests/test_text_agent.py` (it has `_seed`, `_ingest`):

```python
async def test_inbound_text_carries_the_numbers_agent(async_session) -> None:
    _, agent, _ = await _seed(async_session)
    result = await _ingest(async_session, "Hello", "SM9")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_id == agent.id


async def test_inbound_text_to_paused_agent_still_belongs_to_it(async_session) -> None:
    _, agent, _ = await _seed(async_session, status="paused")
    result = await _ingest(async_session, "Hello", "SM10")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_id == agent.id
    assert row.agent_reply_state is None


async def test_inbound_text_on_unrouted_number_has_no_agent(async_session) -> None:
    await _seed(async_session, route=False)
    result = await _ingest(async_session, "Hello", "SM11")
    assert (await async_session.get(Sms, result.sms_id)).agent_id is None
```

In `api/tests/test_internal_agent_send.py`, find the existing successful in-call `send-sms` test and add `assert sms.agent_id == call.agent_id` using the same row lookup the test already does (read the test first and reuse its fixtures; the call fixture must have `agent_id` set; if it has none, give it an `Agent` row).

- [ ] **Step 2: Run to verify failure**

Run: `cd core && uv run pytest tests/test_text_agent.py -q -k "carries or paused_agent_still or unrouted"`
Expected: FAIL (`agent_id` is None).

- [ ] **Step 3: Implement**

In `sms_ingest.py`, replace the block:

```python
    if action is None and await should_queue_reply(db, number):
        sms.agent_reply_state = "pending"
```

with:

```python
    # The row belongs to the number's text agent (live or not): that is what
    # puts it in the agent's thread.
    sms.agent_id = number.sms_agent_id
    if action is None and await should_queue_reply(db, number):
        sms.agent_reply_state = "pending"
```

In `routes/internal/agent.py`, in the `Sms(` constructor at ~line 307 add `agent_id=call.agent_id,`.

- [ ] **Step 4: Run tests**

Run: `cd core && uv run pytest tests/test_text_agent.py tests/test_sms_ingest.py -q && cd ../api && uv run pytest tests/test_internal_agent_send.py tests/test_internal_agent_reply.py -q`
Expected: PASS. 🟡 In-call texts now count toward the per-thread reply cap (`replies_in_thread` counts outbound rows with `agent_id`). Per-call send caps are lower than 20, so this is accepted.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/sms_ingest.py api/hailhq/api/routes/internal/agent.py core/tests api/tests
git commit -m "feat(threads): tag agent texts with their agent"
```

---

### Task 4: Choose the SMS number (`resolve_sms_number`)

**Files:**

- Modify: `api/hailhq/api/numbers.py`
- Modify: `api/hailhq/api/routes/internal/agent.py:291-305`
- Test: `api/tests/test_internal_agent_send.py`

**Interfaces:**

- Produces: `async def resolve_sms_number(db, organization_id, agent_id: UUID | None, dialed: PhoneNumber | None) -> PhoneNumber | None` implementing spec §5:
  1. `dialed` if active and has `sms`.
  2. Oldest active org number with `sms` and `sms_agent_id == agent_id`.
  3. Oldest active org number with `sms` and `sms_agent_id IS NULL`: set `sms_agent_id = agent_id`, return it.
  4. None.
- When `agent_id` is None (a call with no agent), return the oldest active org SMS number (old behavior), with no binding change.

- [ ] **Step 1: Failing tests** (add to `api/tests/test_internal_agent_send.py`; it already imports `Call`, `Sms`, `uuid`, `_sms_payload`, `_signed`, and has the `client`, `async_session`, `sms_mock`, `add_phone_number` fixtures. Add `Agent` to its `hailhq.core.models` import.)

```python
async def _inbound_call_on_voice_only_number(session, add_phone_number, *, sms_agent_id="same"):
    """Agent A, voice-only number V (dialed), and one SMS number S."""
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="A", system_prompt="x")
    other = Agent(organization_id=org, name="B", system_prompt="x")
    session.add_all([agent, other])
    await session.commit()
    voice = await add_phone_number(session, org, e164="+14155550001", provider_resource_id="PN_V")
    voice.capabilities = ["voice"]
    voice.voice_agent_id = agent.id
    sms_number = await add_phone_number(session, org, e164="+14155550002", provider_resource_id="PN_S")
    sms_number.capabilities = ["sms"]
    sms_number.sms_agent_id = {"same": agent.id, "other": other.id, "none": None}[sms_agent_id]
    call = Call(
        organization_id=org,
        agent_id=agent.id,
        to_number_id=voice.id,
        from_e164="+14155550123",
        to_e164=voice.e164,
        direction="inbound",
        status="in_progress",
        voice_config={},
        metadata_={CALL_META_BILLED: False},
    )
    session.add(call)
    await session.commit()
    return agent, other, voice, sms_number, call


async def test_voice_only_dialed_number_texts_from_the_agents_sms_number(
    client, async_session, sms_mock, add_phone_number
):
    agent, _, _, sms_number, call = await _inbound_call_on_voice_only_number(
        async_session, add_phone_number, sms_agent_id="same"
    )
    body = _sms_payload(call.id, body="Your code is 42.")
    resp = await client.post("/internal/agent/send-sms", content=body, headers=_signed(body))
    assert resp.json()["ok"] is True
    sent = (await async_session.execute(select(Sms))).scalars().one()
    assert sent.from_number_id == sms_number.id
    assert sent.agent_id == agent.id


async def test_voice_only_dialed_number_binds_an_unbound_sms_number(
    client, async_session, sms_mock, add_phone_number
):
    agent, _, _, sms_number, call = await _inbound_call_on_voice_only_number(
        async_session, add_phone_number, sms_agent_id="none"
    )
    body = _sms_payload(call.id)
    resp = await client.post("/internal/agent/send-sms", content=body, headers=_signed(body))
    assert resp.json()["ok"] is True
    await async_session.refresh(sms_number)
    assert sms_number.sms_agent_id == agent.id


async def test_sms_number_bound_to_another_agent_is_not_taken(
    client, async_session, sms_mock, add_phone_number
):
    _, other, _, sms_number, call = await _inbound_call_on_voice_only_number(
        async_session, add_phone_number, sms_agent_id="other"
    )
    body = _sms_payload(call.id)
    resp = await client.post("/internal/agent/send-sms", content=body, headers=_signed(body))
    data = resp.json()
    assert data["ok"] is False
    assert data["spoken"]
    await async_session.refresh(sms_number)
    assert sms_number.sms_agent_id == other.id
    assert (await async_session.execute(select(Sms))).scalars().all() == []
```

- [ ] **Step 2: Run to verify failure**

Run: `cd api && uv run pytest tests/test_internal_agent_send.py -q -k "agents_sms_number or binds_an_unbound or another_agent"`
Expected: FAIL.

- [ ] **Step 3: Implement**

`api/hailhq/api/numbers.py` (add; imports as needed: `and_`, `select`, `Agent` not needed):

```python
async def resolve_sms_number(
    db: AsyncSession,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    dialed: PhoneNumber | None,
) -> PhoneNumber | None:
    """The org number an agent texts from. Order: the number the person dialed
    (if it can text), a text number already routed to this agent, a text
    number with no agent (it is routed to this agent), else None. A number
    routed to another agent is never taken."""
    if (
        dialed is not None
        and dialed.provisioning_state == "active"
        and "sms" in dialed.capabilities
    ):
        return dialed
    if agent_id is None:
        return await resolve_org_number(db, organization_id, None, capability="sms")
    base = (
        select(PhoneNumber)
        .where(
            PhoneNumber.organization_id == organization_id,
            PhoneNumber.provisioning_state == "active",
            PhoneNumber.capabilities.any("sms"),
        )
        .order_by(PhoneNumber.created_at)
        .limit(1)
    )
    routed = (
        await db.execute(base.where(PhoneNumber.sms_agent_id == agent_id))
    ).scalar_one_or_none()
    if routed is not None:
        return routed
    free = (
        await db.execute(base.where(PhoneNumber.sms_agent_id.is_(None)))
    ).scalar_one_or_none()
    if free is not None:
        free.sms_agent_id = agent_id
        await db.flush()
    return free
```

(Match the imports and `uuid` alias already at the top of the file.)

In `routes/internal/agent.py` replace lines 291-305 (`from_number = None` through the `if from_number is None: return ...`) with:

```python
    # Text from the number the caller dialed when it can text; else a text
    # number of this agent (see resolve_sms_number).
    dialed = (
        await db.get(PhoneNumber, call.to_number_id)
        if call.direction == "inbound" and call.to_number_id is not None
        else None
    )
    from_number = await resolve_sms_number(db, org, call.agent_id, dialed)
    if from_number is None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_SMS_UNCONFIGURED)
```

Add `resolve_sms_number` to that file's import from `hailhq.api.numbers`. The `Sms` insert below runs `db.commit()`, which also saves the binding.

- [ ] **Step 4: Run tests**

Run: `cd api && uv run pytest tests/test_internal_agent_send.py -q`
Expected: PASS (new and old).

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/numbers.py api/hailhq/api/routes/internal/agent.py api/tests/test_internal_agent_send.py
git commit -m "feat(threads): text from a number of the same agent"
```

---

### Task 5: Text agent uses the thread; no auto-reply during a call

**Files:**

- Modify: `core/hailhq/core/text_agent.py` (`build_chat_messages`, remove `thread_messages`, `THREAD_LIMIT`, `THREAD_WINDOW` re-exports as needed)
- Modify: `core/hailhq/core/sms_ingest.py` (skip when a call is active)
- Modify: `voicebot/hailhq/voicebot/textbot.py:~84`
- Test: `core/tests/test_text_agent.py`, `core/tests/test_sms_ingest.py`

**Interfaces:**

- Consumes: `threads.thread_items`, `threads.active_call_for_thread`.
- Produces:
  - `async def thread_history_for_reply(db, sms: Sms, agent: Agent) -> list[ThreadItem]` in `text_agent.py`: `thread_items(db, sms.organization_id, agent.id, sms.from_e164, until=sms.requested_at)`.
  - `build_chat_messages(agent: Agent, history: list[ThreadItem])`: `text_in` and `call_caller` become `user`; `text_out` and `call_agent` become `assistant`. Call items get the prefix `(on a call) `.
  - `thread_lock_key`, `replies_in_thread` and `_thread_filter` stay (the reply cap still counts texts only).

- [ ] **Step 1: Failing tests**

In `core/tests/test_text_agent.py`: find the existing tests that use `thread_messages` / `build_chat_messages` and rewrite them for the new signature, then add:

```python
async def test_reply_history_includes_call_turns(async_session) -> None:
    org, agent, number = await _seed(async_session)
    call = Call(
        organization_id=org, agent_id=agent.id, from_e164=PERSON,
        to_e164=ORG_NUMBER, direction="inbound", status="completed", provider="twilio",
    )
    async_session.add(call)
    await async_session.flush()
    async_session.add(CallEvent(call_id=call.id, kind="user_turn",
                                payload={"role": "user", "text": "I want Tuesday"}))
    await async_session.commit()
    result = await _ingest(async_session, "Did you book it?", "SM20")
    sms = await async_session.get(Sms, result.sms_id)

    history = await text_agent.thread_history_for_reply(async_session, sms, agent)
    messages = text_agent.build_chat_messages(agent, history)

    assert [m["role"] for m in messages] == ["system", "user", "user"]
    assert messages[1]["content"] == "(on a call) I want Tuesday"
    assert messages[2]["content"] == "Did you book it?"
```

(add `Call, CallEvent` to the imports). In `core/tests/test_sms_ingest.py` (or `test_text_agent.py` using its seed) add:

```python
async def test_text_during_an_active_call_is_not_queued(async_session) -> None:
    org, agent, _ = await _seed(async_session)
    async_session.add(Call(
        organization_id=org, agent_id=agent.id, from_e164=PERSON, to_e164=ORG_NUMBER,
        direction="inbound", status="in_progress", provider="twilio",
    ))
    await async_session.commit()
    result = await _ingest(async_session, "my address is 5 Rue X", "SM21")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_reply_state == "skipped"
    assert row.agent_id == agent.id


async def test_text_after_a_finished_call_is_queued(async_session) -> None:
    org, agent, _ = await _seed(async_session)
    async_session.add(Call(
        organization_id=org, agent_id=agent.id, from_e164=PERSON, to_e164=ORG_NUMBER,
        direction="inbound", status="completed", provider="twilio",
    ))
    await async_session.commit()
    result = await _ingest(async_session, "hello again", "SM22")
    assert (await async_session.get(Sms, result.sms_id)).agent_reply_state == "pending"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd core && uv run pytest tests/test_text_agent.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`text_agent.py`: add `from hailhq.core.threads import ThreadItem, thread_items`; delete `thread_messages`, `THREAD_LIMIT`, `THREAD_WINDOW` and their `__all__` entries (keep `_thread_filter`, `replies_in_thread`, `thread_lock_key`; `THREAD_WINDOW` is still used by `replies_in_thread`, so keep a local `REPLY_CAP_WINDOW = timedelta(hours=24)` and use it there; rename the reference). Add:

```python
async def thread_history_for_reply(
    db: AsyncSession, sms: Sms, agent: Agent
) -> list[ThreadItem]:
    """The thread up to and including ``sms``: texts and call turns."""
    return await thread_items(
        db, sms.organization_id, agent.id, sms.from_e164, until=sms.requested_at
    )
```

Replace `build_chat_messages`:

```python
def build_chat_messages(agent: Agent, history: list[ThreadItem]) -> list[dict[str, Any]]:
    """OpenAI-style messages: system (preamble + instructions) then the
    thread, the caller as ``user`` and the agent as ``assistant``. Call turns
    carry an ``(on a call)`` prefix."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_text_instructions(agent.system_prompt)}
    ]
    for item in history:
        role = "user" if item.kind in ("text_in", "call_caller") else "assistant"
        prefix = "(on a call) " if item.kind.startswith("call") else ""
        messages.append({"role": role, "content": prefix + item.text})
    return messages
```

`textbot.py` `_prepare`:

```python
    async with session_scope() as db:
        history = await thread_history_for_reply(db, claimed.sms, claimed.agent)
    return build_chat_messages(claimed.agent, history)
```

(update the import list: drop `thread_messages`, add `thread_history_for_reply`).

`sms_ingest.py`, replace the block from Task 3:

```python
    sms.agent_id = number.sms_agent_id
    if action is None and await should_queue_reply(db, number):
        if await active_call_for_thread(
            db, organization_id, number.sms_agent_id, from_e164
        ):
            # The voice agent on the line gets this text (voicebot text watch).
            sms.agent_reply_state = "skipped"
        else:
            sms.agent_reply_state = "pending"
```

Import `active_call_for_thread` from `hailhq.core.threads` (use `organization_id` as the variable already in scope; check its name in the function and match it).

- [ ] **Step 4: Run tests**

Run: `cd core && uv run pytest tests -q -x && cd ../voicebot && uv run pytest tests -q -x`
Expected: PASS. Fix remaining references with `grep -rn "thread_messages\|THREAD_LIMIT\|THREAD_WINDOW" core voicebot api --include='*.py'`.

- [ ] **Step 5: Commit**

```bash
git add core voicebot
git commit -m "feat(threads): text agent reads texts and call turns"
```

---

### Task 6: Voice agent gets the thread at call start

**Files:**

- Modify: `core/hailhq/core/prompts.py` (`build_voice_instructions`)
- Modify: `voicebot/hailhq/voicebot/agent.py` (`build_instructions`, call setup near line 1495)
- Test: `core/tests/test_prompts.py`, `voicebot/tests/test_agent_inbound.py`

**Interfaces:**

- Consumes: `threads.call_thread_key`, `threads.thread_items`, `threads.render_thread`.
- Produces:
  - `build_voice_instructions(system_prompt, direction=None, history: str | None = None)`: when `history` is non-empty, appends `"\n\n# Earlier with this caller\n\n{history}"` after the caller instructions. Heading constant `HISTORY_HEADING = "# Earlier with this caller"`.
  - `async def load_thread_context(call_id: UUID) -> str | None` in `voicebot/hailhq/voicebot/agent.py`: own `session_scope`, returns rendered thread (cut at `CUT_CHARS`) or None on no agent, no items, or any exception (logged).
  - `build_instructions(system_prompt, direction=None, history=None)` passes `history` through.

- [ ] **Step 1: Failing tests**

`core/tests/test_prompts.py`:

```python
def test_voice_instructions_append_history_section():
    out = build_voice_instructions("Be kind.", "inbound", history="[2026-10-06 10:00] text from caller: hi")
    assert out.endswith("# Earlier with this caller\n\n[2026-10-06 10:00] text from caller: hi")
    assert "# Caller instructions\n\nBe kind." in out


def test_voice_instructions_without_history_are_unchanged():
    assert build_voice_instructions("Be kind.", "inbound") == build_voice_instructions(
        "Be kind.", "inbound", history=None
    )
    assert "Earlier with this caller" not in build_voice_instructions("x", None, history="")
```

`voicebot/tests/test_agent_inbound.py` (the `async_session` fixture points production `session_scope()` at the test database, so no patching is needed; add `from datetime import datetime, timezone`, `from hailhq.core.models import Agent, Call, Sms`):

```python
async def test_load_thread_context_renders_the_callers_texts(async_session) -> None:
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="a", system_prompt="x")
    async_session.add(agent)
    await async_session.flush()
    call = Call(organization_id=org, agent_id=agent.id, from_e164="+33612345678",
                to_e164="+14155550100", direction="inbound", status="in_progress",
                provider="twilio")
    async_session.add(call)
    async_session.add(Sms(organization_id=org, agent_id=agent.id, provider="twilio",
                          from_e164="+33612345678", to_e164="+14155550100",
                          direction="inbound", status="received", body="my order is 4411",
                          requested_at=datetime.now(timezone.utc)))
    await async_session.commit()

    text = await agent_mod.load_thread_context(call.id)

    assert text is not None and "my order is 4411" in text


async def test_load_thread_context_is_none_without_an_agent(async_session) -> None:
    assert await agent_mod.load_thread_context(uuid.uuid4()) is None


async def test_load_thread_context_is_none_when_the_read_fails(monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(agent_mod.threads, "call_thread_key", boom)
    assert await agent_mod.load_thread_context(uuid.uuid4()) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `cd core && uv run pytest tests/test_prompts.py -q && cd ../voicebot && uv run pytest tests/test_agent_inbound.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`prompts.py`:

```python
HISTORY_HEADING = "# Earlier with this caller"


def build_voice_instructions(
    system_prompt: str | None,
    direction: str | None = None,
    history: str | None = None,
) -> str:
    preamble = VOICE_PREAMBLE_INBOUND if direction == "inbound" else VOICE_PREAMBLE
    caller = (system_prompt or "").strip()
    out = preamble if not caller else f"{preamble}\n\n{VOICE_HEADING}\n\n{caller}"
    if history and history.strip():
        out = f"{out}\n\n{HISTORY_HEADING}\n\n{history.strip()}"
    return out
```

(Keep the existing docstring, add one line for `history`.)

`voicebot/agent.py`: add `from hailhq.core import threads` and

```python
async def load_thread_context(call_id: UUID) -> str | None:
    """This caller's earlier texts and calls with this agent, rendered for the
    prompt. None when there is nothing, or when the read fails: a call never
    waits on its history."""
    try:
        async with session_scope() as db:
            key = await threads.call_thread_key(db, call_id)
            if key is None:
                return None
            org, agent_id, caller = key
            items = await threads.thread_items(db, org, agent_id, caller)
        return threads.render_thread(items) or None
    except Exception:
        logger.exception("call_id=%s thread history unavailable", call_id)
        return None
```

Change `build_instructions` to accept and forward `history`. In the entrypoint before `SpeechSanitizingAgent(...)` (~line 1495):

```python
    history = await load_thread_context(call_id)
    agent = SpeechSanitizingAgent(
        instructions=build_instructions(
            metadata.get("system_prompt"), metadata.get("direction"), history
        ),
        tools=agent_tools,
    )
```

Note: the current call's own turns are not in `call_events` yet at this point, so there is no overlap.

- [ ] **Step 4: Run tests**

Run: `cd core && uv run pytest tests/test_prompts.py -q && cd ../voicebot && uv run pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core voicebot
git commit -m "feat(threads): voice agent starts with the caller's history"
```

---

### Task 7: Live texts during a call (`voicebot/text_watch.py`)

**Files:**

- Create: `voicebot/hailhq/voicebot/text_watch.py`
- Modify: `voicebot/hailhq/voicebot/agent.py` (start the watcher after `session.start`, cancel in `_shutdown`)
- Test: `voicebot/tests/test_text_watch.py`

**Interfaces:**

- Consumes: `threads.call_thread_key`, `threads.TEXT_MARKER`, `Sms`.
- Produces:
  - `POLL_SECONDS = 2.0`, `MAX_INJECT_CHARS = 1000`
  - `async def new_inbound_texts(db, organization_id, agent_id, caller_e164, after: datetime) -> list[Sms]` (inbound, same agent and caller, `requested_at > after`, oldest first).
  - `async def watch_incoming_texts(session, call_id: UUID, *, poll_seconds: float = POLL_SECONDS) -> None`: loop until cancelled; each new text calls `session.generate_reply(user_input=TEXT_MARKER + body[:MAX_INJECT_CHARS])`.

- [ ] **Step 1: Check the LiveKit call signature**

Run: `cd voicebot && uv run python -c "import inspect; from livekit.agents.voice import AgentSession as A; print(inspect.signature(A.generate_reply))"`
Expected: signature that includes `user_input`. If it does not, use the form it does have for adding a user message (`session.history.add_message(role="user", content=...)` or `agent.update_chat_ctx`) followed by `session.generate_reply()`, and change only the one injection line in Step 3 and in the test's fake.

- [ ] **Step 2: Failing tests** `voicebot/tests/test_text_watch.py`

```python
"""Mid-call texts reach the live voice session."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from hailhq.core import threads
from hailhq.core.models import Agent, Call, Sms
from hailhq.voicebot import text_watch

ORG_NUMBER = "+14155550100"
PERSON = "+33612345678"


class FakeSession:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def generate_reply(self, **kwargs) -> None:
        self.inputs.append(kwargs["user_input"])


async def _seed_call(async_session):
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="a", system_prompt="x")
    async_session.add(agent)
    await async_session.flush()
    call = Call(organization_id=org, agent_id=agent.id, from_e164=PERSON,
                to_e164=ORG_NUMBER, direction="inbound", status="in_progress",
                provider="twilio")
    async_session.add(call)
    await async_session.commit()
    return org, agent, call


def _text(org, agent_id, body, *, person=PERSON, at=None):
    return Sms(organization_id=org, agent_id=agent_id, provider="twilio",
               from_e164=person, to_e164=ORG_NUMBER, direction="inbound",
               status="received", body=body,
               requested_at=at or datetime.now(timezone.utc))


async def test_new_inbound_texts_only_for_this_thread(async_session):
    org, agent, call = await _seed_call(async_session)
    after = datetime.now(timezone.utc) - timedelta(seconds=1)
    async_session.add(_text(org, agent.id, "mine"))
    async_session.add(_text(org, agent.id, "other caller", person="+33600000000"))
    async_session.add(_text(org, agent.id, "too old", at=after - timedelta(minutes=1)))
    await async_session.commit()

    rows = await text_watch.new_inbound_texts(async_session, org, agent.id, PERSON, after)

    assert [r.body for r in rows] == ["mine"]


async def test_watcher_injects_a_new_text_once(async_session):
    org, agent, call = await _seed_call(async_session)
    fake = FakeSession()
    task = asyncio.create_task(text_watch.watch_incoming_texts(fake, call.id, poll_seconds=0.05))
    await asyncio.sleep(0.1)
    async_session.add(_text(org, agent.id, "my address is 5 Rue X"))
    await async_session.commit()
    await asyncio.sleep(0.3)
    task.cancel()

    assert fake.inputs == [threads.TEXT_MARKER + "my address is 5 Rue X"]


async def test_long_text_is_cut_before_injection(async_session):
    org, agent, call = await _seed_call(async_session)
    fake = FakeSession()
    task = asyncio.create_task(text_watch.watch_incoming_texts(fake, call.id, poll_seconds=0.05))
    await asyncio.sleep(0.1)
    async_session.add(_text(org, agent.id, "y" * 5000))
    await async_session.commit()
    await asyncio.sleep(0.3)
    task.cancel()

    assert len(fake.inputs[0]) == len(threads.TEXT_MARKER) + text_watch.MAX_INJECT_CHARS
```

The `async_session` fixture installs the test sessionmaker for production `session_scope()`, so the watcher reads the test database with no patching.

- [ ] **Step 3: Run to verify failure**

Run: `cd voicebot && uv run pytest tests/test_text_watch.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 4: Implement `voicebot/hailhq/voicebot/text_watch.py`**

```python
"""Feed a text the caller sends during a call into the live voice session.

The API marks such a text's auto-reply ``skipped`` (see
``hailhq.core.sms_ingest``). This watcher polls ``sms`` for new inbound texts
of the call's thread and hands each to the voice agent as a user message.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from hailhq.core import threads
from hailhq.core.db import session_scope
from hailhq.core.models import Sms
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("hailhq.voicebot")

POLL_SECONDS = 2.0
MAX_INJECT_CHARS = 1000


async def new_inbound_texts(
    db: AsyncSession,
    organization_id: UUID,
    agent_id: UUID,
    caller_e164: str,
    after: datetime,
) -> list[Sms]:
    stmt = (
        select(Sms)
        .where(
            Sms.organization_id == organization_id,
            Sms.agent_id == agent_id,
            Sms.direction == "inbound",
            Sms.from_e164 == caller_e164,
            Sms.requested_at > after,
        )
        .order_by(Sms.requested_at)
    )
    return list((await db.execute(stmt)).scalars().all())


async def watch_incoming_texts(
    session: Any, call_id: UUID, *, poll_seconds: float = POLL_SECONDS
) -> None:
    """Run until cancelled. A failed poll is logged and retried."""
    try:
        async with session_scope() as db:
            key = await threads.call_thread_key(db, call_id)
    except Exception:
        logger.exception("call_id=%s text watch could not start", call_id)
        return
    if key is None:
        return
    org, agent_id, caller = key
    cursor = datetime.now(timezone.utc)
    while True:
        await asyncio.sleep(poll_seconds)
        try:
            async with session_scope() as db:
                rows = await new_inbound_texts(db, org, agent_id, caller, cursor)
            for row in rows:
                cursor = max(cursor, row.requested_at)
                session.generate_reply(
                    user_input=threads.TEXT_MARKER + (row.body or "")[:MAX_INJECT_CHARS]
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("call_id=%s text watch poll failed", call_id)
```

In `voicebot/agent.py` after `await session.start(agent=agent, room=ctx.room)` add:

```python
    text_watch_task = asyncio.create_task(text_watch.watch_incoming_texts(session, call_id))
```

(import `from hailhq.voicebot import text_watch`). Cancel it where `_shutdown` cancels the soft cap task: find that block with `grep -n "soft_cap_task" voicebot/hailhq/voicebot/agent.py` and add the same `cancel()` call for `text_watch_task` on every exit path that cancels `soft_cap_task`. Because `_shutdown` is defined before the task exists, declare `text_watch_task: asyncio.Task[None] | None = None` next to `captured` earlier in the entrypoint and assign with `nonlocal`/the same closure pattern that `soft_cap_task` uses.

- [ ] **Step 5: Run tests**

Run: `cd voicebot && uv run pytest tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add voicebot
git commit -m "feat(threads): caller texts reach the live call"
```

---

### Task 8: `thread_history` tool

**Files:**

- Create: `core/hailhq/core/agent_tools/thread_history.py`
- Modify: `core/hailhq/core/agent_tools/registry.py`
- Test: `core/tests/test_agent_tools.py`

**Interfaces:**

- Consumes: `threads.call_thread_key`, `thread_items`, `thread_item`, `render_thread`.
- Produces: `SPEC` named `thread_history`, tier `read_only`, parameters `before` (string), `limit` (integer 1 to 30), `item_id` (string). No number parameter. Always available.
  - With `item_id`: the full text of that item, or `"I can't find that message."`.
  - Else: up to `limit` (default 10) items older than `before` (or the newest when no `before`), rendered with `cut=CUT_CHARS`; `"There is nothing earlier."` when empty.

- [ ] **Step 1: Failing tests** (append to `core/tests/test_agent_tools.py`; update the names set in `test_registry_names_and_tiers` to include `"thread_history"` and add `assert tools["thread_history"].risk_tier == "read_only"`)

```python
async def _thread_call(session):
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="a", system_prompt="x")
    session.add(agent)
    await session.flush()
    call = Call(organization_id=org, agent_id=agent.id, from_e164="+33612345678",
                to_e164="+14155550100", direction="inbound", status="in_progress",
                provider="twilio")
    session.add(call)
    session.add(Sms(organization_id=org, agent_id=agent.id, provider="twilio",
                    from_e164="+33612345678", to_e164="+14155550100",
                    direction="inbound", status="received", body="z" * 700))
    session.add(Sms(organization_id=org, agent_id=agent.id, provider="twilio",
                    from_e164="+33699999999", to_e164="+14155550100",
                    direction="inbound", status="received", body="someone else"))
    await session.commit()
    return org, call


async def test_thread_history_lists_only_this_callers_items(async_session):
    org, call = await _thread_call(async_session)
    tools = {t.name: t for t in all_tools()}

    out = await tools["thread_history"].execute(_ctx(call_id=call.id, organization_id=org), {})

    assert "z" * 500 in out and "someone else" not in out


async def test_thread_history_returns_full_text_for_an_item(async_session):
    org, call = await _thread_call(async_session)
    tools = {t.name: t for t in all_tools()}
    listing = await tools["thread_history"].execute(_ctx(call_id=call.id, organization_id=org), {})
    item_id = listing.split('item_id "')[1].split('"')[0]

    full = await tools["thread_history"].execute(
        _ctx(call_id=call.id, organization_id=org), {"item_id": item_id}
    )

    assert "z" * 700 in full


async def test_thread_history_refuses_an_item_of_another_caller(async_session):
    org, call = await _thread_call(async_session)
    other = (await async_session.execute(
        select(Sms).where(Sms.body == "someone else"))).scalar_one()
    tools = {t.name: t for t in all_tools()}

    out = await tools["thread_history"].execute(
        _ctx(call_id=call.id, organization_id=org), {"item_id": f"sms:{other.id}"}
    )

    assert out == "I can't find that message."


async def test_thread_history_without_agent_on_call(async_session):
    org = uuid.uuid4()
    tools = {t.name: t for t in all_tools()}
    out = await tools["thread_history"].execute(_ctx(call_id=uuid.uuid4(), organization_id=org), {})
    assert out == "There is nothing earlier."
```

The `async_session` fixture installs the test sessionmaker for production `session_scope()`, so the tool reads the test database with no patching. Add `Agent, Call, Sms` and `select` to the file's imports.

- [ ] **Step 2: Run to verify failure**

Run: `cd core && uv run pytest tests/test_agent_tools.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement `thread_history.py`**

```python
"""thread_history — read the earlier texts and calls with this caller.

Read-only. The caller is found from the call on the server; no number is ever
a parameter, so the agent cannot read another person's thread.
"""

from __future__ import annotations

import uuid
from typing import Any

from hailhq.core import threads
from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.db import session_scope
from sqlalchemy.ext.asyncio import AsyncSession

_NOT_FOUND = "I can't find that message."
_EMPTY = "There is nothing earlier."
_DEFAULT_LIMIT = 10


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    return True


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> str:
    item_id = args.get("item_id")
    before = args.get("before")
    try:
        limit = max(1, min(int(args.get("limit", _DEFAULT_LIMIT)), threads.THREAD_LIMIT))
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    async with session_scope() as db:
        key = await threads.call_thread_key(db, ctx.call_id)
        if key is None or key[0] != ctx.organization_id:
            return _NOT_FOUND if item_id else _EMPTY
        org, agent_id, caller = key
        if item_id:
            item = await threads.thread_item(db, org, agent_id, caller, str(item_id))
            return item.text if item is not None else _NOT_FOUND
        items = await threads.thread_items(
            db, org, agent_id, caller, limit=limit, before=str(before) if before else None
        )
    return threads.render_thread(items) or _EMPTY


SPEC = ToolSpec(
    name="thread_history",
    description=(
        "Read earlier texts and calls with the person on this call. Use it for "
        "older messages, or pass item_id to get the full text of one message "
        "that was cut short. You can only read this person's history."
    ),
    parameters={
        "type": "object",
        "properties": {
            "before": {
                "type": "string",
                "description": "An item id from an earlier result. Returns older items.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": threads.THREAD_LIMIT,
                "description": "How many items to return. Default 10.",
            },
            "item_id": {
                "type": "string",
                "description": "Return the full text of this one item.",
            },
        },
        "required": [],
    },
    risk_tier="read_only",
    is_available=_always,
    execute=_execute,
)
```

`registry.py`: import `thread_history` and add `thread_history.SPEC,` to `all_tools()`.

- [ ] **Step 4: Run tests**

Run: `cd core && uv run pytest tests/test_agent_tools.py -q && cd ../voicebot && uv run pytest tests -q`
Expected: PASS. Note: an agent whose `tools` list is explicit and lacks `thread_history` keeps the prompt history but not the tool. Existing `tools: null` agents get it.

- [ ] **Step 5: Commit**

```bash
git add core
git commit -m "feat(threads): thread_history tool for the voice agent"
```

---

### Task 9: End-to-end test, docs, full run

**Files:**

- Create: `core/tests/test_threads_flow.py`
- Modify: `docs/public/agents.md`
- Modify: `docs/superpowers/specs/2026-10-07-agent-threads-design.md` (two spec amendments, see Step 3)

- [ ] **Step 1: Write the flow test** `core/tests/test_threads_flow.py`

```python
"""Voice-only number + SMS number: call, text, call again."""

from __future__ import annotations

import uuid

from hailhq.core import threads
from hailhq.core.models import Agent, Call, CallEvent, PhoneNumber
from hailhq.core.sms_ingest import ingest_inbound_sms

VOICE_NUMBER = "+33100000001"
SMS_NUMBER = "+33100000002"
PERSON = "+33612345678"


async def test_text_on_the_sms_number_shows_in_the_next_calls_thread(async_session):
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="Desk", system_prompt="Help.")
    async_session.add(agent)
    await async_session.flush()
    for e164, caps, kw in (
        (VOICE_NUMBER, ["voice"], {"voice_agent_id": agent.id}),
        (SMS_NUMBER, ["sms"], {"sms_agent_id": agent.id}),
    ):
        async_session.add(PhoneNumber(
            organization_id=org, e164=e164, country_code="FR", number_type="local",
            provisioning_state="active", provider_resource_id=e164, capabilities=caps, **kw,
        ))
    first = Call(organization_id=org, agent_id=agent.id, from_e164=PERSON, to_e164=VOICE_NUMBER,
                 direction="inbound", status="completed", provider="twilio")
    async_session.add(first)
    await async_session.flush()
    async_session.add(CallEvent(call_id=first.id, kind="user_turn",
                                payload={"role": "user", "text": "I will text you my order"}))
    await async_session.commit()

    await ingest_inbound_sms(async_session, from_e164=PERSON, to_e164=SMS_NUMBER,
                             body="order 4411", provider_message_sid="SMX", opt_out_type=None,
                             carrier="twilio")
    second = Call(organization_id=org, agent_id=agent.id, from_e164=PERSON, to_e164=VOICE_NUMBER,
                  direction="inbound", status="in_progress", provider="twilio")
    async_session.add(second)
    await async_session.commit()

    key = await threads.call_thread_key(async_session, second.id)
    items = await threads.thread_items(async_session, *key)

    assert [i.text for i in items] == ["I will text you my order", "order 4411"]
```

- [ ] **Step 2: Run it**

Run: `cd core && uv run pytest tests/test_threads_flow.py -q`
Expected: PASS.

- [ ] **Step 3: Docs and spec amendments**

`docs/public/agents.md`: add a short section "Threads" (lead with the example: caller texts an order number on the SMS number, then calls; the agent greets knowing it). State: scope is agent + caller number; calls and texts both count; the last 30 items from the last 7 days are in the prompt; the voice agent has the `thread_history` tool; a text sent during a call goes to the voice agent and the text agent does not reply; `send_sms` uses the dialed number if it can text, else a text number of the same agent, else an unbound org text number (which becomes this agent's), and never takes a number bound to another agent. Keep it under one screen.

Amend the spec in the same commit:

- §1: replace "Add index on `sms` (...)" and "Add the same index on `calls`" with: "Add two indexes per table, one per caller column (`from_e164`, `to_e164`), because the caller sits in a different column by direction."
- §5 step 4: replace "None found: the tool is hidden. The agent prompt says it cannot text." with "None found: the agent tells the caller it cannot text (the tool stays listed while the org has any SMS number; it is hidden when the org has none)."

- [ ] **Step 4: Full verification**

Run:

```bash
cd core && uv run pytest tests -q && cd ../api && uv run pytest tests -q && cd ../voicebot && uv run pytest tests -q && cd .. && uv run ruff check . && uv run black --check core api voicebot
```

Expected: all PASS, ruff and black clean. If `openapi/openapi.yaml` check exists in CI, no regeneration is needed (no public route changed); confirm with `cd api && uv run pytest tests/test_openapi_descriptions.py -q`.

- [ ] **Step 5: Commit**

```bash
git add core/tests/test_threads_flow.py docs
git commit -m "docs(threads): agents guide and spec amendments"
```
