# Contacts v2 (hail repo) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Org-scoped contacts — a computed union of org members (phone on `users.phone_number`) and manual contacts — exposed via API routes, MCP tools, and a voicebot lookup tool.

**Architecture:** One new table (`contacts`) plus one new column (`users.phone_number`). The member∪manual union is computed in `hailhq.core.contacts.search_contacts`, the single source used by both the API routes and the voicebot's LiveKit function tool. MCP tools are thin wrappers over the API. `Principal` gains `user_id` so the member-phone routes can enforce self-or-admin.

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic + Pydantic v2; FastMCP server; LiveKit Agents voicebot; pytest.

**Spec:** `/Users/r/playground/hail-website/docs/superpowers/specs/2026-07-10-contacts-v2-design.md` (website repo). This is plan 1 of 2; plan 2 (website UI + seeding swap) is separate.

## Global Constraints

- **Git writes belong to the user.** Never run `git add`/`git commit`/`git stash`/`git rm`. Each "Commit" step means: hand the user the exact commands (one file per `git add` line) and continue.
- Work in `/Users/r/playground/hail`. Tests: `cd api && uv run pytest tests/<file> -v` (likewise `cd mcp` / `cd voicebot`). Lint gate per task: `uv run ruff check --fix . && uv run black .` in the touched package.
- Migrations are Alembic, numbered files in `api/migrations/versions/` — this plan adds `0029` and `0030` (verify `ls api/migrations/versions | tail -1` is still `0028_*` first; renumber if the repo moved).
- The `users` table is OWNED by better-auth (the website repo). We add one column and one minimal mapped model for it; never write any users column other than `phone_number`.
- E.164 validation: reuse the existing pattern in `core/hailhq/core/schemas.py` (see `CallCreate`'s `to`/`from_` field_validator) — do not invent a new regex.
- Response entry contract (verbatim from spec): `{ id: "member:<user_id>" | "<uuid>", kind: "member" | "manual", name, phone_e164 | null, email | null, role | null }`.
- Error semantics: POST duplicate `(org, phone_e164)` or `(org, email)` → 409; neither phone nor email → 422; PATCH/DELETE on `member:*` ids → 422 with detail `member contacts are managed via membership`; PATCH that would clear both fields → 422.
- Member-phone permissions: target is self, OR caller has role `owner` or `admin` in the org AND target is a member of the same org.

---

### Task 1: Migrations + models (`users.phone_number`, `contacts`)

**Files:**
- Create: `api/migrations/versions/0029_users_phone_number.py`
- Create: `api/migrations/versions/0030_contacts.py`
- Modify: `core/hailhq/core/models.py` (add `User` and `Contact` models; `OrganizationMember` already exists at line ~38)
- Test: `api/tests/test_contacts_core.py` (model round-trip only in this task; grows in Task 2)

**Interfaces:**
- Produces: `hailhq.core.models.User` (`__tablename__="users"`; columns `id: UUID pk`, `name: str`, `email: str`, `phone_number: str | None`) and `hailhq.core.models.Contact` (`id UUID pk default uuid4`, `organization_id UUID FK organizations.id ondelete CASCADE`, `name str`, `phone_e164 str | None`, `email str | None`, `created_by UUID | None FK users.id ondelete SET NULL`, `created_at/updated_at datetime`).

- [ ] **Step 1: Read the conventions**

Read `api/migrations/versions/0028_provider_config_multi.py` (header shape: `revision: str = "0029"`, `down_revision: Union[str, None] = "0028"`) and `core/hailhq/core/models.py` lines 26–60 (Base, `OrganizationMember`) plus the `Call` model (~333) for column style (`Mapped[...]`, `mapped_column`).

- [ ] **Step 2: Write migration 0029**

`api/migrations/versions/0029_users_phone_number.py`:

```python
"""users.phone_number: org-member phone, written via the Hail API only.

The users table is owned by better-auth (hail-website repo); this column is
named for a migration-free upgrade to better-auth's phone-number plugin.
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone_number", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "phone_number")
```

- [ ] **Step 3: Write migration 0030**

`api/migrations/versions/0030_contacts.py`:

```python
"""contacts: manual org contacts (phone and/or email)."""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contacts",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("phone_e164", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "phone_e164 IS NOT NULL OR email IS NOT NULL",
            name="contacts_phone_or_email",
        ),
    )
    op.create_index(
        "contacts_org_phone_key",
        "contacts",
        ["organization_id", "phone_e164"],
        unique=True,
        postgresql_where=sa.text("phone_e164 IS NOT NULL"),
    )
    op.create_index(
        "contacts_org_email_key",
        "contacts",
        ["organization_id", "email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    op.create_index("contacts_org_idx", "contacts", ["organization_id"])


def downgrade() -> None:
    op.drop_table("contacts")
```

- [ ] **Step 4: Add the models**

In `core/hailhq/core/models.py`, after `OrganizationMember` add (match the file's `Mapped`/`mapped_column` style exactly — copy imports already present):

```python
class User(Base):
    """Better-auth's users table (owned by hail-website). Mapped read-mostly;
    the ONLY column this codebase writes is phone_number."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    phone_number: Mapped[str | None] = mapped_column(Text, nullable=True)


class Contact(Base):
    """Manual org contact — phone and/or email (CHECK enforces at least one)."""

    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone_e164: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
```

Adjust import names to whatever the file actually uses (`PG_UUID` vs `UUID`, `Text`, `TIMESTAMP`, `text`, `ForeignKey` — all appear in existing models; reuse them, add none needlessly). If the file lacks a `users` mapping conflict check, grep first: `grep -n '"users"' core/hailhq/core/models.py` must return nothing before adding.

- [ ] **Step 5: Failing test — model round-trip**

`api/tests/test_contacts_core.py`:

```python
"""Contacts core: models (Task 1) and search_contacts union (Task 2)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from hailhq.core.models import Contact, User


@pytest.mark.anyio
async def test_contact_model_round_trip(async_session, org_and_key):
    org_id = org_and_key.org_id
    row = Contact(organization_id=org_id, name="Maya", phone_e164="+14155550100")
    async_session.add(row)
    await async_session.commit()

    got = (await async_session.execute(select(Contact))).scalar_one()
    assert got.name == "Maya"
    assert got.email is None
    assert got.id is not None


@pytest.mark.anyio
async def test_user_model_maps_users_table(async_session):
    uid = uuid.uuid4()
    async_session.add(User(id=uid, name="Ada", email=f"{uid}@example.com"))
    await async_session.commit()
    got = (await async_session.execute(select(User).where(User.id == uid))).scalar_one()
    assert got.phone_number is None
```

Check `api/tests/conftest.py` first for the real shape of `org_and_key` (line ~266) and the anyio/asyncio marker other tests use (`grep -n "anyio\|asyncio" api/tests/test_calls_api.py | head -3`) — mirror it exactly; adjust the fixture attribute (`org_and_key.org_id` vs tuple unpacking) to what conftest provides.

- [ ] **Step 6: Run to verify it fails**

Run: `cd api && uv run pytest tests/test_contacts_core.py -v`
Expected: FAIL — `ImportError: cannot import name 'Contact'`.

- [ ] **Step 7: Apply migrations to the test DB and make tests pass**

The test fixtures create the schema from `Base.metadata` or a migrated DB — check `core/hailhq/core/testing/fixtures.py` `db` fixture: if it runs `Base.metadata.create_all`, the new models suffice; if it runs alembic, run `cd api && uv run alembic upgrade head` against the test database first. Then:

Run: `cd api && uv run pytest tests/test_contacts_core.py -v`
Expected: 2 PASS.

- [ ] **Step 8: Lint + commit (hand to user)**

`cd api && uv run ruff check --fix . && uv run black .` (also in `core` if configured per-package).

```bash
git add -N api/migrations/versions/0029_users_phone_number.py
git add -p api/migrations/versions/0029_users_phone_number.py
git add -N api/migrations/versions/0030_contacts.py
git add -p api/migrations/versions/0030_contacts.py
git add -p core/hailhq/core/models.py
git add -N api/tests/test_contacts_core.py
git add -p api/tests/test_contacts_core.py
git commit -m "feat(contacts): users.phone_number + contacts table, mapped models"
```

---

### Task 2: `hailhq.core.contacts` — the union + schemas

**Files:**
- Create: `core/hailhq/core/contacts.py`
- Modify: `core/hailhq/core/schemas.py` (add `ContactEntry`, `ContactCreate`, `ContactPatch`, `ContactListResponse`, `MemberPhonePut`)
- Test: `api/tests/test_contacts_core.py` (extend)

**Interfaces:**
- Consumes: `User`, `Contact`, `OrganizationMember` models (Task 1).
- Produces:
  - `ContactEntry(BaseModel)`: `id: str`, `kind: Literal["member","manual"]`, `name: str`, `phone_e164: str | None`, `email: str | None`, `role: str | None`
  - `async search_contacts(session: AsyncSession, org_id: UUID, q: str | None = None, limit: int = 100) -> list[ContactEntry]` — members first (by name), then manual (by name); `q` ILIKE-filters name/email/phone on both branches; member ids are `f"member:{user_id}"`.
  - `ContactCreate`: `name: str` (min 1), `phone_e164: str | None`, `email: str | None` (EmailStr-ish lowercase-trim), model_validator: at least one of phone/email.
  - `ContactPatch`: all-optional `name/phone_e164/email`.
  - `MemberPhonePut`: `phone_e164: str` (E.164 validated, same validator as `CallCreate.to`).

- [ ] **Step 1: Failing tests — union semantics**

Append to `api/tests/test_contacts_core.py`:

```python
from hailhq.core.contacts import search_contacts
from hailhq.core.models import OrganizationMember


async def _seed_member(session, org_id, *, name, email, phone=None, role="member"):
    uid = uuid.uuid4()
    session.add(User(id=uid, name=name, email=email, phone_number=phone))
    session.add(
        OrganizationMember(organization_id=org_id, user_id=uid, role=role)
    )
    await session.commit()
    return uid


@pytest.mark.anyio
async def test_union_members_first_then_manual(async_session, org_and_key):
    org_id = org_and_key.org_id
    uid = await _seed_member(
        async_session, org_id, name="Ada", email="ada@acme.com", phone="+15550001"
    )
    async_session.add(Contact(organization_id=org_id, name="Maya", email="maya@x.com"))
    await async_session.commit()

    entries = await search_contacts(async_session, org_id)
    kinds = [e.kind for e in entries]
    assert kinds == ["member", "manual"]
    member = entries[0]
    assert member.id == f"member:{uid}"
    assert member.phone_e164 == "+15550001"
    assert member.role == "member"
    assert entries[1].phone_e164 is None and entries[1].email == "maya@x.com"


@pytest.mark.anyio
async def test_q_filters_both_branches_case_insensitive(async_session, org_and_key):
    org_id = org_and_key.org_id
    await _seed_member(async_session, org_id, name="Ada Lovelace", email="ada@acme.com")
    async_session.add(Contact(organization_id=org_id, name="Maya", phone_e164="+14155550100"))
    async_session.add(Contact(organization_id=org_id, name="Bob", email="bob@x.com"))
    await async_session.commit()

    assert [e.name for e in await search_contacts(async_session, org_id, q="ada")] == ["Ada Lovelace"]
    assert [e.name for e in await search_contacts(async_session, org_id, q="MAYA")] == ["Maya"]
    assert [e.name for e in await search_contacts(async_session, org_id, q="4155550100")] == ["Maya"]


@pytest.mark.anyio
async def test_org_isolation(async_session, org_and_key, make_org):
    org_id = org_and_key.org_id
    other_org = await make_org(async_session)
    async_session.add(Contact(organization_id=other_org, name="Other", email="o@x.com"))
    await async_session.commit()
    assert await search_contacts(async_session, org_id) == []
```

`make_org`: check conftest for an org-creating fixture/factory; if none exists, inline-create an `Organization` row (grep the model name) instead of a fixture. Adjust to reality; the assertion set is the contract.

- [ ] **Step 2: Run to verify failure**

Run: `cd api && uv run pytest tests/test_contacts_core.py -v`
Expected: new tests FAIL — no module `hailhq.core.contacts`.

- [ ] **Step 3: Schemas**

In `core/hailhq/core/schemas.py` (near the other response models), add:

```python
class ContactEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: Literal["member", "manual"]
    name: str
    phone_e164: str | None = None
    email: str | None = None
    role: str | None = None


class ContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    phone_e164: str | None = None
    email: str | None = None

    _validate_phone = field_validator("phone_e164")(  # reuse CallCreate's normalizer
        classmethod(lambda cls, v: _normalize_e164(v) if v is not None else None)
    )

    @model_validator(mode="after")
    def _phone_or_email(self) -> "ContactCreate":
        if self.phone_e164 is None and self.email is None:
            raise ValueError("provide at least one of phone_e164 or email")
        return self


class ContactPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    phone_e164: str | None = None
    email: str | None = None


class ContactListResponse(BaseModel):
    items: list[ContactEntry]


class MemberPhonePut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone_e164: str
```

IMPORTANT: `_normalize_e164` — find how `CallCreate` validates `to` (field_validator around line ~145 of schemas.py). If the logic is inline in a validator method, extract it into a module-level `_normalize_e164(value: str) -> str` and have BOTH `CallCreate` and the new schemas call it — do not duplicate the regex. `MemberPhonePut.phone_e164` gets the same validator. If `email` validation convention exists in the file (e.g. `EmailStr`), match it; otherwise store lowercase-stripped as-is.

- [ ] **Step 4: The union module**

`core/hailhq/core/contacts.py`:

```python
"""Org contacts: computed union of members (users.phone_number) and manual rows.

Single source for the API routes AND the voicebot lookup tool — keep this the
only place that knows what "a contact" is.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import Contact, OrganizationMember, User
from hailhq.core.schemas import ContactEntry


async def search_contacts(
    session: AsyncSession,
    org_id: UUID,
    q: str | None = None,
    limit: int = 100,
) -> list[ContactEntry]:
    like = f"%{q.strip()}%" if q and q.strip() else None

    member_stmt = (
        select(User.id, User.name, User.email, User.phone_number, OrganizationMember.role)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == org_id)
        .order_by(User.name.asc())
        .limit(limit)
    )
    if like is not None:
        member_stmt = member_stmt.where(
            or_(User.name.ilike(like), User.email.ilike(like), User.phone_number.ilike(like))
        )

    manual_stmt = (
        select(Contact)
        .where(Contact.organization_id == org_id)
        .order_by(Contact.name.asc())
        .limit(limit)
    )
    if like is not None:
        manual_stmt = manual_stmt.where(
            or_(Contact.name.ilike(like), Contact.email.ilike(like), Contact.phone_e164.ilike(like))
        )

    entries: list[ContactEntry] = [
        ContactEntry(
            id=f"member:{uid}",
            kind="member",
            name=name,
            phone_e164=phone,
            email=email,
            role=role,
        )
        for uid, name, email, phone, role in (await session.execute(member_stmt)).all()
    ]
    entries.extend(
        ContactEntry(
            id=str(c.id),
            kind="manual",
            name=c.name,
            phone_e164=c.phone_e164,
            email=c.email,
            role=None,
        )
        for c in (await session.execute(manual_stmt)).scalars()
    )
    return entries[:limit]
```

- [ ] **Step 5: Run to verify pass**

Run: `cd api && uv run pytest tests/test_contacts_core.py -v`
Expected: all PASS.

- [ ] **Step 6: Lint + commit (hand to user)**

```bash
git add -N core/hailhq/core/contacts.py
git add -p core/hailhq/core/contacts.py
git add -p core/hailhq/core/schemas.py
git add -p api/tests/test_contacts_core.py
git commit -m "feat(contacts): search_contacts union + contact schemas"
```

---

### Task 3: `Principal.user_id`

**Files:**
- Modify: `api/hailhq/api/deps.py` (Principal model + both auth paths)
- Test: extend the existing auth tests (`api/tests/test_auth.py`, `api/tests/test_auth_jwt.py`) — find where Principal assertions live.

**Interfaces:**
- Produces: `Principal.user_id: uuid.UUID | None` — the caller's user id. API-key path: `api_keys.reference_id` cast to UUID (the existing members join already casts it — reuse that value). JWT path: the token's `sub`. Shared-key (`HAIL_API_KEY`) path: `None`.

- [ ] **Step 1: Read the three auth paths**

Read `api/hailhq/api/deps.py` fully (Principal ~69, api-key resolution ~160-210, shared-key path, JWT path ~280) and `api/hailhq/api/auth.py` (where `sub` is decoded). Identify exactly where each path constructs `Principal(...)`.

- [ ] **Step 2: Failing tests**

Add to the relevant existing test files (mirror their fixtures — they already mint keys/JWTs):

```python
@pytest.mark.anyio
async def test_api_key_principal_carries_user_id(client, org_and_key):
    # existing pattern: call any authed endpoint with the org key and inspect
    # the principal via a route that echoes it, OR unit-test the dependency
    # directly the way test_auth.py already does — follow that file's style.
    principal = await resolve_principal_for_key(org_and_key.key)  # adapt to file's helper
    assert principal.user_id == org_and_key.user_id


@pytest.mark.anyio
async def test_jwt_principal_carries_sub_as_user_id(...):
    # mirror test_auth_jwt.py's existing happy-path test; assert
    # principal.user_id == the sub the fixture minted.
    ...
```

These two tests MUST be written concretely against the real helpers in those files (read them first — the names above are placeholders for the file's own fixtures, which the implementer replaces with the actual ones; the assertions are the contract).

- [ ] **Step 3: Implement**

- Add `user_id: uuid.UUID | None` to `Principal` (after `api_key_id`).
- API-key path: the members join at deps.py:164-180 already computes `cast(ApiKey.reference_id, PG_UUID)` — select it (or reuse `api_key.reference_id`) and pass `user_id=UUID(api_key.reference_id)` (guard: `try/except ValueError → None` if reference_id isn't a UUID).
- JWT path: pass `user_id=UUID(claims["sub"])` (same guard).
- Shared-key path: `user_id=None`.

- [ ] **Step 4: Run the full auth suite**

Run: `cd api && uv run pytest tests/test_auth.py tests/test_auth_jwt.py tests/test_auth_shared.py -v`
Expected: all PASS including the two new tests.

- [ ] **Step 5: Lint + commit (hand to user)**

```bash
git add -p api/hailhq/api/deps.py
git add -p api/tests/test_auth.py
git add -p api/tests/test_auth_jwt.py
git commit -m "feat(auth): surface caller user_id on Principal"
```

---

### Task 4: Routes — `/contacts` CRUD + `/members/{id}/phone`

**Files:**
- Create: `api/hailhq/api/routes/contacts.py`
- Modify: `api/hailhq/api/main.py` (`include_router` beside the others, ~line 236)
- Test: `api/tests/test_contacts_api.py`

**Interfaces:**
- Consumes: `search_contacts`, schemas (Task 2), `Principal.user_id` (Task 3), `get_current_principal`, `get_session`, `unprocessable` (`api/hailhq/api/errors.py:19`).
- Produces (the wire contract plans 2 and Tasks 5–6 rely on):
  - `GET /contacts?q=&limit=` → `ContactListResponse` `{items: [...]}`
  - `POST /contacts` body `ContactCreate` → 201 `ContactEntry`; 409 on either unique clash
  - `PATCH /contacts/{id}` body `ContactPatch` → `ContactEntry`; `member:*` → 422; clears-both → 422; 404 unknown/other-org
  - `DELETE /contacts/{id}` → 204; `member:*` → 422
  - `PUT /members/{user_id}/phone` (also accepts literal `me`) body `MemberPhonePut` → 200 `{user_id, phone_e164}`; `DELETE` → 204 (clears)
  - Permissions: 401 without auth (existing dependency), 403 when target ≠ self and caller's role in the org is not `owner`/`admin`; 404 when target isn't a member of the caller's org; 403 when `user_id=None` principal (shared key) targets `me`.

- [ ] **Step 1: Failing tests**

`api/tests/test_contacts_api.py` — mirror `test_emails_api.py` / `test_calls_api.py` client usage (the `client` fixture + `org_and_key` bearer). Cover, at minimum, each row of this table (one test per row, names as given):

| test | arrange | act | assert |
|---|---|---|---|
| test_list_union | seed member w/ phone + manual contact | GET /contacts | 200; two items; member first; ids `member:<uuid>` / `<uuid>` |
| test_list_q_filter | same | GET /contacts?q=maya | only the manual row |
| test_create_manual_phone_only | — | POST {name, phone_e164} | 201; kind manual; email null |
| test_create_email_only | — | POST {name, email} | 201 |
| test_create_neither_422 | — | POST {name} | 422 |
| test_create_duplicate_phone_409 | existing row same phone | POST | 409 |
| test_create_duplicate_email_409 | existing row same email | POST | 409 |
| test_patch_manual | manual row | PATCH name | 200; new name |
| test_patch_member_422 | member | PATCH member:<uid> | 422, detail mentions membership |
| test_patch_clear_both_422 | phone-only row | PATCH {phone_e164: null} | 422 |
| test_delete_manual_204_and_gone | manual row | DELETE then GET | 204; list shrinks |
| test_delete_member_422 | member | DELETE member:<uid> | 422 |
| test_put_own_phone_via_me | JWT principal for a member | PUT /members/me/phone | 200; users.phone_number updated |
| test_admin_sets_other_phone | caller role owner | PUT /members/<other>/phone | 200 |
| test_member_cannot_set_other_403 | caller role member | PUT /members/<other>/phone | 403 |
| test_phone_target_not_in_org_404 | other-org user | PUT | 404 |
| test_delete_phone_clears | member w/ phone | DELETE /members/me/phone | 204; column null |
| test_other_org_contact_404 | contact in org B | PATCH/DELETE with org A key | 404 |

Use the JWT fixtures from `test_auth_jwt.py` for the `me`/self cases (API-key principals carry user_id too — Task 3 — so either works; write `me` tests through whichever auth path the fixtures make easy, but at least one test must exercise `me`).

- [ ] **Step 2: Run to verify failure**

Run: `cd api && uv run pytest tests/test_contacts_api.py -v`
Expected: FAIL — 404s (router not registered).

- [ ] **Step 3: Implement the router**

`api/hailhq/api/routes/contacts.py`:

```python
"""Org contacts: computed member∪manual list, manual CRUD, member phones.

GET    /contacts        - union list (members live from users/members; manual rows)
POST   /contacts        - create manual contact (phone and/or email)
PATCH  /contacts/{id}   - manual only; member:* ids are managed via membership
DELETE /contacts/{id}   - manual only
PUT    /members/{user_id|me}/phone - self or org owner/admin
DELETE /members/{user_id|me}/phone
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.core.contacts import search_contacts
from hailhq.core.db import get_session
from hailhq.core.models import Contact, OrganizationMember, User
from hailhq.core.schemas import (
    ContactCreate,
    ContactEntry,
    ContactListResponse,
    ContactPatch,
    MemberPhonePut,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["contacts"])

_MEMBER_ID_DETAIL = "member contacts are managed via membership"


def _manual_uuid_or_422(contact_id: str) -> UUID:
    if contact_id.startswith("member:"):
        raise unprocessable(_MEMBER_ID_DETAIL, loc=["path", "contact_id"])
    try:
        return UUID(contact_id)
    except ValueError as exc:
        raise unprocessable(f"invalid contact id: {contact_id}") from exc


async def _get_manual_or_404(
    db: AsyncSession, org_id: UUID, contact_id: str
) -> Contact:
    cid = _manual_uuid_or_422(contact_id)
    row = (
        await db.execute(
            select(Contact).where(Contact.id == cid, Contact.organization_id == org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="contact not found")
    return row


def _entry(row: Contact) -> ContactEntry:
    return ContactEntry(
        id=str(row.id), kind="manual", name=row.name,
        phone_e164=row.phone_e164, email=row.email, role=None,
    )


@router.get("/contacts", response_model=ContactListResponse)
async def list_contacts(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> ContactListResponse:
    items = await search_contacts(db, principal.organization_id, q=q, limit=limit)
    return ContactListResponse(items=items)


@router.post("/contacts", response_model=ContactEntry, status_code=http_status.HTTP_201_CREATED)
async def create_contact(
    body: ContactCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ContactEntry:
    row = Contact(
        organization_id=principal.organization_id,
        name=body.name,
        phone_e164=body.phone_e164,
        email=body.email,
        created_by=principal.user_id,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="a contact with that phone or email already exists",
        ) from exc
    await db.refresh(row)
    return _entry(row)


@router.patch("/contacts/{contact_id}", response_model=ContactEntry)
async def patch_contact(
    contact_id: str,
    body: ContactPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ContactEntry:
    row = await _get_manual_or_404(db, principal.organization_id, contact_id)
    data = body.model_dump(exclude_unset=True)
    next_phone = data.get("phone_e164", row.phone_e164)
    next_email = data.get("email", row.email)
    if next_phone is None and next_email is None:
        raise unprocessable("a contact needs at least one of phone_e164 or email")
    for field, value in data.items():
        setattr(row, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="a contact with that phone or email already exists",
        ) from exc
    await db.refresh(row)
    return _entry(row)


@router.delete("/contacts/{contact_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    row = await _get_manual_or_404(db, principal.organization_id, contact_id)
    await db.delete(row)
    await db.commit()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


async def _resolve_phone_target(
    db: AsyncSession, principal: Principal, user_id: str
) -> UUID:
    """Resolve `me`/UUID, enforce self-or-admin, and same-org membership."""
    if user_id == "me":
        if principal.user_id is None:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="this credential has no user identity; pass an explicit user id",
            )
        target = principal.user_id
    else:
        try:
            target = UUID(user_id)
        except ValueError as exc:
            raise unprocessable(f"invalid user id: {user_id}") from exc

    target_role = (
        await db.execute(
            select(OrganizationMember.role).where(
                OrganizationMember.organization_id == principal.organization_id,
                OrganizationMember.user_id == target,
            )
        )
    ).scalar_one_or_none()
    if target_role is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="member not found")

    if target != principal.user_id:
        caller_role = (
            await db.execute(
                select(OrganizationMember.role).where(
                    OrganizationMember.organization_id == principal.organization_id,
                    OrganizationMember.user_id == principal.user_id,
                )
            )
        ).scalar_one_or_none()
        if caller_role not in ("owner", "admin"):
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="only the member themselves or an org owner/admin can set a member phone",
            )
    return target


@router.put("/members/{user_id}/phone")
async def put_member_phone(
    user_id: str,
    body: MemberPhonePut,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    target = await _resolve_phone_target(db, principal, user_id)
    await db.execute(update(User).where(User.id == target).values(phone_number=body.phone_e164))
    await db.commit()
    return {"user_id": str(target), "phone_e164": body.phone_e164}


@router.delete("/members/{user_id}/phone", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_member_phone(
    user_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    target = await _resolve_phone_target(db, principal, user_id)
    await db.execute(update(User).where(User.id == target).values(phone_number=None))
    await db.commit()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
```

Register in `api/hailhq/api/main.py`: import `contacts as contacts_routes` beside the other route imports and add `app.include_router(contacts_routes.router)` after the sms router (line ~242).

- [ ] **Step 4: Run to verify pass**

Run: `cd api && uv run pytest tests/test_contacts_api.py tests/test_contacts_core.py -v`
Expected: all PASS. Then the full API suite: `cd api && uv run pytest` — no regressions.

- [ ] **Step 5: Lint + commit (hand to user)**

```bash
git add -N api/hailhq/api/routes/contacts.py
git add -p api/hailhq/api/routes/contacts.py
git add -p api/hailhq/api/main.py
git add -N api/tests/test_contacts_api.py
git add -p api/tests/test_contacts_api.py
git commit -m "feat(api): /contacts union CRUD + member phone routes"
```

---

### Task 5: MCP tools

**Files:**
- Modify: `mcp/hailhq/mcp/hail_client.py` (two methods)
- Modify: `mcp/hailhq/mcp/tools.py` (three tools in `register_tools`, ~line 448)
- Test: `mcp/tests/test_tools.py` (extend, mirroring its existing stub-client pattern)

**Interfaces:**
- Consumes: Task 4's wire contract.
- Produces MCP tools: `list_contacts()`, `lookup_contact(query: str)`, `create_contact(name: str, phone_e164: str | None = None, email: str | None = None)`.

- [ ] **Step 1: Read the pattern**

Read `mcp/hailhq/mcp/tools.py` `register_tools` + one tool (`place_call_tool` at ~461) and `hail_client.py` `list_calls` (~151). Mirror error formatting (`_format_api_error`) and the `_client_for(ctx)` context manager exactly.

- [ ] **Step 2: Failing tests**

Extend `mcp/tests/test_tools.py` following its existing fake-client style: three tests — `list_contacts` calls `GET /contacts`; `lookup_contact("maya")` passes `q=maya`; `create_contact` posts the JSON body and surfaces a 409 as the formatted API error (assert on the error dict the file's other tests use).

- [ ] **Step 3: Implement client methods**

In `hail_client.py` beside `list_calls`:

```python
    async def list_contacts(self, q: str | None = None, limit: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if q is not None:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        resp = await self._client.get("/contacts", params=params)
        return _decode_or_raise(resp)

    async def create_contact(
        self, name: str, phone_e164: str | None = None, email: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if phone_e164 is not None:
            body["phone_e164"] = phone_e164
        if email is not None:
            body["email"] = email
        resp = await self._client.post("/contacts", json=body)
        return _decode_or_raise(resp)
```

(Adapt `_decode_or_raise` to the file's actual decode/raise helper names — `_decode` + explicit raise pattern per the neighbors.)

- [ ] **Step 4: Implement tools**

In `register_tools`, after the existing tools:

```python
    @mcp_app.tool(name="list_contacts")
    async def list_contacts_tool(ctx: Context) -> dict[str, Any]:
        """List the workspace's contacts: org members (with their phone/email)
        plus manually saved contacts. Use lookup_contact for name searches."""
        async with _client_for(ctx) as client:
            return await client.list_contacts()

    @mcp_app.tool(name="lookup_contact")
    async def lookup_contact_tool(ctx: Context, query: str) -> dict[str, Any]:
        """Find a contact by name, email, or phone fragment. Resolve a person
        to their phone_e164/email BEFORE calling place_call or send_email."""
        async with _client_for(ctx) as client:
            return await client.list_contacts(q=query, limit=10)

    @mcp_app.tool(name="create_contact")
    async def create_contact_tool(
        ctx: Context,
        name: str,
        phone_e164: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        """Save a new contact for the workspace. Provide at least one of
        phone_e164 (E.164, e.g. +14155551234) or email."""
        async with _client_for(ctx) as client:
            return await client.create_contact(name=name, phone_e164=phone_e164, email=email)
```

Wrap in the same try/except `HailAPIError` → `_format_api_error` shape the existing tools use (copy it exactly).

- [ ] **Step 5: Run tests + lint + commit (hand to user)**

Run: `cd mcp && uv run pytest tests/test_tools.py -v` — all PASS; then full `cd mcp && uv run pytest`.

```bash
git add -p mcp/hailhq/mcp/hail_client.py
git add -p mcp/hailhq/mcp/tools.py
git add -p mcp/tests/test_tools.py
git commit -m "feat(mcp): list_contacts / lookup_contact / create_contact tools"
```

---

### Task 6: Voicebot `lookup_contact` function tool

**Files:**
- Modify: `voicebot/hailhq/voicebot/agent.py` (Agent construction at ~707; org resolution in `entrypoint`)
- Test: `voicebot/tests/test_agent.py` (extend, using its `_fakes.py`/conftest DB pattern)

**Interfaces:**
- Consumes: `hailhq.core.contacts.search_contacts`, the voicebot's existing `session_scope` (used by `write_call_event` at agent.py ~250), `Call` model (org lookup by `call_id`).
- Produces: a LiveKit function tool `lookup_contact(query: str) -> str` returning up to 5 matches as lines `name · phone · email` (misses → `"no contacts matched"`), org-scoped to the call's organization.

- [ ] **Step 1: Read the wiring**

Read `agent.py` `entrypoint` (metadata parse, call_id), `write_call_event` (session_scope usage), and check the installed livekit-agents API for tools: `grep -rn "def function_tool\|class FunctionTool" .venv/lib/python*/site-packages/livekit/agents/llm/*.py | head` — confirm the decorator import path (`from livekit.agents import function_tool` in current versions) and that `Agent(instructions=..., tools=[...])` accepts a tools list. If the installed version wires tools differently (e.g. `@function_tool` methods on an `Agent` subclass), follow the installed version — the test in Step 2 pins the behavior either way.

- [ ] **Step 2: Failing test**

In `voicebot/tests/test_agent.py` (mirror its DB fixture style):

```python
@pytest.mark.anyio
async def test_lookup_contact_tool_org_scoped(seeded_db):  # adapt fixture names
    org_id = await seed_org(...)
    await seed_member(org_id, name="Maya", phone="+14155550100")
    other = await seed_org(...)
    await seed_manual_contact(other, name="Maya Other", email="x@y.z")

    tool = build_lookup_contact_tool(org_id)
    out = await tool("maya")            # call the underlying fn the way the
                                        # file's other tests invoke helpers
    assert "Maya" in out and "+14155550100" in out
    assert "Maya Other" not in out


@pytest.mark.anyio
async def test_lookup_contact_tool_no_match(seeded_db):
    org_id = await seed_org(...)
    tool = build_lookup_contact_tool(org_id)
    assert await tool("nobody") == "no contacts matched"
```

Concrete fixture names come from `voicebot/tests/conftest.py` — read it first and adapt seeding to its helpers; the assertions are the contract.

- [ ] **Step 3: Implement**

In `agent.py`:

```python
def build_lookup_contact_tool(org_id: UUID):
    """Org-scoped contact lookup for the live call. Uses the same
    core.contacts union as the API; results are compact text for speech."""

    @function_tool()
    async def lookup_contact(query: str) -> str:
        """Look up a person in this workspace's contacts by name, email, or
        phone fragment. Returns up to 5 matches as `name · phone · email`."""
        async with session_scope() as session:
            entries = await search_contacts(session, org_id, q=query, limit=5)
        if not entries:
            return "no contacts matched"
        return "\n".join(
            f"{e.name} · {e.phone_e164 or 'no phone'} · {e.email or 'no email'}"
            for e in entries
        )

    return lookup_contact
```

In `entrypoint`, after `metadata` parse and before the Agent is built, resolve the org once:

```python
    async with session_scope() as session:
        org_id = (
            await session.execute(
                select(Call.organization_id).where(Call.id == call_id)
            )
        ).scalar_one_or_none()

    tools = [build_lookup_contact_tool(org_id)] if org_id is not None else []
    agent = Agent(
        instructions=build_instructions(metadata.get("system_prompt")),
        tools=tools,
    )
```

(Import `select`, `UUID`, `function_tool`, `search_contacts`, `Call` as needed; `session_scope` is already imported for call-event writes. If `org_id` is None — call row missing — log a warning and proceed toolless; never block the call.)

- [ ] **Step 4: Run tests + lint + commit (hand to user)**

Run: `cd voicebot && uv run pytest tests/test_agent.py -v` — new tests PASS; then full `cd voicebot && uv run pytest`.

```bash
git add -p voicebot/hailhq/voicebot/agent.py
git add -p voicebot/tests/test_agent.py
git commit -m "feat(voicebot): org-scoped lookup_contact function tool"
```

---

### Task 7: End-to-end verification

**Files:** none — verification only.

- [ ] **Step 1: Full gates**

`cd api && uv run pytest && uv run ruff check . && uv run black --check .` — repeat for `mcp` and `voicebot`. All green.

- [ ] **Step 2: Migration dry-run**

Local stack (`docker compose -f docker-compose.yml -f docker-compose.local.yml up -d postgres`), then `cd api && uv run alembic upgrade head`; verify with `psql`: `\d contacts` shows the CHECK + two partial uniques; `\d users` shows `phone_number`.

- [ ] **Step 3: Live smoke (local API)**

Start the API (`cd api && uv run uvicorn hailhq.api.main:app --port 8080`), then with a seeded org key:
`curl -H "Authorization: Bearer $KEY" localhost:8080/contacts` → `{"items":[...]}` with the seeded member;
`curl -X POST … /contacts -d '{"name":"Maya","phone_e164":"+14155550100"}'` → 201; repeat → 409;
`curl -X PUT … /members/me/phone -d '{"phone_e164":"+15550001"}'` → 200 (JWT auth path).

- [ ] **Step 4: Hand off**

Report to the owner: migration `0029`+`0030` need `uv run alembic upgrade head` on the shared prod DB at deploy; the website repo's plan 2 (console two-group UI + `PUT /members/me/phone` seeding swap + better-auth `additionalFields` registration of `phoneNumber`) unblocks once these routes deploy.
