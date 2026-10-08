# Human Handover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The voice agent can connect the caller to a contact the admin picked for that agent, by dialing the contact into the same LiveKit room.

**Architecture:** A join table links agents to contacts. The dispatch metadata carries the names and notes (no numbers). A new core tool `transfer_call` asks the API (`/internal/agent/handover`) for the route, then calls a voicebot transport handle that dials the contact with `create_sip_participant`, says one line, and mutes the agent. The API records the result (`/internal/agent/handover-result`): a `handover` call event and, on answer, the `call.transferred` webhook. Billing is unchanged because the call ends through the normal `on_call_end`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, livekit-agents 1.6 / livekit-api 1.2, pytest. Console: Next.js (hail-website), plain CSS in `app/console/console.css`, vitest.

**Spec:** `docs/superpowers/specs/2026-10-08-human-handover-design.md`

## Global Constraints

- Max 10 handover contacts per agent. Note 1–200 chars. `reason` tool argument capped at 200 chars.
- Ring timeout 30 s. Dial identity `human-{call_id}`. Caller ID = the Hail number on the call (`from_e164` outbound, `to_e164` inbound).
- The LLM never receives a phone number: not in the tool description, parameters, result text, or `tool_call` event.
- `check_call_allowed` runs on save AND before dialing.
- Destination guard: the contact's country must have a row in a carrier catalog (`costs/<carrier>.json`); at dial time, in the call's own carrier catalog.
- One connected handover per call.
- Billing code is not touched.
- `core` must not import `livekit` (enforced by `core/tests/test_agent_tools.py`).
- After any API route change: regenerate `openapi/openapi.yaml` and run `cd cli && make codegen` in the same PR.
- Never `uv sync --extra dev` inside a subpackage. Use `uv sync --all-packages --all-extras` at the repo root.
- Conventional Commits. No AI co-author trailer.

## Review Focus

1. Caller hangs up while the contact is still ringing → the contact's phone stops ringing, the call ends as a normal hang-up (Task 7 test `test_caller_leaves_while_ringing_deletes_room`).
2. The contact's leg fails or is declined → the CALL must not be marked busy/no_answer; only the handover event records it (Task 7 test `test_human_leg_disconnect_does_not_restamp_call`).
3. Two contacts with the same name → both stay selectable with distinct labels (Task 2 test `test_targets_dedupe_labels`).
4. Agent has a `tools` allowlist without `transfer_call` → the tool stays hidden even with contacts set; the console adds `transfer_call` to the list when contacts are set (Task 9 test `formToInput adds transfer_call`).
5. A contact's phone is removed after it was linked → skipped at call time, not offered to the LLM (Task 2 test `test_targets_skip_contacts_without_phone`).

---

## File map

Backend (`hail`, branch `feat/human-handover`):

| File | Change |
|---|---|
| `core/hailhq/core/models.py` | add `AgentHandoverContact` |
| `api/migrations/versions/0053_agent_handover_contacts.py` | new table |
| `core/hailhq/core/telephony_catalog.py` | add `sells_in(country_code, provider)` |
| `core/hailhq/core/handover.py` | new: validate, replace, load, targets, dial check |
| `core/hailhq/core/schemas.py` | agent fields, `call.transferred` |
| `api/hailhq/api/routes/agents.py` | save + return handover contacts |
| `api/hailhq/api/routes/calls.py`, `core/hailhq/core/inbound_calls.py` | metadata `handover_targets` |
| `api/hailhq/api/routes/internal/agent.py` | `/handover`, `/handover-result` |
| `core/hailhq/core/agent_tools/spec.py` | `ToolSpec.bind`, `ToolContext.bridge`, `BridgeRoute`, `BridgeOutcome` |
| `core/hailhq/core/agent_tools/transfer_call.py` | new tool |
| `core/hailhq/core/agent_tools/registry.py` | register |
| `voicebot/hailhq/voicebot/tools.py` | apply `bind`, pass `bridge` |
| `voicebot/hailhq/voicebot/agent.py` | `make_agent_bridge`, disconnect handling |
| `openapi/openapi.yaml`, `cli/…`, `mcp/hailhq/mcp/tools.py` | regen + agent field |
| `docs/public/agents.md`, `docs/public/webhooks.md` (whichever lists call events), `CHANGELOG.md` | docs |

Console (`hail-website`, branch `feat/human-handover`):

| File | Change |
|---|---|
| `lib/agent-queries.ts` | `handoverContacts` on `AgentRow`, `phoneContacts(orgId)` |
| `app/console/agents/actions.ts` | `handover_contacts` in `AgentInput` |
| `app/console/agents/agent-form.ts` | form state, `formToInput`, `validateForm` |
| `app/console/agents/HandoverStep.tsx` | new UI step |
| `app/console/agents/AgentSheet.tsx`, `new/page.tsx`, `[id]/page.tsx` | wire step + contacts prop |
| `app/console/activity/ActivityDrawer.tsx` | `handover` event line |
| `app/console/console.css` | `ag-ho-*` styles |

---

### Task 1: Table, model, catalog helper

**Files:**
- Modify: `core/hailhq/core/models.py` (after `class Agent`, ~line 458)
- Create: `api/migrations/versions/0053_agent_handover_contacts.py`
- Modify: `core/hailhq/core/telephony_catalog.py`
- Test: `core/tests/test_telephony_catalog.py` (exists; add tests)

**Interfaces:**
- Produces: `AgentHandoverContact(agent_id, contact_id, note, position, created_at)`; `telephony_catalog.sells_in(country_code: str, provider: str = "auto") -> bool`.

- [ ] **Step 1: Failing test for `sells_in`**

```python
from hailhq.core import telephony_catalog


def test_sells_in_known_country() -> None:
    assert telephony_catalog.sells_in("US") is True
    assert telephony_catalog.sells_in("US", "twilio") is True


def test_sells_in_unknown_country() -> None:
    assert telephony_catalog.sells_in("ZZ") is False
    assert telephony_catalog.sells_in("ZZ", "twilio") is False
```

- [ ] **Step 2: Run, expect FAIL** — `cd core && uv run pytest tests/test_telephony_catalog.py -k sells_in -v` → `AttributeError: ... has no attribute 'sells_in'`.

- [ ] **Step 3: Implement**

```python
def sells_in(country_code: str, provider: str = "auto") -> bool:
    """True when ``provider`` (or any carrier the API buys from, for 'auto')
    lists any number in ``country_code``. The handover destination guard:
    a flat per-minute rate only covers countries Hail already sells in."""
    from hailhq.core.number_offers import PROVIDERS

    providers = PROVIDERS if provider == "auto" else (provider,)
    return any(cc == country_code for name in providers for cc, _ in _load(name))
```

Add `"sells_in"` to `__all__`.

- [ ] **Step 4: Model** in `models.py`, after `Agent`:

```python
class AgentHandoverContact(Base):
    """A contact the agent may hand a live call over to (spec:
    docs/superpowers/specs/2026-10-08-human-handover-design.md)."""

    __tablename__ = "agent_handover_contacts"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "char_length(note) BETWEEN 1 AND 200", name="agent_handover_note_len"
        ),
    )
```

Import `SmallInteger` / `ForeignKey` from sqlalchemy if not already imported in `models.py`.

- [ ] **Step 5: Migration** `api/migrations/versions/0053_agent_handover_contacts.py`:

```python
"""Contacts an agent may hand a live call over to.

Revision ID: 0053
Revises: 0052
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_handover_contacts",
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(note) BETWEEN 1 AND 200", name="agent_handover_note_len"
        ),
    )
    op.create_index(
        "agent_handover_contacts_contact_idx",
        "agent_handover_contacts",
        ["contact_id"],
    )


def downgrade() -> None:
    op.drop_index("agent_handover_contacts_contact_idx")
    op.drop_table("agent_handover_contacts")
```

Check `git ls-files api/migrations/versions | sort | tail -2` first; if main moved past 0052, renumber.

- [ ] **Step 6: Run** `cd core && uv run pytest tests/test_telephony_catalog.py -v` → PASS. Then the migration test the repo already has: `cd api && uv run pytest tests -k "migration" -v` → PASS.

- [ ] **Step 7: Commit** `git add -A && git commit -m "feat(core): agent_handover_contacts table and catalog sells_in"`

---

### Task 2: Core handover helpers

**Files:**
- Create: `core/hailhq/core/handover.py`
- Test: `core/tests/test_handover.py`

