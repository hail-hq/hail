# Superadmin Role Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hail staff (`*@hail.so`, verified, Google or email login) can open any customer org in the console with owner powers, never appear as a member, and are logged as "Hail support".

**Architecture:** No `members` rows. The website computes `superadmin` per request from the user row, lets a superadmin set the session's active org to any org, and mints the console's 5-minute API JWT with `superadmin: true`. The API accepts that claim in place of a membership for the `activeOrganizationId` org, exposes it on `Principal`, gates `/admin/*` on it, and records `actor_user_id` + `actor_kind` on audit rows. API keys, CLI and MCP tokens never carry the claim.

**Tech Stack:** hail-hq/hail: FastAPI, SQLAlchemy async, Alembic, pytest (`uv run --package hailhq-api pytest`), ruff, black. hail-website: Next.js 15 App Router, Better Auth 1.6.11 (`jwt`, `organization`, `lastLoginMethod` plugins), `pg` pool, vitest (`npx vitest run`), eslint.

**Spec:** `docs/superpowers/specs/2026-09-25-superadmin-design.md` (this repo).

**Repos / worktrees:**
- API: `/Users/r/playground/hail-superadmin`, branch `feat/superadmin` (from `origin/main`). Tasks 1–5.
- Website: `/Users/r/playground/hail-website-superadmin`, branch `feat/superadmin` (from `origin/feat/console-verification`, PR r13i/hail-website#94, which must merge first). Tasks 6–11. `node_modules` is a symlink to the main checkout.

## Global Constraints

- Superadmin = `email` ends with `@hail.so` AND `email_verified` AND `users.last_login_method` in `{"google", "email"}`. GitHub never qualifies.
- Never insert a `members` row for a superadmin. Never return the role in any member listing.
- The `superadmin` JWT claim is set only by the website's `definePayload`. The API trusts it only on the JWT path (signature already verified); never on API-key or shared-key paths.
- Audit `actor_kind` values: `api_key`, `user`, `superadmin`, `system`. Console copy for `superadmin` rows: "Hail support".
- No new env vars. No Co-Authored-By trailers in commits.
- Commit messages: Conventional Commits (`feat(auth): …`).

## Review Focus

1. A JWT with `superadmin: true` but no `activeOrganizationId` → must resolve to the user's own membership like today (test in Task 2).
2. A JWT with `superadmin: true` whose `activeOrganizationId` does not exist → 403 "organization not found", not a crash (Task 2).
3. An API key owned by a superadmin calling `/admin/verifications` → 403 (Task 3).
4. A non-superadmin calling `switchOrgAction` / `searchOrgsAction` → error, session untouched (Task 9).
5. A superadmin whose last login was GitHub → `superadmin` false, no claim, no switcher (Task 6, Task 8).

---

### Task 1: Audit actor columns (API)

**Files:**
- Create: `api/migrations/versions/0047_audit_actor.py`
- Modify: `core/hailhq/core/models.py:1138-1163` (AuditLog)
- Modify: `api/hailhq/api/audit.py:37-65`
- Modify: every `write_audit_log(` caller (30 sites, `git grep -n "write_audit_log(" -- api | grep -v "def \|tests"`)
- Test: `api/tests/test_audit_actor.py`

**Interfaces:**
- Produces: `write_audit_log(organization_id, api_key_id, action, resource_type, resource_id, payload, *, actor_user_id: UUID | None = None, actor_kind: str | None = None)`; helper `actor_of(principal) -> tuple[UUID | None, str]` in `audit.py` returning `(principal.user_id, "superadmin" if principal.superadmin else "api_key" if principal.api_key_id else "user" if principal.user_id else "system")`. Task 2 adds `Principal.superadmin`; until then `getattr(principal, "superadmin", False)`.

- [ ] **Step 1: Write the failing test** `api/tests/test_audit_actor.py`

```python
import uuid
from types import SimpleNamespace

from hailhq.api.audit import actor_of, write_audit_log
from hailhq.core.models import AuditLog
from sqlalchemy import select


def test_actor_of_classifies_principals():
    u = uuid.uuid4()
    assert actor_of(SimpleNamespace(user_id=u, api_key_id=uuid.uuid4(), superadmin=False)) == (u, "api_key")
    assert actor_of(SimpleNamespace(user_id=u, api_key_id=None, superadmin=False)) == (u, "user")
    assert actor_of(SimpleNamespace(user_id=u, api_key_id=None, superadmin=True)) == (u, "superadmin")
    assert actor_of(SimpleNamespace(user_id=None, api_key_id=None, superadmin=False)) == (None, "system")


async def test_write_audit_log_stores_actor(async_session):
    org, user = uuid.uuid4(), uuid.uuid4()
    await write_audit_log(org, None, "verification.approve", "carrier_verification", None, {}, actor_user_id=user, actor_kind="superadmin")
    row = (await async_session.execute(select(AuditLog).where(AuditLog.organization_id == org))).scalar_one()
    assert row.actor_user_id == user and row.actor_kind == "superadmin"
```

- [ ] **Step 2: Run it** — `uv run --package hailhq-api pytest api/tests/test_audit_actor.py -q` → FAIL (ImportError `actor_of`).

- [ ] **Step 3: Migration** `api/migrations/versions/0047_audit_actor.py`

```python
"""Audit log: who acted (actor_user_id) and as what (actor_kind).

actor_kind: api_key | user | superadmin | system. Existing rows stay NULL.

Revision ID: 0047
Revises: 0046
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("actor_user_id", UUID(as_uuid=True), nullable=True))
    op.add_column("audit_log", sa.Column("actor_kind", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "actor_kind")
    op.drop_column("audit_log", "actor_user_id")
```

Model: add after `api_key_id` in `AuditLog`:

```python
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # api_key | user | superadmin | system — who acted, beyond which key was used.
    actor_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
```

`audit.py`:

```python
def actor_of(principal: Any) -> tuple[UUID | None, str]:
    """(actor_user_id, actor_kind) for an authenticated principal."""
    if getattr(principal, "superadmin", False):
        return principal.user_id, "superadmin"
    if principal.api_key_id is not None:
        return principal.user_id, "api_key"
    if principal.user_id is not None:
        return principal.user_id, "user"
    return None, "system"
```

Add `actor_user_id: UUID | None = None, actor_kind: str | None = None` keyword params to `write_audit_log` and pass them to `AuditLog(...)`. Export `actor_of` in `__all__`.

- [ ] **Step 4: Update callers.** For each of the 30 sites that has a `principal` (or `admin`) in scope, add `actor_user_id=..., actor_kind=...` via `aid, kind = actor_of(principal)` or inline `*actor_of(principal)` is NOT possible with keywords — write `actor_user_id=actor_of(principal)[0], actor_kind=actor_of(principal)[1]` only if you must; prefer two locals. Internal routes (`routes/internal/*.py`) have no principal: pass `actor_kind="system"`. The three verification sites: approve/reject use `admin`; the recovery site in `_refresh` has no principal → `actor_user_id=row.approved_by, actor_kind="superadmin"`.

- [ ] **Step 5: Run** `uv run --package hailhq-api pytest api/tests/test_audit_actor.py api/tests/test_verifications_api.py api/tests/test_calls_api.py -q` → PASS. `uvx ruff check api core && uvx black --check api core`.

- [ ] **Step 6: Commit** `git add -A && git commit -m "feat(audit): record actor_user_id and actor_kind on audit rows"`

---

### Task 2: `Principal.superadmin` and the JWT claim (API)

**Files:**
- Modify: `api/hailhq/api/deps.py:69-91` (Principal), `:273-341` (`_principal_from_jwt`)
- Test: `api/tests/test_jwt_superadmin.py`

**Interfaces:**
- Produces: `Principal.superadmin: bool = False`. JWT path: claim `superadmin is True` + `activeOrganizationId` → org must exist in `organizations`; no membership needed; `superadmin=True`. Claim without `activeOrganizationId` → unchanged path, `superadmin=True` still set (harmless; the org is the user's own).

- [ ] **Step 1: Failing tests** `api/tests/test_jwt_superadmin.py` (copy `_member` and `_patch_jwt` from `api/tests/test_jwt_active_org.py`):

```python
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from hailhq.api import deps
from hailhq.core.models import Organization, OrganizationMember


async def _org(session, org_id):
    session.add(Organization(id=org_id, origin="human")); await session.commit()

async def _member(session, user_id, org_id):
    session.add(OrganizationMember(id=uuid.uuid4(), user_id=user_id, organization_id=org_id, role="owner", created_at=datetime.now(timezone.utc)))
    await session.commit()

def _patch_jwt(monkeypatch, claims):
    monkeypatch.setattr(deps, "get_jwks_cache", lambda: object())
    async def _fake_verify(*_a, **_k): return claims
    monkeypatch.setattr(deps, "verify_jwt", _fake_verify)


async def test_superadmin_claim_opens_a_foreign_org(async_session, monkeypatch):
    user, own, foreign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _member(async_session, user, own); await _org(async_session, foreign)
    _patch_jwt(monkeypatch, {"sub": str(user), "activeOrganizationId": str(foreign), "superadmin": True})
    p = await deps._principal_from_jwt("a.b.c", async_session)
    assert p.organization_id == foreign and p.superadmin is True and p.auth_kind == "jwt"


async def test_superadmin_claim_without_active_org_uses_own_membership(async_session, monkeypatch):
    user, own = uuid.uuid4(), uuid.uuid4()
    await _member(async_session, user, own)
    _patch_jwt(monkeypatch, {"sub": str(user), "superadmin": True})
    p = await deps._principal_from_jwt("a.b.c", async_session)
    assert p.organization_id == own


async def test_superadmin_claim_unknown_org_is_403(async_session, monkeypatch):
    user = uuid.uuid4()
    _patch_jwt(monkeypatch, {"sub": str(user), "activeOrganizationId": str(uuid.uuid4()), "superadmin": True})
    with pytest.raises(HTTPException) as exc:
        await deps._principal_from_jwt("a.b.c", async_session)
    assert exc.value.status_code == 403


async def test_non_boolean_superadmin_claim_is_ignored(async_session, monkeypatch):
    user, own, foreign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _member(async_session, user, own); await _org(async_session, foreign)
    _patch_jwt(monkeypatch, {"sub": str(user), "activeOrganizationId": str(foreign), "superadmin": "true"})
    with pytest.raises(HTTPException) as exc:
        await deps._principal_from_jwt("a.b.c", async_session)
    assert exc.value.status_code == 403


async def test_api_key_principal_is_never_superadmin():
    p = deps.Principal(auth_kind="apikey", api_key_id=uuid.uuid4(), user_id=uuid.uuid4(), organization_id=uuid.uuid4(), scopes=["*"])
    assert p.superadmin is False
```

- [ ] **Step 2: Run** → FAIL (`superadmin` unknown field / 403).

- [ ] **Step 3: Implement.** In `Principal` add `superadmin: bool = False` with a docstring line: "True only on the JWT path when the website minted `superadmin: true`; never for API keys." In `_principal_from_jwt`, after `user_uuid` is parsed:

```python
    is_superadmin = claims.get("superadmin") is True
    active_org_claim = claims.get("activeOrganizationId")
    if is_superadmin and active_org_claim:
        try:
            active_org_uuid = uuid.UUID(str(active_org_claim))
        except ValueError as exc:
            raise _unauthorized("jwt activeOrganizationId is not a valid org id") from exc
        exists = (
            await db.execute(select(Organization.id).where(Organization.id == active_org_uuid))
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="organization not found")
        return Principal(auth_kind="jwt", api_key_id=None, user_id=user_uuid, organization_id=active_org_uuid, scopes=_scopes_from_jwt(claims), superadmin=True)
```

Keep the existing membership code for every other case, and pass `superadmin=is_superadmin` in its final `Principal(...)`. Import `Organization` from `hailhq.core.models`.

- [ ] **Step 4: Run** `uv run --package hailhq-api pytest api/tests/test_jwt_superadmin.py api/tests/test_jwt_active_org.py api/tests/test_auth_jwt.py -q` → PASS.

- [ ] **Step 5: Commit** `git commit -am "feat(auth): honor the website's superadmin claim on the JWT path"`

---

### Task 3: Superadmin gate, contacts role, whoami (API)

**Files:**
- Modify: `api/hailhq/api/superadmin.py:17-23`
- Modify: `api/hailhq/api/routes/contacts.py:71-82` (`_member_role`) and its two callers (`:246-254`)
- Modify: `api/hailhq/api/routes/whoami.py` + `WhoamiResponse` in `core/hailhq/core/schemas.py`
- Modify: `openapi/openapi.yaml` (regenerate)
- Test: `api/tests/test_superadmin_gate.py`, extend `api/tests/test_whoami_api.py`

- [ ] **Step 1: Failing tests** `api/tests/test_superadmin_gate.py`:

```python
import uuid

import pytest
from fastapi import HTTPException
from hailhq.api.deps import Principal
from hailhq.api.superadmin import require_superadmin


def _p(**over):
    base = dict(auth_kind="jwt", api_key_id=None, user_id=uuid.uuid4(), organization_id=uuid.uuid4(), scopes=["*"], superadmin=False)
    return Principal(**{**base, **over})


async def test_jwt_superadmin_passes():
    p = _p(superadmin=True)
    assert await require_superadmin(p) is p


@pytest.mark.parametrize("over", [dict(), dict(auth_kind="apikey", api_key_id=uuid.uuid4(), superadmin=True), dict(auth_kind="shared", user_id=None, superadmin=True)])
async def test_everyone_else_is_403(over):
    with pytest.raises(HTTPException) as exc:
        await require_superadmin(_p(**over))
    assert exc.value.status_code == 403
```

Whoami: in `api/tests/test_whoami_api.py` add a JWT-path test asserting `body["superadmin"] is False` for a normal caller (mirror the existing JWT whoami test; if none exists, assert the field is present and `False` on the api-key test).

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.** `superadmin.py`:

```python
async def require_superadmin(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    """Only a console session the website minted with superadmin: true.
    API keys and the shared key are never superadmins, whatever the claim."""
    if principal.auth_kind == "jwt" and principal.superadmin:
        return principal
    raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="superadmin access required")
```

Update the module docstring (remove "denies everyone"). Contacts: where `_member_role(db, org_id, principal.user_id)` decides the caller's role, use `"owner" if principal.superadmin else await _member_role(...)`. Whoami: add `superadmin: bool = False` to `WhoamiResponse` (description: "True for a Hail staff console session acting on this organization.") and pass `superadmin=principal.superadmin` in both returns. Regenerate the spec:

```bash
uv run --directory api python -c "import json, yaml; from hailhq.api.main import app; yaml.safe_dump(json.loads(json.dumps(app.openapi())), open('../openapi/openapi.yaml','w'), sort_keys=False)" && npx -y prettier@3 --write openapi/openapi.yaml
```

- [ ] **Step 4: Run** `uv run --package hailhq-api pytest api/tests/test_superadmin_gate.py api/tests/test_whoami_api.py api/tests/test_contacts_api.py api/tests/test_verifications_api.py -q` → PASS (verification tests override the dependency; they still pass).

- [ ] **Step 5: Commit** `git commit -am "feat(auth): superadmin gate on admin routes; superadmin counts as owner; whoami reports it"`

---

### Task 4: Superadmin actions are audited as such (API)

**Files:**
- Modify: `api/hailhq/api/routes/verifications.py:725-758` (approve/reject audit calls) — done in Task 1 if `actor_of(admin)` was used; verify.
- Test: extend `api/tests/test_verifications_api.py::test_admin_approve…` (the existing approve test that overrides `require_superadmin`): give the override `superadmin=True` and assert the audit row.

- [ ] **Step 1: Failing test** — in the existing approve test, after the POST:

```python
    audits = (await async_session.execute(select(AuditLog).where(AuditLog.action == "verification.approve"))).scalars().all()
    assert audits[0].actor_kind == "superadmin" and audits[0].actor_user_id == ADMIN_USER_ID
```

(`ADMIN_USER_ID` is the `user_id` the test's `SimpleNamespace` override uses; add `superadmin=True` to that namespace.)

- [ ] **Step 2: Run** → FAIL if Task 1 missed the site; else PASS (then this task is verification only).
- [ ] **Step 3: Fix if needed; run** `uv run --package hailhq-api pytest api/tests/test_verifications_api.py -q` → PASS.
- [ ] **Step 4: Commit** `git commit -am "test(verifications): admin approvals are audited as superadmin"`

---

### Task 5: API PR

- [ ] `uv run --package hailhq-api pytest api -q` and `uv run --package hailhq-core pytest core -q` → all pass. `uvx ruff check api core`, `uvx black --check api core`.
- [ ] `git push -u origin feat/superadmin`; `gh pr create -R hail-hq/hail --base main --title "feat(auth): superadmin role (JWT claim, admin gate, audit actor)" --body` with: what the claim is, that API keys/CLI/MCP are unaffected, migration 0047, "deploy before hail-website's superadmin PR".

---

### Task 6: Superadmin detection (website)

**Files:**
- Modify: `lib/auth.ts:343` (`lastLoginMethod()` → `lastLoginMethod({ storeInDatabase: true })`), user `additionalFields` (`:172-215`) add `lastLoginMethod` mapping to `last_login_method`
- Create: `better-auth_migrations/2026-09-25T00-00-00.000Z.sql` with `ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_method text;`
- Create: `lib/superadmin.ts`
- Test: `lib/__tests__/superadmin.test.ts`

**Interfaces:**
- Produces: `isSuperadminUser(user: { email: string; emailVerified: boolean; lastLoginMethod?: string | null }): boolean`; `SUPERADMIN_DOMAIN = "@hail.so"`; `SUPERADMIN_LOGIN_METHODS = ["google", "email"]`.

- [ ] **Step 1: Failing test** `lib/__tests__/superadmin.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { isSuperadminUser } from "@/lib/superadmin";

const u = (over: Partial<{ email: string; emailVerified: boolean; lastLoginMethod: string | null }> = {}) => ({
  email: "r@hail.so", emailVerified: true, lastLoginMethod: "google", ...over,
});

describe("isSuperadminUser", () => {
  it("accepts a verified @hail.so user who signed in with Google or email", () => {
    expect(isSuperadminUser(u())).toBe(true);
    expect(isSuperadminUser(u({ lastLoginMethod: "email" }))).toBe(true);
  });
  it("rejects other domains, unverified emails, GitHub and unknown methods", () => {
    expect(isSuperadminUser(u({ email: "r@gmail.com" }))).toBe(false);
    expect(isSuperadminUser(u({ email: "r@hail.so.evil.com" }))).toBe(false);
    expect(isSuperadminUser(u({ emailVerified: false }))).toBe(false);
    expect(isSuperadminUser(u({ lastLoginMethod: "github" }))).toBe(false);
    expect(isSuperadminUser(u({ lastLoginMethod: null }))).toBe(false);
  });
  it("is case-insensitive on the domain", () => {
    expect(isSuperadminUser(u({ email: "R@HAIL.SO" }))).toBe(true);
  });
});
```

- [ ] **Step 2: Run** `npx vitest run lib/__tests__/superadmin.test.ts` → FAIL.

- [ ] **Step 3: Implement** `lib/superadmin.ts`:

```ts
// Hail staff detection. No database role: a superadmin is any verified
// @hail.so account whose last sign-in proved the mailbox (Google or the
// email+password flow with verification). GitHub can carry an unverified
// address, so it never qualifies.
export const SUPERADMIN_DOMAIN = "@hail.so";
export const SUPERADMIN_LOGIN_METHODS = ["google", "email"] as const;

export function isSuperadminUser(user: {
  email: string;
  emailVerified: boolean;
  lastLoginMethod?: string | null;
}): boolean {
  const email = user.email.trim().toLowerCase();
  return (
    email.endsWith(SUPERADMIN_DOMAIN) &&
    user.emailVerified === true &&
    SUPERADMIN_LOGIN_METHODS.includes((user.lastLoginMethod ?? "") as (typeof SUPERADMIN_LOGIN_METHODS)[number])
  );
}
```

In `lib/auth.ts`: `lastLoginMethod({ storeInDatabase: true })`; add to user `additionalFields`:

```ts
      // Written by the lastLoginMethod plugin on every sign-in; read by
      // lib/superadmin.ts. Never client-writable.
      lastLoginMethod: { type: "string", required: false, input: false, returned: true, fieldName: "last_login_method" },
```

Migration SQL file as above. Check the plugin's own schema name matches (`lastLoginMethod` field on `user`); if the plugin already declares the field, keep only the `fieldName` mapping.

- [ ] **Step 4: Run** `npx vitest run lib` → PASS. `npx tsc --noEmit` (ignore pre-existing errors in `lib/__tests__/*.test.ts`).
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat(auth): detect Hail staff superadmins from verified @hail.so logins"`

---

### Task 7: Session flag, role checks, active org (website)

**Files:**
- Modify: `lib/auth.ts:742-750` (`getCurrentSession`, `requireSession`), `:732-736` (`getActiveOrgIdForSession`)
- Modify: `lib/require-org-admin.ts`
- Modify: `lib/require-superadmin.ts` (from #94)
- Test: `lib/__tests__/require-org-admin.test.ts`, `lib/__tests__/require-superadmin.test.ts`

**Interfaces:**
- Produces: `getCurrentSession()` returns the Better Auth session plus `superadmin: boolean`; type `ConsoleSession`. `requireOrgAdmin()` returns `{ orgId, userId, orgName, session, superadmin }` and passes for superadmins. `getOrgMemberRole(orgId, userId, superadmin = false)` returns `"owner"` when `superadmin`. `isSuperadmin()` reads the session flag.

- [ ] **Step 1: Failing tests** `lib/__tests__/require-org-admin.test.ts`:

```ts
import { beforeEach, expect, it, vi } from "vitest";
const query = vi.fn();
vi.mock("@/lib/db", () => ({ pool: { query: (...a: unknown[]) => query(...a) } }));
const session = { user: { id: "u1" }, session: { activeOrganizationId: "org-x" }, superadmin: false };
vi.mock("@/lib/auth", () => ({
  requireSession: vi.fn(async () => session),
  getActiveOrgIdForSession: vi.fn(async () => "org-x"),
}));
import { getOrgMemberRole, requireOrgAdmin } from "@/lib/require-org-admin";

beforeEach(() => { query.mockReset(); session.superadmin = false; });

it("rejects a non-member", async () => {
  query.mockResolvedValue({ rows: [] });
  await expect(requireOrgAdmin()).rejects.toThrow(/Only admins and owners/);
});
it("lets a superadmin through as owner of any org", async () => {
  session.superadmin = true;
  query.mockResolvedValue({ rows: [{ name: "Acme" }] }); // org name lookup only
  const out = await requireOrgAdmin();
  expect(out).toMatchObject({ orgId: "org-x", orgName: "Acme", superadmin: true });
  expect(await getOrgMemberRole("org-x", "u1", true)).toBe("owner");
});
```

`lib/__tests__/require-superadmin.test.ts`:

```ts
import { expect, it, vi } from "vitest";
const getCurrentSession = vi.fn();
vi.mock("@/lib/auth", () => ({ getCurrentSession: () => getCurrentSession() }));
import { isSuperadmin } from "@/lib/require-superadmin";
it("reads the session flag", async () => {
  getCurrentSession.mockResolvedValueOnce({ superadmin: true }); expect(await isSuperadmin()).toBe(true);
  getCurrentSession.mockResolvedValueOnce({ superadmin: false }); expect(await isSuperadmin()).toBe(false);
  getCurrentSession.mockResolvedValueOnce(null); expect(await isSuperadmin()).toBe(false);
});
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.** `lib/auth.ts`:

```ts
export const getCurrentSession = cache(async () => {
  const s = await auth.api.getSession({ headers: await headers() });
  if (!s) return null;
  return { ...s, superadmin: isSuperadminUser(s.user as Parameters<typeof isSuperadminUser>[0]) };
});
export type ConsoleSession = NonNullable<Awaited<ReturnType<typeof getCurrentSession>>>;
```

(import `isSuperadminUser` from `./superadmin`.) `getActiveOrgIdForSession`: unchanged logic (a superadmin's `activeOrganizationId` may be any org; the switcher writes it in Task 9).

`lib/require-org-admin.ts`:

```ts
export async function requireOrgAdmin(nextPath = "/console/settings") {
  const session = await requireSession(`/signin?next=${nextPath}`);
  const orgId = await getActiveOrgIdForSession(session);
  if (!orgId) throw new Error("No active organization for this user.");
  if (session.superadmin) {
    const { rows } = await pool.query<{ name: string }>(`SELECT name FROM organizations WHERE id = $1`, [orgId]);
    if (!rows[0]) throw new Error("Organization not found.");
    return { orgId, userId: session.user.id, orgName: rows[0].name, session, superadmin: true as const };
  }
  … existing query …
  return { orgId, userId: session.user.id, orgName: rows[0].org_name, session, superadmin: false as const };
}

export async function getOrgMemberRole(orgId: string, userId: string, superadmin = false): Promise<string | null> {
  if (superadmin) return "owner";
  … existing …
}
```

Update every `getOrgMemberRole(orgId, session.user.id)` caller (grep) to pass `session.superadmin`. `lib/require-superadmin.ts`:

```ts
export async function isSuperadmin(): Promise<boolean> {
  return (await getCurrentSession())?.superadmin === true;
}
```

- [ ] **Step 4: Run** `npx vitest run lib app` → PASS; `npx eslint lib app`.
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat(auth): superadmin session flag; treated as owner by role checks"`

---

### Task 8: `superadmin` claim in the console JWT (website)

**Files:**
- Modify: `lib/auth.ts:354-363` (`definePayload`)
- Test: `lib/__tests__/jwt-payload.test.ts`

- [ ] **Step 1: Failing test.** Export the payload function so it is testable: in `lib/auth.ts` define `export function consoleJwtPayload(session: { session: { activeOrganizationId?: string | null }; user: { email: string; emailVerified: boolean; lastLoginMethod?: string | null } })` and use it in `definePayload`. Test:

```ts
import { expect, it, vi } from "vitest";
vi.mock("next/headers", () => ({ headers: async () => new Headers() }));
import { consoleJwtPayload } from "@/lib/auth";

it("adds superadmin: true only for staff sessions", () => {
  const staff = { session: { activeOrganizationId: "o" }, user: { email: "r@hail.so", emailVerified: true, lastLoginMethod: "google" } };
  const customer = { session: { activeOrganizationId: "o" }, user: { email: "a@b.com", emailVerified: true, lastLoginMethod: "google" } };
  expect(consoleJwtPayload(staff)).toEqual({ activeOrganizationId: "o", superadmin: true });
  expect(consoleJwtPayload(customer)).toEqual({ activeOrganizationId: "o" });
});
```

If importing `@/lib/auth` in a test is too heavy (it builds the auth instance), move `consoleJwtPayload` to `lib/superadmin.ts` and import from there; `lib/auth.ts` imports it.

- [ ] **Step 2: Run** → FAIL. **Step 3:** implement:

```ts
export function consoleJwtPayload(session: …) {
  const base: { activeOrganizationId: string | null; superadmin?: true } = { activeOrganizationId: session.session.activeOrganizationId ?? null };
  if (isSuperadminUser(session.user)) base.superadmin = true;
  return base;
}
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `git commit -am "feat(auth): console API token carries superadmin: true for staff"`

---

### Task 9: Org switcher actions (website)

**Files:**
- Create: `app/console/superadmin-actions.ts` (`"use server"`)
- Test: `app/console/__tests__/superadmin-actions.test.ts`

**Interfaces:**
- Produces: `searchOrgsAction(q: string): Promise<{ id: string; name: string; ownerEmail: string | null; createdAt: string }[]>` (max 20, superadmin only, else throws); `switchOrgAction(orgId: string): Promise<{ ok: true } | { ok: false; error: string }>`; `switchBackAction(): Promise<{ ok: true }>` (sets the active org to the user's oldest membership).

- [ ] **Step 1: Failing test**:

```ts
import { beforeEach, expect, it, vi } from "vitest";
const query = vi.fn();
vi.mock("@/lib/db", () => ({ pool: { query: (...a: unknown[]) => query(...a) } }));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
const session = { user: { id: "u1" }, session: { id: "s1", activeOrganizationId: "own" }, superadmin: false };
vi.mock("@/lib/auth", () => ({ requireSession: vi.fn(async () => session), getOrgIdForUser: vi.fn(async () => "own") }));
import { searchOrgsAction, switchBackAction, switchOrgAction } from "@/app/console/superadmin-actions";

beforeEach(() => { query.mockReset(); session.superadmin = false; });

it("refuses non-superadmins without touching the database", async () => {
  await expect(searchOrgsAction("acme")).rejects.toThrow(/superadmin/i);
  expect(await switchOrgAction("org-x")).toEqual({ ok: false, error: "Not allowed." });
  expect(query).not.toHaveBeenCalled();
});
it("searches by name, owner email or id and switches the session's org", async () => {
  session.superadmin = true;
  query.mockResolvedValueOnce({ rows: [{ id: "org-x", name: "Acme", owner_email: "o@acme.com", created_at: new Date("2026-01-01") }] });
  expect(await searchOrgsAction("acme")).toEqual([{ id: "org-x", name: "Acme", ownerEmail: "o@acme.com", createdAt: "2026-01-01T00:00:00.000Z" }]);
  query.mockResolvedValueOnce({ rows: [{ id: "org-x" }] }); // org exists
  query.mockResolvedValueOnce({ rowCount: 1 });               // update session
  expect(await switchOrgAction("org-x")).toEqual({ ok: true });
  expect(query.mock.calls[2][0]).toMatch(/UPDATE sessions SET active_organization_id/);
  expect(query.mock.calls[2][1]).toEqual(["org-x", "s1"]);
});
it("switch back returns to the own org", async () => {
  session.superadmin = true;
  query.mockResolvedValueOnce({ rowCount: 1 });
  expect(await switchBackAction()).toEqual({ ok: true });
  expect(query.mock.calls[0][1]).toEqual(["own", "s1"]);
});
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `app/console/superadmin-actions.ts`:

```ts
"use server";

import { revalidatePath } from "next/cache";
import { getOrgIdForUser, requireSession } from "@/lib/auth";
import { pool } from "@/lib/db";

async function requireSuperadminSession() {
  const session = await requireSession("/signin?next=/console");
  if (!session.superadmin) throw new Error("superadmin only");
  return session;
}

export async function searchOrgsAction(q: string) {
  await requireSuperadminSession();
  const term = `%${q.trim()}%`;
  const { rows } = await pool.query<{ id: string; name: string; owner_email: string | null; created_at: Date }>(
    `SELECT o.id, o.name, o.created_at,
            (SELECT u.email FROM members m JOIN users u ON u.id = m.user_id
              WHERE m.organization_id = o.id AND m.role = 'owner' ORDER BY m.created_at LIMIT 1) AS owner_email
       FROM organizations o
      WHERE o.name ILIKE $1 OR o.id::text = $2
         OR EXISTS (SELECT 1 FROM members m JOIN users u ON u.id = m.user_id WHERE m.organization_id = o.id AND u.email ILIKE $1)
      ORDER BY o.created_at DESC
      LIMIT 20`,
    [term, q.trim()],
  );
  return rows.map((r) => ({ id: r.id, name: r.name, ownerEmail: r.owner_email, createdAt: r.created_at.toISOString() }));
}

async function setActiveOrg(sessionId: string, orgId: string) {
  await pool.query(`UPDATE sessions SET active_organization_id = $1 WHERE id = $2`, [orgId, sessionId]);
  revalidatePath("/console", "layout");
}

export async function switchOrgAction(orgId: string): Promise<{ ok: true } | { ok: false; error: string }> {
  const session = await requireSession("/signin?next=/console");
  if (!session.superadmin) return { ok: false, error: "Not allowed." };
  const { rows } = await pool.query<{ id: string }>(`SELECT id FROM organizations WHERE id = $1`, [orgId]);
  if (!rows[0]) return { ok: false, error: "Organization not found." };
  await setActiveOrg(session.session.id, orgId);
  return { ok: true };
}

export async function switchBackAction(): Promise<{ ok: true }> {
  const session = await requireSuperadminSession();
  const own = await getOrgIdForUser(session.user.id);
  if (own) await setActiveOrg(session.session.id, own);
  return { ok: true };
}
```

- [ ] **Step 4: Run** → PASS; eslint. **Step 5: Commit** `git add -A && git commit -m "feat(console): superadmin org search and switch actions"`

---

### Task 10: Org switcher UI and "Hail support" banner (website)

**Files:**
- Create: `app/console/OrgSwitcher.tsx` (client)
- Modify: `app/console/layout.tsx:44-60` (top bar), `app/console/console.css` (`.console-topbar` block near line 245)
- Test: `app/console/__tests__/OrgSwitcher.test.tsx`

- [ ] **Step 1: Failing test** (jsdom, like `app/console/numbers/__tests__/*.test.tsx`):

```tsx
/** @vitest-environment jsdom */
import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
const search = vi.fn(async () => [{ id: "org-x", name: "Acme", ownerEmail: "o@acme.com", createdAt: "2026-01-01T00:00:00.000Z" }]);
const switchOrg = vi.fn(async () => ({ ok: true }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("../superadmin-actions", () => ({ searchOrgsAction: (q: string) => search(q), switchOrgAction: (id: string) => switchOrg(id), switchBackAction: vi.fn() }));
import { OrgSwitcher } from "../OrgSwitcher";
afterEach(cleanup);

it("searches and switches", async () => {
  render(<OrgSwitcher currentOrgName="My org" viewingForeign={false} />);
  await userEvent.type(screen.getByRole("searchbox", { name: /find an organization/i }), "acme");
  await userEvent.click(await screen.findByRole("option", { name: /Acme/ }));
  expect(switchOrg).toHaveBeenCalledWith("org-x");
});
it("shows the Hail support banner with a way back", () => {
  render(<OrgSwitcher currentOrgName="Acme" viewingForeign={true} />);
  expect(screen.getByRole("status").textContent).toMatch(/Viewing Acme as Hail support/);
  expect(screen.getByRole("button", { name: /back to my org/i })).toBeTruthy();
});
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `OrgSwitcher.tsx`: a `<form role="search">` with `<input type="search" aria-label="Find an organization">`, debounced (250 ms) call to `searchOrgsAction`, results as `<ul role="listbox">` of `<li role="option">` buttons (name, owner email, short id); click → `switchOrgAction(id)` then `router.refresh()`. When `viewingForeign`, render `<div role="status" className="c-superadmin-banner">Viewing {currentOrgName} as Hail support <button onClick={switchBackAction → refresh}>Back to my org</button></div>`. Layout: in `ConsoleLayout`, when `session.superadmin`, compute `orgId = await getActiveOrgIdForSession(session)`, `orgName` (SELECT name), `viewingForeign = !(await getOrgMemberRole(orgId, user.id))`, and render `<OrgSwitcher …/>` inside `.console-topbar` after `.crumb`. CSS: `.c-superadmin-banner { background: var(--c-tape); border: 2px solid var(--c-ink); padding: 6px 10px; font-size: 13px; display: flex; gap: 10px; align-items: center; }`, `.c-org-switcher { position: relative; margin-left: auto; }`, results popover on `var(--c-paper)` with a 2px ink border, `max-height: 320px; overflow-y: auto`.

- [ ] **Step 4: Run** `npx vitest run app/console` → PASS; eslint. **Step 5: Commit** `git add -A && git commit -m "feat(console): org switcher and Hail support banner for superadmins"`

---

### Task 11: "Hail support" labels and website PR

**Files:**
- Modify: `app/console/admin/verifications/ReviewList.tsx` (Organization column: show org name via a name map fetched in `page.tsx` with one `SELECT id, name FROM organizations WHERE id = ANY($1)`), and any console view that prints an actor from audit data — grep `actor_kind`; if none, only the review list changes.
- Docs: `docs/superpowers/specs/2026-09-25-superadmin-design.md` is in the hail repo; add a short "Superadmin" section to the website README's console notes if one exists (grep `## Console`).

- [ ] Full checks: `npx vitest run`, `npx eslint app lib`, `npx tsc --noEmit` (pre-existing test-file errors excluded).
- [ ] `git push -u origin feat/superadmin`; `gh pr create -R r13i/hail-website --base feat/console-verification` (or `master` once #94 is merged) with: who qualifies, that no member rows are created, the switcher, "needs hail-hq/hail superadmin PR deployed first", and the 2FA follow-up.