**Interfaces:**
- Consumes: `AgentHandoverContact`, `Contact`, `check_call_allowed`, `sells_in`.
- Produces:
  - `MAX_HANDOVER_CONTACTS = 10`
  - `@dataclass(frozen=True) class HandoverItem: contact_id: UUID; note: str`
  - `class HandoverInvalid(ValueError)` with `.index: int | None`
  - `async def validate_handover(db, org_id: UUID, items: list[HandoverItem]) -> None` — raises `HandoverInvalid`
  - `async def replace_handover(db, agent_id: UUID, items: list[HandoverItem]) -> None` — no commit
  - `async def load_handover(db, agent_ids: list[UUID]) -> dict[UUID, list[dict]]` — `{agent_id: [{contact_id, name, phone_e164, note}]}` ordered by position
  - `async def handover_targets(db, agent_id: UUID | None) -> list[dict]` — `[{contact_id: str, label: str, note: str}]`, phone-less skipped, labels unique
  - `def country_of(e164: str) -> str | None`

- [ ] **Step 1: Failing tests** `core/tests/test_handover.py` (use the core `async_session` fixture the other core DB tests use, e.g. `core/tests/test_threads.py`):

```python
import uuid

import pytest
from hailhq.core.compliance_gate import add_suppression
from hailhq.core.handover import (
    HandoverInvalid,
    HandoverItem,
    country_of,
    handover_targets,
    load_handover,
    replace_handover,
    validate_handover,
)
from hailhq.core.models import Agent, Contact


async def _agent_and_contacts(s, *phones):
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="Desk", system_prompt="Help.")
    s.add(agent)
    contacts = []
    for i, phone in enumerate(phones):
        c = Contact(
            organization_id=org,
            name=f"Person {i}",
            phone_e164=phone,
            email=None if phone else f"p{i}@example.com",
        )
        s.add(c)
        contacts.append(c)
    await s.flush()
    return org, agent, contacts


def test_country_of() -> None:
    assert country_of("+14155550100") == "US"
    assert country_of("+447700900123") == "GB"
    assert country_of("+999") is None


async def test_validate_ok(async_session) -> None:
    org, _, (c,) = await _agent_and_contacts(async_session, "+14155550101")
    await validate_handover(async_session, org, [HandoverItem(c.id, "Billing")])


@pytest.mark.parametrize("case", ["other_org", "no_phone", "duplicate", "too_many"])
async def test_validate_rejects(async_session, case) -> None:
    org, _, (c, nophone) = await _agent_and_contacts(
        async_session, "+14155550102", None
    )
    items = {
        "other_org": [HandoverItem(uuid.uuid4(), "x")],
        "no_phone": [HandoverItem(nophone.id, "x")],
        "duplicate": [HandoverItem(c.id, "x"), HandoverItem(c.id, "y")],
        "too_many": [HandoverItem(c.id, "x")] * 11,
    }[case]
    with pytest.raises(HandoverInvalid):
        await validate_handover(async_session, org, items)


async def test_validate_rejects_suppressed(async_session) -> None:
    org, _, (c,) = await _agent_and_contacts(async_session, "+14155550103")
    await add_suppression(async_session, org, "+14155550103", channel="voice")
    with pytest.raises(HandoverInvalid) as err:
        await validate_handover(async_session, org, [HandoverItem(c.id, "x")])
    assert err.value.index == 0


async def test_validate_rejects_unsold_country(async_session) -> None:
    # +882 is an international network code: no carrier catalog lists it.
    org, _, (c,) = await _agent_and_contacts(async_session, "+88213000000")
    with pytest.raises(HandoverInvalid):
        await validate_handover(async_session, org, [HandoverItem(c.id, "x")])


async def test_replace_and_load_keep_order(async_session) -> None:
    _, agent, (a, b) = await _agent_and_contacts(
        async_session, "+14155550104", "+14155550105"
    )
    await replace_handover(
        async_session, agent.id, [HandoverItem(b.id, "second"), HandoverItem(a.id, "first")]
    )
    rows = (await load_handover(async_session, [agent.id]))[agent.id]
    assert [r["note"] for r in rows] == ["second", "first"]
    assert rows[0]["phone_e164"] == "+14155550105"
    await replace_handover(async_session, agent.id, [])
    assert (await load_handover(async_session, [agent.id])).get(agent.id, []) == []


async def test_targets_have_no_numbers(async_session) -> None:
    _, agent, (a,) = await _agent_and_contacts(async_session, "+14155550106")
    await replace_handover(async_session, agent.id, [HandoverItem(a.id, "Billing")])
    targets = await handover_targets(async_session, agent.id)
    assert targets == [{"contact_id": str(a.id), "label": "Person 0", "note": "Billing"}]
    assert "+1415" not in repr(targets)


async def test_targets_skip_contacts_without_phone(async_session) -> None:
    _, agent, (a,) = await _agent_and_contacts(async_session, "+14155550107")
    await replace_handover(async_session, agent.id, [HandoverItem(a.id, "x")])
    a.phone_e164 = None
    a.email = "a@example.com"
    await async_session.flush()
    assert await handover_targets(async_session, agent.id) == []


async def test_targets_dedupe_labels(async_session) -> None:
    _, agent, (a, b) = await _agent_and_contacts(
        async_session, "+14155550108", "+14155550109"
    )
    b.name = a.name
    await replace_handover(
        async_session, agent.id, [HandoverItem(a.id, "x"), HandoverItem(b.id, "y")]
    )
    labels = [t["label"] for t in await handover_targets(async_session, agent.id)]
    assert labels == ["Person 0", "Person 0 (2)"]


async def test_targets_none_agent(async_session) -> None:
    assert await handover_targets(async_session, None) == []
```

Check `add_suppression`'s real signature in `core/hailhq/core/compliance_gate.py` before running and adjust the call.

- [ ] **Step 2: Run, expect FAIL** — `cd core && uv run pytest tests/test_handover.py -v` → `ModuleNotFoundError: hailhq.core.handover`.

- [ ] **Step 3: Implement** `core/hailhq/core/handover.py`:

```python
"""Human handover: which contacts an agent may hand a live call to.

Spec: docs/superpowers/specs/2026-10-08-human-handover-design.md. Numbers
stay server-side: ``handover_targets`` (what reaches the LLM via dispatch
metadata) carries names and notes only.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import phonenumbers
from hailhq.core.compliance_gate import check_call_allowed
from hailhq.core.models import AgentHandoverContact, Contact
from hailhq.core.telephony_catalog import sells_in
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

MAX_HANDOVER_CONTACTS = 10


@dataclass(frozen=True)
class HandoverItem:
    contact_id: UUID
    note: str


class HandoverInvalid(ValueError):
    def __init__(self, message: str, index: int | None = None) -> None:
        super().__init__(message)
        self.index = index


def country_of(e164: str) -> str | None:
    try:
        region = phonenumbers.region_code_for_number(phonenumbers.parse(e164))
    except phonenumbers.NumberParseException:
        return None
    return region if region and region != "001" else None


async def validate_handover(
    db: AsyncSession, org_id: UUID, items: list[HandoverItem]
) -> None:
    if len(items) > MAX_HANDOVER_CONTACTS:
        raise HandoverInvalid(f"at most {MAX_HANDOVER_CONTACTS} handover contacts")
    ids = [i.contact_id for i in items]
    if len(set(ids)) != len(ids):
        raise HandoverInvalid("a contact is listed twice")
    rows = {
        c.id: c
        for c in (
            await db.execute(
                select(Contact).where(
                    Contact.organization_id == org_id, Contact.id.in_(ids)
                )
            )
        ).scalars()
    }
    for index, item in enumerate(items):
        contact = rows.get(item.contact_id)
        if contact is None:
            raise HandoverInvalid("contact not found", index)
        if not contact.phone_e164:
            raise HandoverInvalid(f"{contact.name} has no phone number", index)
        country = country_of(contact.phone_e164)
        if country is None or not sells_in(country):
            raise HandoverInvalid(
                f"{contact.name}'s number is in a country Hail does not call", index
            )
        gate = await check_call_allowed(db, org_id, contact.phone_e164)
        if not gate.allowed:
            raise HandoverInvalid(f"{contact.name}'s number cannot be called", index)


async def replace_handover(
    db: AsyncSession, agent_id: UUID, items: list[HandoverItem]
) -> None:
    await db.execute(
        delete(AgentHandoverContact).where(AgentHandoverContact.agent_id == agent_id)
    )
    for position, item in enumerate(items):
        db.add(
            AgentHandoverContact(
                agent_id=agent_id,
                contact_id=item.contact_id,
                note=item.note,
                position=position,
            )
        )
    await db.flush()


async def load_handover(
    db: AsyncSession, agent_ids: list[UUID]
) -> dict[UUID, list[dict]]:
    if not agent_ids:
        return {}
    result = await db.execute(
        select(AgentHandoverContact, Contact)
        .join(Contact, Contact.id == AgentHandoverContact.contact_id)
        .where(AgentHandoverContact.agent_id.in_(agent_ids))
        .order_by(AgentHandoverContact.agent_id, AgentHandoverContact.position)
    )
    out: dict[UUID, list[dict]] = {}
    for link, contact in result.all():
        out.setdefault(link.agent_id, []).append(
            {
                "contact_id": contact.id,
                "name": contact.name,
                "phone_e164": contact.phone_e164,
                "note": link.note,
            }
        )
    return out


async def handover_targets(db: AsyncSession, agent_id: UUID | None) -> list[dict]:
    """Dispatch-metadata shape. No numbers. Labels unique per call."""
    if agent_id is None:
        return []
    rows = (await load_handover(db, [agent_id])).get(agent_id, [])
    seen: dict[str, int] = {}
    targets = []
    for row in rows:
        if not row["phone_e164"]:
            continue
        name = row["name"]
        seen[name] = seen.get(name, 0) + 1
        label = name if seen[name] == 1 else f"{name} ({seen[name]})"
        targets.append(
            {"contact_id": str(row["contact_id"]), "label": label, "note": row["note"]}
        )
    return targets
```

- [ ] **Step 4: Run, expect PASS** — `cd core && uv run pytest tests/test_handover.py -v`.

- [ ] **Step 5: Commit** `git add core/hailhq/core/handover.py core/tests/test_handover.py && git commit -m "feat(core): handover contact helpers"`

---

### Task 3: Agent API field

**Files:**
- Modify: `core/hailhq/core/schemas.py` (`AgentCreate` ~686, `AgentUpdate` ~760, `AgentResponse` ~817)
- Modify: `api/hailhq/api/routes/agents.py`
- Test: `api/tests/test_agents_api.py`

**Interfaces:**
- Consumes: `validate_handover`, `replace_handover`, `load_handover`, `HandoverItem`, `HandoverInvalid`.
- Produces: JSON field `handover_contacts` on create/update (`[{contact_id, note}]`, update `null` = leave unchanged, `[]` = clear) and response (`[{contact_id, name, phone_e164, note}]`).

- [ ] **Step 1: Failing tests** in `api/tests/test_agents_api.py`:

```python
async def _contact(client, headers, name="Sam", phone="+14155550111") -> str:
    r = await client.post(
        "/contacts", json={"name": name, "phone_e164": phone}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_agent_handover_contacts_round_trip(client, org) -> None:
    _, headers = org
    cid = await _contact(client, headers)
    r = await client.post(
        "/agents",
        json={
            "name": "Desk",
            "system_prompt": "Help.",
            "handover_contacts": [{"contact_id": cid, "note": "Billing"}],
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["handover_contacts"] == [
        {"contact_id": cid, "name": "Sam", "phone_e164": "+14155550111", "note": "Billing"}
    ]
    agent_id = body["id"]
    r = await client.patch(
        f"/agents/{agent_id}", json={"name": "Desk 2"}, headers=headers
    )
    assert r.json()["handover_contacts"][0]["contact_id"] == cid  # untouched
    r = await client.patch(
        f"/agents/{agent_id}", json={"handover_contacts": []}, headers=headers
    )
    assert r.json()["handover_contacts"] == []
    listed = (await client.get("/agents", headers=headers)).json()["items"]
    assert listed[0]["handover_contacts"] == []


async def test_agent_handover_contact_from_other_org_is_422(client, org) -> None:
    _, headers = org
    r = await client.post(
        "/agents",
        json={
            "name": "Desk",
            "system_prompt": "Help.",
            "handover_contacts": [{"contact_id": str(uuid.uuid4()), "note": "x"}],
        },
        headers=headers,
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "handover_contacts", 0]


async def test_agent_handover_note_required(client, org) -> None:
    _, headers = org
    cid = await _contact(client, headers)
    r = await client.post(
        "/agents",
        json={
            "name": "Desk",
            "system_prompt": "Help.",
            "handover_contacts": [{"contact_id": cid, "note": ""}],
        },
        headers=headers,
    )
    assert r.status_code == 422
```

Check `unprocessable()`'s output shape in `api/hailhq/api/errors.py` and match the `loc` assertion to it.

- [ ] **Step 2: Run, expect FAIL** — `cd api && uv run pytest tests/test_agents_api.py -k handover -v`.

- [ ] **Step 3: Schemas** — add to `schemas.py` above `AgentCreate`:

```python
class HandoverContactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: UUID = Field(description="A contact of this organization with a phone number.")
    note: str = Field(
        min_length=1,
        max_length=200,
        description="When the agent should hand over to this person, e.g. 'billing questions'. Read by the agent.",
    )


class HandoverContactOut(BaseModel):
    contact_id: UUID = Field(description="The contact.")
    name: str = Field(description="Contact name, as the agent says it.")
    phone_e164: str | None = Field(description="Number Hail dials. Null when the contact lost its number; it is then skipped.")
    note: str = Field(description="When the agent hands over to this person.")
```

Add to `AgentCreate`:

```python
    handover_contacts: list[HandoverContactIn] = Field(
        default_factory=list,
        max_length=10,
        description="People the agent may hand a live call to, in order. The agent never sees their numbers.",
    )
```

Add to `AgentUpdate`:

```python
    handover_contacts: list[HandoverContactIn] | None = Field(
        default=None,
        max_length=10,
        description="New handover list; replaces the old one. [] removes all; null leaves it.",
    )
```

Add to `AgentResponse`:

```python
    handover_contacts: list[HandoverContactOut] = Field(
        default_factory=list, description="People the agent may hand a live call to."
    )
```

- [ ] **Step 4: Route** — in `agents.py`:

```python
from hailhq.core.handover import (
    HandoverInvalid,
    HandoverItem,
    load_handover,
    replace_handover,
    validate_handover,
)


async def _check_handover(db, org_id, items) -> list[HandoverItem]:
    parsed = [HandoverItem(i.contact_id, i.note.strip()) for i in items]
    try:
        await validate_handover(db, org_id, parsed)
    except HandoverInvalid as exc:
        loc = ["body", "handover_contacts"]
        if exc.index is not None:
            loc.append(exc.index)
        raise unprocessable(str(exc), loc=loc) from exc
    return parsed


async def _respond(db, agents: list[Agent]) -> list[AgentResponse]:
    links = await load_handover(db, [a.id for a in agents])
    return [
        AgentResponse.model_validate(a).model_copy(
            update={"handover_contacts": links.get(a.id, [])}
        )
        for a in agents
    ]
```

Then:
- `create_agent`: after `_check_tools`, `handover = await _check_handover(db, principal.organization_id, body.handover_contacts)`. Build `Agent(...)` as today; after `db.add(agent)` do `await db.flush()` then `await replace_handover(db, agent.id, handover)` inside the existing try (an `IntegrityError` on flush is still the name conflict). Return `(await _respond(db, [agent]))[0]`.
- `update_agent`: pop `handover_contacts` out of `changes` before the setattr loop: `handover = changes.pop("handover_contacts", None)`; if `handover is not None`: `items = await _check_handover(db, org, body.handover_contacts)`; `await replace_handover(db, agent.id, items)`; and make sure the commit branch runs (`if changes or handover is not None:`), with `"handover_contacts"` added to the audit `fields`.
- `list_agents` and `get_agent`: return via `_respond`.

`model_validate` on the ORM row will fail without the attribute because `handover_contacts` has a default — it does not; `from_attributes` reads only attributes present, defaults fill the rest. Confirm by running the tests.

- [ ] **Step 5: Run, expect PASS** — `cd api && uv run pytest tests/test_agents_api.py -v` (whole file: old tests must stay green).

- [ ] **Step 6: Commit** `git add -A && git commit -m "feat(api): handover_contacts on agents"`

---

### Task 4: Dispatch metadata

**Files:**
- Modify: `api/hailhq/api/routes/calls.py:556-571`
- Modify: `core/hailhq/core/inbound_calls.py:246-260`
- Test: `api/tests/test_calls_api.py`, `core/tests/test_inbound_calls.py` (the existing files that assert dispatch metadata; find with `grep -rn '"org_name"' api/tests core/tests`)

**Interfaces:**
- Consumes: `handover_targets(db, agent_id)`.
- Produces: metadata key `handover_targets: list[{contact_id: str, label: str, note: str}]` (always present; `[]` when none).

- [ ] **Step 1: Failing tests** — extend one existing outbound test that inspects `dispatch_agent`'s `metadata` and one inbound test that inspects `Accepted.metadata`:

```python
    assert meta["handover_targets"] == []
```

and a new outbound test that creates an agent with one handover contact (reuse `_contact` pattern from Task 3) and places a call with `agent_id`:

```python
    meta = lk_mock.dispatch_agent.call_args.kwargs["metadata"]
    assert meta["handover_targets"] == [
        {"contact_id": cid, "label": "Sam", "note": "Billing"}
    ]
    assert "+14155550111" not in json.dumps(meta)
```

- [ ] **Step 2: Run, expect FAIL** (`KeyError: 'handover_targets'`).

- [ ] **Step 3: Implement** — `calls.py` metadata dict, after `"tools": body.tools,`:

```python
                "handover_targets": await handover_targets(
                    db, agent.id if agent else None
                ),
```

`inbound_calls.py` metadata dict, after `"tools": agent.tools,`:

```python
            "handover_targets": await handover_targets(db, agent.id),
```

Import `from hailhq.core.handover import handover_targets` in both. In `calls.py` the session variable may not be named `db`; use the handler's session name.

- [ ] **Step 4: Run, expect PASS** — the two test files.

- [ ] **Step 5: Commit** `git commit -am "feat: handover targets in dispatch metadata"`

---

### Task 5: Internal handover endpoints + webhook type

**Files:**
- Modify: `api/hailhq/api/routes/internal/agent.py`
- Modify: `core/hailhq/core/schemas.py` (`WebhookEventType` literal ~1891)
- Test: `api/tests/test_internal_agent_handover.py` (new)

**Interfaces:**
- Consumes: `AgentHandoverContact`, `Contact`, `check_call_allowed`, `country_of`, `sells_in`, `voice_route`, `fanout_call_event`, `call_event_data`.
- Produces:
  - `POST /internal/agent/handover` body `{call_id, contact_id}` → `{ok, spoken, to_e164?, from_e164?, trunk_id?, headers?}`
  - `POST /internal/agent/handover-result` body `{call_id, contact_id, outcome: "answered"|"no_answer"|"busy"|"failed", sip_status: int|null, ring_ms: int}` → `{ok}`
  - Webhook event `call.transferred`, data = `call_event_data(call, transfer={contact_id, contact_name})`.

- [ ] **Step 1: Failing tests** `api/tests/test_internal_agent_handover.py`:

```python
"""Internal handover routes: route lookup, gating, result recording."""

from __future__ import annotations

import json
import uuid

import pytest
from hailhq.core import hmac_signing
from hailhq.core.compliance_gate import add_suppression
from hailhq.core.config import settings
from hailhq.core.models import (
    Agent,
    AgentHandoverContact,
    Call,
    CallEvent,
    Contact,
    WebhookDelivery,
    WebhookSubscription,
)
from sqlalchemy import select

SECRET = "test-internal-secret"


def _signed(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Hail-Signature": hmac_signing.sign(body, SECRET),
    }


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", SECRET)
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_out_tw")


async def _seed(s, add_phone_number, *, phone="+14155550120", link=True):
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="Desk", system_prompt="Help.")
    contact = Contact(organization_id=org, name="Sam", phone_e164=phone)
    s.add_all([agent, contact])
    await s.flush()
    if link:
        s.add(
            AgentHandoverContact(
                agent_id=agent.id, contact_id=contact.id, note="Billing", position=0
            )
        )
    number = await add_phone_number(s, org)
    call = Call(
        organization_id=org,
        from_number_id=number.id,
        from_e164=number.e164,
        to_e164="+14155550199",
        status="in_progress",
        agent_id=agent.id,
        provider="twilio",
        voice_config={},
    )
    s.add(call)
    await s.commit()
    return call, contact


async def _post(client, path, payload):
    body = json.dumps(payload).encode()
    return await client.post(path, content=body, headers=_signed(body))


async def test_handover_returns_route(client, async_session, add_phone_number):
    call, contact = await _seed(async_session, add_phone_number)
    r = await _post(
        client,
        "/internal/agent/handover",
        {"call_id": str(call.id), "contact_id": str(contact.id)},
    )
    data = r.json()
    assert data["ok"] is True
    assert data["to_e164"] == "+14155550120"
    assert data["from_e164"] == call.from_e164
    assert data["trunk_id"] == "ST_out_tw"


@pytest.mark.parametrize("case", ["unlinked", "ended", "suppressed", "unsold", "done"])
async def test_handover_denied(client, async_session, add_phone_number, case):
    call, contact = await _seed(
        async_session,
        add_phone_number,
        link=case != "unlinked",
        phone="+88213000000" if case == "unsold" else "+14155550120",
    )
    if case == "ended":
        call.status = "completed"
        call.end_reason = "normal_hangup"
    if case == "suppressed":
        await add_suppression(
            async_session, call.organization_id, "+14155550120", channel="voice"
        )
    if case == "done":
        async_session.add(
            CallEvent(call_id=call.id, kind="handover", payload={"outcome": "answered"})
        )
    await async_session.commit()
    r = await _post(
        client,
        "/internal/agent/handover",
        {"call_id": str(call.id), "contact_id": str(contact.id)},
    )
    data = r.json()
    assert data["ok"] is False
    assert data["spoken"]
    assert "to_e164" not in data or data["to_e164"] is None


async def test_result_answered_writes_event_and_webhook(
    client, async_session, add_phone_number
):
    call, contact = await _seed(async_session, add_phone_number)
    async_session.add(
        WebhookSubscription(
            organization_id=call.organization_id,
            url="https://example.com/hook",
            event_types=["call.transferred"],
            secret_enc="x",
        )
    )
    await async_session.commit()
    r = await _post(
        client,
        "/internal/agent/handover-result",
        {
            "call_id": str(call.id),
            "contact_id": str(contact.id),
            "outcome": "answered",
            "sip_status": None,
            "ring_ms": 12000,
        },
    )
    assert r.json() == {"ok": True}
    ev = (
        await async_session.execute(
            select(CallEvent).where(CallEvent.call_id == call.id, CallEvent.kind == "handover")
        )
    ).scalar_one()
    assert ev.payload == {
        "contact_id": str(contact.id),
        "name": "Sam",
        "outcome": "answered",
        "sip_status": None,
        "ring_ms": 12000,
    }
    deliveries = (
        await async_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.event_type == "call.transferred")
        )
    ).scalars().all()
    assert len(deliveries) == 1


async def test_result_no_answer_writes_event_only(client, async_session, add_phone_number):
    call, contact = await _seed(async_session, add_phone_number)
    await _post(
        client,
        "/internal/agent/handover-result",
        {
            "call_id": str(call.id),
            "contact_id": str(contact.id),
            "outcome": "no_answer",
            "sip_status": 480,
            "ring_ms": 30000,
        },
    )
    deliveries = (
        await async_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.event_type == "call.transferred")
        )
    ).scalars().all()
    assert deliveries == []
```

Match `WebhookSubscription` required columns to the model before running (`grep -n "class WebhookSubscription" -A40 core/hailhq/core/models.py`), and `add_suppression`'s signature.

- [ ] **Step 2: Run, expect FAIL** (404 on both routes).

- [ ] **Step 3: Webhook type** — in `schemas.py` event literal, after `"call.no_answer",` add `"call.transferred",`.

- [ ] **Step 4: Routes** — append to `internal/agent.py`:

```python
_SPOKEN_HANDOVER_UNAVAILABLE = "I can't connect you to that person right now."
_SPOKEN_HANDOVER_DONE = "You are already connected."


class AgentHandoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: UUID
    contact_id: UUID


class AgentHandoverResponse(BaseModel):
    ok: bool
    spoken: str
    to_e164: str | None = None
    from_e164: str | None = None
    trunk_id: str | None = None
    headers: dict[str, str] | None = None


class AgentHandoverResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: UUID
    contact_id: UUID
    outcome: Literal["answered", "no_answer", "busy", "failed"]
    sip_status: int | None = None
    ring_ms: int = Field(ge=0)


async def _answered_handover(db: AsyncSession, call_id: UUID) -> bool:
    return (
        await db.execute(
            select(CallEvent.id)
            .where(
                CallEvent.call_id == call_id,
                CallEvent.kind == "handover",
                CallEvent.payload["outcome"].astext == "answered",
            )
            .limit(1)
        )
    ).first() is not None


async def _linked_contact(db: AsyncSession, call: Call, contact_id: UUID) -> Contact | None:
    if call.agent_id is None:
        return None
    return (
        await db.execute(
            select(Contact)
            .join(AgentHandoverContact, AgentHandoverContact.contact_id == Contact.id)
            .where(
                AgentHandoverContact.agent_id == call.agent_id,
                Contact.id == contact_id,
                Contact.organization_id == call.organization_id,
            )
        )
    ).scalar_one_or_none()


@router.post("/handover", response_model=AgentHandoverResponse)
async def agent_handover(
    body: AgentHandoverRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentHandoverResponse:
    deny = AgentHandoverResponse(ok=False, spoken=_SPOKEN_HANDOVER_UNAVAILABLE)
    call = await db.get(Call, body.call_id)
    if call is None or call.status != "in_progress":
        return deny
    if await _answered_handover(db, call.id):
        return AgentHandoverResponse(ok=False, spoken=_SPOKEN_HANDOVER_DONE)
    contact = await _linked_contact(db, call, body.contact_id)
    if contact is None or not contact.phone_e164:
        return deny
    country = country_of(contact.phone_e164)
    if country is None or not sells_in(country, call.provider):
        return await _deny_handover(call, body, "country_not_sold")
    gate = await check_call_allowed(db, call.organization_id, contact.phone_e164)
    if not gate.allowed:
        return await _deny_handover(call, body, gate.reason or "gate")
    try:
        trunk_id, headers = voice_route(call.provider)
    except Exception:
        return await _deny_handover(call, body, "carrier_route_failed")
    # The Hail number on this call: outbound dials from it, inbound rang it.
    hail_number = call.to_e164 if call.direction == "inbound" else call.from_e164
    return AgentHandoverResponse(
        ok=True,
        spoken="",
        to_e164=contact.phone_e164,
        from_e164=hail_number,
        trunk_id=trunk_id,
        headers=headers or None,
    )


async def _deny_handover(
    call: Call, body: AgentHandoverRequest, reason: str
) -> AgentHandoverResponse:
    await write_audit_log(
        organization_id=call.organization_id,
        api_key_id=None,
        action="agent.handover.blocked",
        resource_type="call",
        resource_id=call.id,
        payload={"contact_id": str(body.contact_id), "reason": reason},
        actor_kind="system",
    )
    return AgentHandoverResponse(ok=False, spoken=_SPOKEN_HANDOVER_UNAVAILABLE)


@router.post("/handover-result")
async def agent_handover_result(
    body: AgentHandoverResultRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, bool]:
    call = await db.get(Call, body.call_id)
    if call is None:
        return {"ok": False}
    contact = await db.get(Contact, body.contact_id)
    name = contact.name if contact and contact.organization_id == call.organization_id else ""
    db.add(
        CallEvent(
            call_id=call.id,
            kind="handover",
            payload={
                "contact_id": str(body.contact_id),
                "name": name,
                "outcome": body.outcome,
                "sip_status": body.sip_status,
                "ring_ms": body.ring_ms,
            },
        )
    )
    if body.outcome == "answered":
        await fanout_call_event(
            db,
            organization_id=call.organization_id,
            event_type="call.transferred",
            event_id=call.id,
            data=call_event_data(
                call, transfer={"contact_id": str(body.contact_id), "contact_name": name}
            ),
        )
    await db.commit()
    return {"ok": True}
```

Imports to add: `from typing import Literal`, `AgentHandoverContact, CallEvent, Contact` from models, `check_call_allowed` from compliance_gate, `from hailhq.core.carrier_routing import voice_route`, `from hailhq.core.handover import country_of`, `from hailhq.core.telephony_catalog import sells_in`, `from hailhq.core.webhook_fanout import call_event_data, fanout_call_event`. Check `call_event_data`'s `**extra` merges as shown (`core/hailhq/core/webhook_fanout.py:131`). `event_id=call.id` matches the other call events; only one answered handover exists per call.

- [ ] **Step 5: Run, expect PASS** — `cd api && uv run pytest tests/test_internal_agent_handover.py -v`.

- [ ] **Step 6: Commit** `git add -A && git commit -m "feat(api): internal handover routes and call.transferred webhook"`

---

### Task 6: Core tool `transfer_call`

**Files:**
- Modify: `core/hailhq/core/agent_tools/spec.py`
- Create: `core/hailhq/core/agent_tools/transfer_call.py`
- Modify: `core/hailhq/core/agent_tools/registry.py`
- Test: `core/tests/test_transfer_call.py` (new), `core/tests/test_agent_tools.py:29-44` (name set)

**Interfaces:**
- Produces in `spec.py`:

```python
@dataclass(frozen=True)
class BridgeRoute:
    to_e164: str
    from_e164: str
    trunk_id: str
    headers: dict[str, str] | None
    name: str
    reason: str


@dataclass(frozen=True)
class BridgeOutcome:
    outcome: Literal["answered", "no_answer", "busy", "failed"]
    sip_status: int | None
    ring_ms: int
```

  `ToolContext.bridge: Callable[[BridgeRoute], Awaitable[BridgeOutcome]] | None = None` (default so existing constructors keep working).
  `ToolSpec.bind: Callable[[dict[str, Any]], "ToolSpec | None"] | None = None` — per-call shaping from dispatch metadata; `None` result hides the tool.
- `transfer_call.SPEC` (static, for allowlists) and `transfer_call.bind(metadata) -> ToolSpec | None`.

- [ ] **Step 1: Failing tests** `core/tests/test_transfer_call.py`:

```python
import uuid
from unittest.mock import AsyncMock

from hailhq.core.agent_tools import transfer_call
from hailhq.core.agent_tools.spec import BridgeOutcome, ToolContext

CID = str(uuid.uuid4())
META = {"handover_targets": [{"contact_id": CID, "label": "Sam", "note": "Billing"}]}


def _ctx(api_reply, outcome=None):
    api = AsyncMock()
    api.post.side_effect = [api_reply, {"ok": True}]
    bridge = AsyncMock(return_value=outcome)
    return (
        ToolContext(
            call_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            api=api,
            hangup=None,
            send_dtmf=None,
            bridge=bridge,
        ),
        api,
        bridge,
    )


ROUTE = {
    "ok": True,
    "spoken": "",
    "to_e164": "+14155550120",
    "from_e164": "+14155550100",
    "trunk_id": "ST_x",
    "headers": None,
}


def test_bind_hides_without_targets() -> None:
    assert transfer_call.bind({"handover_targets": []}) is None
    assert transfer_call.bind({}) is None


def test_bind_lists_names_without_numbers() -> None:
    spec = transfer_call.bind(META)
    assert spec is not None
    assert "Sam" in spec.description and "Billing" in spec.description
    assert spec.parameters["properties"]["contact"]["enum"] == ["Sam"]
    assert spec.risk_tier == "session_control"


async def test_answered() -> None:
    spec = transfer_call.bind(META)
    ctx, api, bridge = _ctx(ROUTE, BridgeOutcome("answered", None, 9000))
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "an invoice"})
    assert "connected" in said.lower()
    route = bridge.await_args.args[0]
    assert route.to_e164 == "+14155550120" and route.reason == "an invoice"
    assert api.post.await_args_list[0].args == (
        "/internal/agent/handover",
        {"call_id": str(ctx.call_id), "contact_id": CID},
    )
    result = api.post.await_args_list[1].args[1]
    assert result["outcome"] == "answered" and result["ring_ms"] == 9000
    assert "+1415" not in said


async def test_no_answer_comes_back() -> None:
    spec = transfer_call.bind(META)
    ctx, _, _ = _ctx(ROUTE, BridgeOutcome("no_answer", 480, 30000))
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert "could not pick up" in said


async def test_denied_by_api() -> None:
    spec = transfer_call.bind(META)
    ctx, api, bridge = _ctx({"ok": False, "spoken": "I can't connect you to that person right now."})
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert said == "I can't connect you to that person right now."
    bridge.assert_not_awaited()
    assert api.post.await_count == 1


async def test_unknown_name() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    said = await spec.execute(ctx, {"contact": "Bob", "reason": "x"})
    assert "Sam" in said
    api.post.assert_not_awaited()


async def test_no_bridge_handle() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    ctx.bridge = None
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert said
    api.post.assert_not_awaited()
```

In `core/tests/test_agent_tools.py` add `"transfer_call"` to the expected name set and assert its tier is `session_control`.

- [ ] **Step 2: Run, expect FAIL** — `cd core && uv run pytest tests/test_transfer_call.py tests/test_agent_tools.py -v`.

- [ ] **Step 3: `spec.py`** — add `BridgeRoute`, `BridgeOutcome` (above), then:

```python
    send_dtmf: Callable[[str], Awaitable[None]] | None
    bridge: Callable[[BridgeRoute], Awaitable[BridgeOutcome]] | None = None
```

and on `ToolSpec`:

```python
    execute: Callable[[ToolContext, dict[str, Any]], Awaitable[str]]
    # Per-call shaping from dispatch metadata (names in the description,
    # enum of choices). None result hides the tool for this call.
    bind: Callable[[dict[str, Any]], "ToolSpec | None"] | None = None
```

Export the two new names in `__all__`.

- [ ] **Step 4: `transfer_call.py`**:

```python
"""transfer_call — hand the live call to a person the admin picked.

session_control tier: the agent finishes its sentence first. The LLM only
sees names and notes (dispatch metadata ``handover_targets``); the API
resolves and re-checks the number (``/internal/agent/handover``), the
voicebot's ``bridge`` handle dials it into the room, and the API records
the outcome (``/internal/agent/handover-result``).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from hailhq.core.agent_tools.spec import (
    SPOKEN_FALLBACK,
    BridgeRoute,
    ToolContext,
    ToolSpec,
)
from sqlalchemy.ext.asyncio import AsyncSession

MAX_REASON_CHARS = 200

_UNAVAILABLE = "I can't connect you to anyone right now."
_CONNECTED = "Connected."
_NO_ANSWER = "They could not pick up. Offer to take a message."


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    # The static spec is only for allowlists; bind() decides per call.
    return True


async def _unbound(_ctx: ToolContext, _args: dict[str, Any]) -> str:
    return _UNAVAILABLE


SPEC = ToolSpec(
    name="transfer_call",
    description="Connect the caller to a person on the team.",
    parameters={"type": "object", "properties": {}, "required": []},
    risk_tier="session_control",
    is_available=_always,
    execute=_unbound,
)


def bind(metadata: dict[str, Any]) -> ToolSpec | None:
    targets = metadata.get("handover_targets") or []
    if not isinstance(targets, list) or not targets:
        return None
    by_label = {t["label"]: t["contact_id"] for t in targets}
    menu = "; ".join(f"{t['label']} ({t['note']})" for t in targets)

    async def execute(ctx: ToolContext, args: dict[str, Any]) -> str:
        if ctx.api is None or ctx.bridge is None:
            return _UNAVAILABLE
        label = str(args.get("contact", ""))
        contact_id = by_label.get(label)
        if contact_id is None:
            return "I can only connect you to: " + ", ".join(by_label) + "."
        reason = str(args.get("reason", "")).strip()[:MAX_REASON_CHARS]
        route = await ctx.api.post(
            "/internal/agent/handover",
            {"call_id": str(ctx.call_id), "contact_id": contact_id},
        )
        if not route.get("ok"):
            return str(route.get("spoken") or SPOKEN_FALLBACK)
        outcome = await ctx.bridge(
            BridgeRoute(
                to_e164=route["to_e164"],
                from_e164=route["from_e164"],
                trunk_id=route["trunk_id"],
                headers=route.get("headers"),
                name=label,
                reason=reason,
            )
        )
        await ctx.api.post(
            "/internal/agent/handover-result",
            {
                "call_id": str(ctx.call_id),
                "contact_id": contact_id,
                "outcome": outcome.outcome,
                "sip_status": outcome.sip_status,
                "ring_ms": outcome.ring_ms,
            },
        )
        return _CONNECTED if outcome.outcome == "answered" else _NO_ANSWER

    return dataclasses.replace(
        SPEC,
        description=(
            "Connect the caller to a person on the team when they ask for a "
            "person or when the note says so. First tell the caller you are "
            f"connecting them. People: {menu}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "contact": {"type": "string", "enum": list(by_label)},
                "reason": {
                    "type": "string",
                    "description": "One short phrase for the person, e.g. 'an invoice question'.",
                },
            },
            "required": ["contact", "reason"],
        },
        execute=execute,
        bind=None,
    )


SPEC = dataclasses.replace(SPEC, bind=bind)

__all__ = ["MAX_REASON_CHARS", "SPEC", "bind"]
```

- [ ] **Step 5: Register** — `registry.py`: import `transfer_call`, add `transfer_call.SPEC,` after `send_dtmf.SPEC,`.

- [ ] **Step 6: Run, expect PASS** — `cd core && uv run pytest tests/test_transfer_call.py tests/test_agent_tools.py -v`.

- [ ] **Step 7: Commit** `git add -A && git commit -m "feat(core): transfer_call agent tool"`

---

### Task 7: Voicebot bridge handle

**Files:**
- Modify: `voicebot/hailhq/voicebot/tools.py`
- Modify: `voicebot/hailhq/voicebot/agent.py` (`make_agent_bridge` next to `make_agent_send_dtmf` ~279; `build_tools_safely` ~303; `_on_participant_disconnected` ~1391; wiring ~1574)
- Modify: `voicebot/tests/_fakes.py`
- Test: `voicebot/tests/test_agent.py`, `voicebot/tests/test_tools.py`

**Interfaces:**
- Consumes: `BridgeRoute`, `BridgeOutcome`, `ToolSpec.bind`.
- Produces: `make_agent_bridge(ctx, session, call_id, bridge_state) -> Callable[[BridgeRoute], Awaitable[BridgeOutcome]]`; `bridge_state: dict` with keys `"human"` (identity or None) and `"connected"` (bool); `build_agent_tools(metadata, *, call_id, hangup, send_dtmf, bridge=None)`.

Behavior of the handle:
1. `bridge_state["human"] = f"human-{call_id}"`.
2. `await ctx.api.sip.create_sip_participant(CreateSIPParticipantRequest(sip_trunk_id=route.trunk_id, sip_call_to=route.to_e164, sip_number=route.from_e164, room_name=ctx.room.name, participant_identity=f"human-{call_id}", participant_name=route.name, headers=route.headers or {}, wait_until_answered=True, ringing_timeout=Duration(seconds=30), play_dialtone=True))`. Measure `ring_ms` with `time.monotonic()`.
3. `SipCallError` → map `sip_status_code`: 486/600/603 → `busy`, 408/480/487 → `no_answer`, else `failed`. `asyncio.TimeoutError` → `no_answer`. Any other exception → `failed` (log it). Set `bridge_state["human"] = None`, return outcome.
4. Answered: `bridge_state["connected"] = True`; `await session.say(f"Hi {route.name}, I have a caller about {route.reason or 'a question'}. Connecting you now.", allow_interruptions=False)` then `await handle.wait_for_playout()`; `session.input.set_audio_enabled(False)`; `session.output.set_audio_enabled(False)`; return `BridgeOutcome("answered", None, ring_ms)`.

Disconnect handling in `_on_participant_disconnected`:
- If `participant.identity == bridge_state["human"]`: do NOT run `disconnect_reason_to_status`. If `bridge_state["connected"]`: the person hung up → stamp `captured["end_reason"] = NORMAL_HANGUP` if unset, delete the room, shutdown. Else (failed during ringing): return (the handle reports it).
- Else (the caller) and `bridge_state["human"]` is set: run today's mapping, then `asyncio.ensure_future(ctx.delete_room())` so the contact's leg (ringing or connected) is dropped, then `ctx.shutdown(reason="caller_left")`.

- [ ] **Step 1: Fakes** — in `_fakes.py`, add a `FakeSip` with `create_sip_participant` as an `AsyncMock`, a `FakeLKApi` with `.sip = FakeSip()`, and `FakeJobContext.api = FakeLKApi()`. Add a `FakeBridgeSession` with `say()` returning `FakeSpeechHandle`, and `input`/`output` objects recording `set_audio_enabled` calls.

- [ ] **Step 2: Failing tests** in `voicebot/tests/test_agent.py`:

```python
async def test_bridge_answered_mutes_agent() -> None:
    ctx = FakeJobContext()
    session = FakeBridgeSession()
    state = {"human": None, "connected": False}
    bridge = make_agent_bridge(ctx, session, CALL_ID, state)  # type: ignore[arg-type]
    out = await bridge(BridgeRoute("+14155550120", "+14155550100", "ST_x", None, "Sam", "an invoice"))
    assert out.outcome == "answered"
    req = ctx.api.sip.create_sip_participant.await_args.args[0]
    assert req.sip_number == "+14155550100"
    assert req.participant_identity == f"human-{CALL_ID}"
    assert req.wait_until_answered is True
    assert session.said == ["Hi Sam, I have a caller about an invoice. Connecting you now."]
    assert session.input.enabled == [False] and session.output.enabled == [False]
    assert state["connected"] is True


@pytest.mark.parametrize("status,expected", [(486, "busy"), (480, "no_answer"), (500, "failed")])
async def test_bridge_failure_keeps_agent(status, expected) -> None:
    ctx = FakeJobContext()
    ctx.api.sip.create_sip_participant.side_effect = SipCallError(
        "unavailable", "fail", status=200, metadata={"sip_status_code": str(status)}
    )
    session = FakeBridgeSession()
    state = {"human": None, "connected": False}
    out = await make_agent_bridge(ctx, session, CALL_ID, state)(  # type: ignore[arg-type]
        BridgeRoute("+1", "+1", "ST", None, "Sam", "")
    )
    assert out.outcome == expected and out.sip_status == status
    assert session.input.enabled == [] and state["human"] is None
```

Plus two handler tests. The disconnect handler is a closure inside `entrypoint`; extract its body into a module-level `handle_sip_disconnect(ctx, participant, captured, bridge_state, call_id)` first (keeps today's behavior; existing tests that exercise disconnects must stay green), then test:

```python
def test_human_leg_disconnect_does_not_restamp_call() -> None:
    ctx = FakeJobContext()
    captured = {"status": None, "end_reason": None}
    state = {"human": f"human-{CALL_ID}", "connected": False}
    p = FakeParticipant(identity=f"human-{CALL_ID}", reason=rtc.DisconnectReason.USER_REJECTED)
    handle_sip_disconnect(ctx, p, captured, state, CALL_ID)
    assert captured == {"status": None, "end_reason": None}
    assert ctx.shutdown_calls == []


async def test_caller_leaves_while_ringing_deletes_room() -> None:
    ctx = FakeJobContext()
    captured = {"status": None, "end_reason": None}
    state = {"human": f"human-{CALL_ID}", "connected": False}
    p = FakeParticipant(identity="caller-x", reason=rtc.DisconnectReason.CLIENT_INITIATED)
    handle_sip_disconnect(ctx, p, captured, state, CALL_ID)
    await asyncio.sleep(0)
    assert ctx.delete_room_calls == 1
    assert ctx.shutdown_calls == ["caller_left"]


async def test_person_hangs_up_after_connect_ends_call() -> None:
    ctx = FakeJobContext()
    captured = {"status": None, "end_reason": None}
    state = {"human": f"human-{CALL_ID}", "connected": True}
    p = FakeParticipant(identity=f"human-{CALL_ID}", reason=rtc.DisconnectReason.CLIENT_INITIATED)
    handle_sip_disconnect(ctx, p, captured, state, CALL_ID)
    await asyncio.sleep(0)
    assert captured["end_reason"] == CallEndReason.NORMAL_HANGUP.value
    assert ctx.delete_room_calls == 1
```

Add `FakeParticipant(identity, reason)` to `_fakes.py` with `kind = rtc.ParticipantKind.PARTICIPANT_KIND_SIP` and `disconnect_reason = reason`.

In `voicebot/tests/test_tools.py`:

```python
async def test_bind_shapes_transfer_call(monkeypatch) -> None:
    meta = {
        "organization_id": str(uuid.uuid4()),
        "tools": None,
        "handover_targets": [{"contact_id": str(uuid.uuid4()), "label": "Sam", "note": "Billing"}],
    }
    tools, _ = await build_agent_tools(meta, call_id=uuid.uuid4(), hangup=None, send_dtmf=None, bridge=AsyncMock())
    names = [t.info.name for t in tools]
    assert "transfer_call" in names


async def test_transfer_call_hidden_without_targets() -> None:
    meta = {"organization_id": str(uuid.uuid4()), "tools": None, "handover_targets": []}
    tools, _ = await build_agent_tools(meta, call_id=uuid.uuid4(), hangup=None, send_dtmf=None, bridge=AsyncMock())
    assert "transfer_call" not in [t.info.name for t in tools]
```

Follow the existing setup in `test_tools.py` for the DB session / settings fixtures.

- [ ] **Step 3: Run, expect FAIL** — `cd voicebot && uv run pytest tests/test_agent.py tests/test_tools.py -v -k "bridge or disconnect or transfer or person_hangs"`.

- [ ] **Step 4: `tools.py`** — signature gains `bridge=None`; pass `bridge=bridge` into `ToolContext`; replace the specs line with:

```python
    specs: list[ToolSpec] = []
    for s in all_tools():
        if allowed is not None and s.name not in allowed:
            continue
        if s.bind is not None:
            bound = s.bind(metadata)
            if bound is None:
                continue
            s = bound
        specs.append(s)
```

- [ ] **Step 5: `agent.py`** — add `make_agent_bridge` and `handle_sip_disconnect` as described above. Imports: `from livekit import api as lkapi` (check what the module already imports for `api`), `from livekit.api import SipCallError`, `from livekit.protocol.models import ...` only if `Duration` is not reachable via `lkapi`; check `grep -rn "ringing_timeout" .venv/lib/python3.12/site-packages/livekit/api/sip_service.py` for how the SDK sets it (`_pin_ringing_timeout`) and set it the same way. `build_tools_safely` gains `bridge` and passes it through. In `entrypoint`: `bridge_state = {"human": None, "connected": False}` next to `captured`; the room handler becomes `handle_sip_disconnect(ctx, participant, captured, bridge_state, call_id)`; wiring at ~1574 passes `make_agent_bridge(ctx, session, call_id, bridge_state)`.

- [ ] **Step 6: Run, expect PASS** — `cd voicebot && uv run pytest -v`. `tests/test_amd.py` has 6 failures that pre-exist on main; the count must not grow.

- [ ] **Step 7: Commit** `git add -A && git commit -m "feat(voicebot): bridge a handover contact into the call"`

---

### Task 8: OpenAPI, CLI, MCP, docs

**Files:**
- Modify: `openapi/openapi.yaml` (regenerated), `cli/` (codegen), `cli/internal/cmd/agents.go`, `mcp/hailhq/mcp/tools.py` (~1240-1300), `docs/public/agents.md`, the public webhooks doc (`grep -rln "call.no_answer" docs/public`), `CHANGELOG.md` (`[Unreleased]`)

- [ ] **Step 1: Regenerate**

```bash
cd api && uv run python -c "from hailhq.api.main import app; import sys, yaml; yaml.safe_dump(app.openapi(), sys.stdout, sort_keys=False)" > ../openapi/openapi.yaml
cd ../cli && make codegen && go build ./... && go test ./...
```

- [ ] **Step 2: CLI** — `hail agents create|update` gain `--handover <contact_id>=<note>` (repeatable). Map to `b["handover_contacts"] = [{"contact_id": id, "note": note}]`. `--no-handover` sends `[]` on update. Add a test next to the existing agents command tests.

- [ ] **Step 3: MCP** — `create_agent` / `update_agent` tools gain `handover_contacts: list[dict] | None = None`, passed through when not None. Docstring: "People the agent may hand a live call to: [{contact_id, note}]". Run `cd mcp && uv run pytest -v`.

- [ ] **Step 4: Docs** — `docs/public/agents.md`: a "Hand over to a person" section with a runnable `curl -X PATCH .../agents/{id}` example setting `handover_contacts`, the rules (contacts with phone, max 10, numbers never reach the model, caller ID is the Hail number, 30 s ring then the agent takes a message, billed as one call). Webhooks doc: add `call.transferred` with the payload shape. CHANGELOG `[Unreleased]`: one line.

- [ ] **Step 5: Full check** — `uv run ruff check . && uv run black --check . && (cd core && uv run pytest -q) && (cd api && uv run pytest -q) && (cd mcp && uv run pytest -q)`; the OpenAPI CI gate: `git diff --exit-code openapi/openapi.yaml` after regenerating again.

- [ ] **Step 6: Commit** `git add -A && git commit -m "docs: human handover; regen openapi, cli, mcp"`, push `feat/human-handover`, open PR.

---

### Task 9: Console data and form state (hail-website)

Work in a new worktree: `git -C ~/playground/hail-website worktree add -b feat/human-handover .claude/worktrees/human-handover origin/master`. `node_modules`: symlink `../../../node_modules` like earlier worktrees.

**Files:**
- Modify: `lib/agent-queries.ts`, `app/console/agents/actions.ts`, `app/console/agents/agent-form.ts`, `app/console/agents/AgentSheet.tsx` (`fromAgent` only), `app/console/agents/new/page.tsx`, `app/console/agents/[id]/page.tsx`
- Test: `app/console/agents/__tests__/agent-form.test.ts`

**Interfaces:**
- Produces:
  - `AgentRow.handoverContacts: HandoverContactRow[]` where `type HandoverContactRow = { contactId: string; name: string; phone: string | null; note: string }`
  - `type PhoneContact = { id: string; name: string; phone: string }`; `phoneContacts(orgId: string | null): Promise<PhoneContact[]>` (`SELECT id, name, phone_e164 FROM contacts WHERE organization_id = $1 AND phone_e164 IS NOT NULL ORDER BY lower(name)`)
  - `AgentInput.handover_contacts: { contact_id: string; note: string }[]`
  - `AgentFormState.handover: { contactId: string; note: string }[]`
  - `validateForm` errors key `handover` → `{ index: number; message: string } | undefined`

- [ ] **Step 1: Failing tests** in `agent-form.test.ts`:

```ts
describe("handover", () => {
  const ws = { line: DEFAULT_INBOUND_LINE, maxMinutes: 10 };

  it("formToInput sends the list in order, trimmed", () => {
    const f = { ...emptyForm(ws), name: "A", systemPrompt: "B",
      handover: [{ contactId: "c2", note: " Billing " }, { contactId: "c1", note: "Sales" }] };
    expect(formToInput(f, ws).handover_contacts).toEqual([
      { contact_id: "c2", note: "Billing" }, { contact_id: "c1", note: "Sales" },
    ]);
  });

  it("formToInput adds transfer_call to an explicit tools list when contacts are set", () => {
    const f = { ...emptyForm(ws), name: "A", systemPrompt: "B", tools: ["end_call"],
      handover: [{ contactId: "c1", note: "x" }] };
    expect(formToInput(f, ws).tools).toEqual(["end_call", "transfer_call"]);
  });

  it("formToInput removes transfer_call when the list is empty", () => {
    const f = { ...emptyForm(ws), name: "A", systemPrompt: "B", tools: ["end_call", "transfer_call"], handover: [] };
    expect(formToInput(f, ws).tools).toEqual(["end_call"]);
  });

  it("formToInput keeps null tools (all) as null", () => {
    const f = { ...emptyForm(ws), name: "A", systemPrompt: "B", handover: [{ contactId: "c1", note: "x" }] };
    expect(formToInput(f, ws).tools).toBeNull();
  });

  it("validateForm needs a note on every row", () => {
    const f = { ...emptyForm(ws), name: "A", systemPrompt: "B", handover: [{ contactId: "c1", note: "  " }] };
    expect(validateForm(f).handover).toEqual({ index: 0, message: "Say when to hand over to this person." });
  });

  it("validateForm caps at 10", () => {
    const rows = Array.from({ length: 11 }, (_, i) => ({ contactId: `c${i}`, note: "x" }));
    expect(validateForm({ ...emptyForm(ws), name: "A", systemPrompt: "B", handover: rows }).handover?.message)
      .toBe("Up to 10 people.");
  });
});
```

Read the current `validateForm` return shape in `agent-form.ts` and match it (add the `handover` key to its error type).

- [ ] **Step 2: Run, expect FAIL** — `pnpm vitest run app/console/agents/__tests__/agent-form.test.ts`.

- [ ] **Step 3: Implement**
  - `agent-form.ts`: `handover: []` in `emptyForm`; in `formToInput`:

```ts
    handover_contacts: f.handover.map((h) => ({ contact_id: h.contactId, note: h.note.trim() })),
    tools: withHandoverTool(f.tools, f.handover.length > 0),
```

```ts
/** transfer_call follows the handover list; it is not a checkbox. */
export function withHandoverTool(tools: string[] | null, on: boolean): string[] | null {
  if (tools === null) return null;
  const rest = tools.filter((t) => t !== "transfer_call");
  return on ? [...rest, "transfer_call"] : rest;
}
```

  and in `validateForm`: >10 → `{ index: 10, message: "Up to 10 people." }`; first blank note → `{ index, message: "Say when to hand over to this person." }`; note > 200 chars → `{ index, message: "Keep it under 200 characters." }`.
  - `actions.ts`: `handover_contacts` in `AgentInput`. Show the API's 422 `detail` on save as today.
  - `agent-queries.ts`: after loading agents, one query `SELECT h.agent_id, h.contact_id, h.note, c.name, c.phone_e164 FROM agent_handover_contacts h JOIN contacts c ON c.id = h.contact_id WHERE h.agent_id = ANY($1) ORDER BY h.agent_id, h.position`, grouped into `handoverContacts`. Add `phoneContacts`.
  - `AgentSheet.tsx` `fromAgent`: `handover: a.handoverContacts.map((h) => ({ contactId: h.contactId, note: h.note }))`. Add `contacts: PhoneContact[]` to `Props`; both pages load `phoneContacts(orgId)` and pass it.

- [ ] **Step 4: Run, expect PASS** — `pnpm vitest run app/console/agents` and `pnpm tsc --noEmit`.

- [ ] **Step 5: Commit** `git add -A && git commit -m "feat(console): handover contacts data and form state"`

---

### Task 10: Console UI (hail-website)

**First action: invoke the `frontend-design:frontend-design` skill** (user asked for it). Design inside the existing console language: plain CSS tokens in `app/console/console.css` (`--c-ink`, `--c-bg`, `--c-paper`, `--c-mute`, `--c-tape`, `--c-live`), fonts Space Grotesk / Instrument Serif / JetBrains Mono, `ag-*` class family, `<Step label=…>` sections. No new UI library.

**Files:**
- Create: `app/console/agents/HandoverStep.tsx`
- Modify: `app/console/agents/AgentSheet.tsx` (new `<Step label="Hand over">` after "Tools", ~line 510)
- Modify: `app/console/activity/ActivityDrawer.tsx` (`eventSummary` ~341)
- Modify: `app/console/console.css` (`ag-ho-*`)
- Test: `app/console/activity/__tests__/` (existing drawer tests, or new `event-summary.test.ts` if `eventSummary` gets exported)

UI requirements (from the spec):
- Lede line: "If the caller needs a person, the agent can call one of these people and connect them. They see your Hail number. Numbers stay hidden from the AI."
- Picker: search contacts with a phone by name; already-picked contacts hidden from results.
- Row: name, number (mono), "when to use" text input (placeholder "e.g. billing questions"), up/down reorder buttons (keyboard-usable; no drag library), remove button.
- Empty state when the org has no contacts with a phone: link to `/console/contacts`.
- Row-level error from `validateForm` or from the API 422 `loc` index.
- Read-only rendering when `canManage` is false.
- Drawer line for `handover` events:
  - answered → `Handed to {name} · answered after {s} s`
  - no_answer → `Tried {name} · no answer`
  - busy → `Tried {name} · busy`
  - failed → `Tried {name} · could not connect`

- [ ] **Step 1: Failing test for the drawer line** — export `eventSummary` (or a new pure `handoverSummary(payload)`) and test the four outcomes above with `ring_ms: 12000` → `12 s`.

- [ ] **Step 2: Run, expect FAIL**, implement, run, expect PASS.

- [ ] **Step 3: Build `HandoverStep.tsx`** per the skill's direction and the requirements above; wire into `AgentSheet.tsx`.

- [ ] **Step 4: Check in the browser** — `pnpm dev`, open `/console/agents/new` and an existing agent: add two contacts, reorder, save, reload, confirm order and notes persist; remove all, save; try a contact without a note (inline error). Open a call with a `handover` event in Activity.

- [ ] **Step 5: Full check** — `pnpm vitest run && pnpm tsc --noEmit && pnpm lint`.

- [ ] **Step 6: Commit** `git add -A && git commit -m "feat(console): hand over step and call drawer line"`, push, open PR in `r13i/hail-website` linking the hail PR. Merge order: hail PR first (migration 0053 + API field), then console.

---

## Rollout notes (for the PR bodies)

- Migration `0053` runs on deploy (GHA deploy on push to main).
- No new env vars. Uses the existing outbound trunks (`LIVEKIT_*_SIP_OUTBOUND_TRUNK_ID`).
- Inbound calls on a carrier with no outbound trunk configured cannot hand over; the endpoint denies and the agent says so.
- Manual test after deploy: agent with one handover contact (your own phone) → call the agent's number → ask for a person → phone rings from the Hail number → answer → hear the one-line intro → talk → hang up → call shows one `handover` event and is billed once.
