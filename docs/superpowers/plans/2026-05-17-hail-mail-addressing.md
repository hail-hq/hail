# Hail-Mail Addressing & Self-Host/Managed Resolution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the opaque `org-<8hex>@base` hail-mail format with a human-readable `<user>+<org>@base` address whose prefixes are visible and configurable — via env vars on self-hosted Hail, via the website console on managed Hail.

**Architecture:** Two prefix columns on `sender_domains` (`local_prefix_user`, `local_prefix_org`). For `kind='hail_mail'` rows both are required and the `domain` column stores the computed `<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>` address. Defaults come from `HAIL_MAIL_DEFAULT_USER_PREFIX` + `HAIL_MAIL_DEFAULT_ORG_PREFIX` env vars at creation time; subsequent edits go through a new `PATCH /sender-domains/{id}` endpoint, which is what the hail-website console writes to.

**Tech Stack:** Same as the rest of `api/`: FastAPI + Pydantic v2 + SQLAlchemy + Alembic + pytest. No new dependencies.

**Scope note:** The existing email migration (`0005_emails.py`) and routes have not yet been committed. This plan modifies them in place rather than introducing a `0006` follow-up — a clean schema is cheaper than a deprecation cycle no production database will see.

---

## Status

| Task | Description                                          | Status     |
| ---- | ---------------------------------------------------- | ---------- |
| 1    | Settings: add user/org default prefix env vars       | ⏳ Pending |
| 2    | Schema: add prefix columns + validation regex        | ⏳ Pending |
| 3    | Model: add prefix columns + CHECK for hail_mail rows | ⏳ Pending |
| 4    | Migration 0005: extend with prefix columns + CHECK   | ⏳ Pending |
| 5    | POST /sender-domains: compute address from prefixes  | ⏳ Pending |
| 6    | PATCH /sender-domains/{id}: edit prefixes            | ⏳ Pending |
| 7    | Tests: cover env-var defaults, PATCH, validation     | ⏳ Pending |
| 8    | Docs: architecture.md + setup/aws-ses.md sections    | ⏳ Pending |
| 9    | Regenerate openapi.yaml + final lint/test            | ⏳ Pending |

---

## Background — why two flavors

| Surface         | Auth model                                                                       | "User" exists?              | "Org" exists?                                   | Prefix source                                                                                              |
| --------------- | -------------------------------------------------------------------------------- | --------------------------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| **Self-hosted** | Shared `HAIL_API_KEY`; principal lands in sentinel "Self-hosted" org (nil UUID). | No. There's the operator.   | No real org — single sentinel.                  | `HAIL_MAIL_DEFAULT_USER_PREFIX` + `HAIL_MAIL_DEFAULT_ORG_PREFIX` from `.env`.                              |
| **Managed**     | Per-user `hl_live_*` keys via the website's auth backend.                        | Yes (auth backend `users`). | Yes (auth backend `organizations` + `members`). | Defaults from env vars (same vars, set at deploy time); editable per-org via the console (PATCH endpoint). |

**Why not per-user rows?** The website's auth tables are owned by a separate migration history; hail/api would have to read across that boundary to materialize per-user prefixes. For v1 the prefix is org-shared — any user in the org sending mail uses the org's one hail-mail address. The "user" portion is named that way because the operator (self-host) or the org admin (managed) typically maps it to a real human or team alias.

Multi-user-per-org with distinct addresses is v2; the schema is forward-compatible (the columns sit on `sender_domains`, so adding a `user_id` later is a column-add migration, not a redesign).

---

## File Structure

**Modify**

- `core/hailhq/core/config.py` — two new `Settings` fields.
- `core/hailhq/core/models.py` — two prefix columns on `SenderDomain` + CHECK constraint.
- `core/hailhq/core/schemas.py` — new `LOCAL_PREFIX` regex; `SenderDomainCreate` / response / new `SenderDomainPatch` schema; computed full address in response.
- `api/hailhq/api/routes/sender_domains.py` — wire defaults from env; add `PATCH` handler.
- `api/migrations/versions/0005_emails.py` — bake the two columns + CHECK into the initial table create.
- `api/tests/conftest.py` — already exports `email_mock`; nothing to change unless a new fixture is needed.
- `api/tests/test_sender_domains_api.py` — add PATCH + env-default coverage; update existing hail-mail assertions.
- `api/tests/test_emails_api.py` — replace `endswith("@mail.hail.so")` assertion with the new format check.
- `.env.example` — document the two new env vars.
- `docs/architecture.md` — add "Outbound email" section.
- `docs/setup/aws-ses.md` — replace the hail-mail subsection with the configurable-prefixes flow.

**Create**

- (none — all changes land in existing files)

---

## Task 1 — Settings env vars

**Files:**

- Modify: `core/hailhq/core/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Add two fields to `Settings`** (after the existing `hail_mail_base_domain`):

```python
    # Hail-mail address prefixes. The full hail-mail sender is
    # ``<user_prefix>+<org_prefix>@<HAIL_MAIL_BASE_DOMAIN>``. These are
    # the defaults baked into newly minted rows when the caller doesn't
    # pass them on POST /sender-domains. On managed Hail the org admin
    # then overrides them through the console (PATCH); on self-hosted
    # Hail the env vars ARE the configuration.
    hail_mail_default_user_prefix: str = ""
    hail_mail_default_org_prefix: str = ""
```

- [ ] **Step 2: Add the same keys to `.env.example`** under the existing `HAIL_MAIL_BASE_DOMAIN` block:

```bash
# Default prefixes for newly minted hail-mail rows. The full sender is
# <user>+<org>@<HAIL_MAIL_BASE_DOMAIN> — e.g. alice+acme@mail.hail.so.
# Both must match ^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$ (1–20 chars,
# lowercase alphanumeric + hyphens, no leading/trailing hyphens).
#
# Self-host: these env vars ARE the addressing — set them once and
# every hail-mail row picks them up. Managed: defaults at row-create
# time; org admins override via the console (PATCH /sender-domains/{id}).
HAIL_MAIL_DEFAULT_USER_PREFIX=
HAIL_MAIL_DEFAULT_ORG_PREFIX=
```

- [ ] **Step 3: Verify with a smoke import**

Run: `uv run python -c "from hailhq.core.config import settings; print(settings.hail_mail_default_user_prefix, settings.hail_mail_default_org_prefix)"`
Expected: prints two empty strings.

---

## Task 2 — Schema + validator

**Files:**

- Modify: `core/hailhq/core/schemas.py`

- [ ] **Step 1: Add the validation regex next to `EMAIL_ADDR` / `DOMAIN_NAME`:**

```python
# Local-part prefix for hail-mail: lowercase alphanumeric + hyphens,
# 1–20 chars, no leading/trailing hyphen. Each side of the ``+`` is
# validated independently; both fit in the RFC-5321 64-char local-part
# budget with room to spare.
LOCAL_PREFIX = re.compile(r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$|^[a-z0-9]$")
```

- [ ] **Step 2: Update `SenderDomainCreate`** to accept optional prefix fields for hail_mail mode:

```python
class SenderDomainCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: SenderDomainKind
    domain: str | None = None
    # Only valid when kind='hail_mail'. Each prefix must match LOCAL_PREFIX.
    # When omitted, the server falls back to HAIL_MAIL_DEFAULT_USER_PREFIX /
    # HAIL_MAIL_DEFAULT_ORG_PREFIX from settings.
    local_prefix_user: str | None = None
    local_prefix_org: str | None = None

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if not DOMAIN_NAME.match(v):
            raise ValueError(
                "must be a valid DNS domain (e.g. 'acme.com'); no schemes or paths"
            )
        return v

    @field_validator("local_prefix_user", "local_prefix_org")
    @classmethod
    def _validate_local_prefix(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if not LOCAL_PREFIX.match(v):
            raise ValueError(
                "must be 1–20 chars of lowercase a-z, 0-9, or '-', "
                "no leading or trailing '-'"
            )
        return v

    @model_validator(mode="after")
    def _kind_field_consistency(self):
        if self.kind == "custom":
            if not self.domain:
                raise ValueError("domain is required when kind='custom'")
            if self.local_prefix_user is not None or self.local_prefix_org is not None:
                raise ValueError(
                    "local_prefix_user/local_prefix_org are only valid when kind='hail_mail'"
                )
        if self.kind == "hail_mail" and self.domain is not None:
            raise ValueError(
                "domain must be omitted when kind='hail_mail'; the server assigns the address"
            )
        return self
```

- [ ] **Step 3: Add `SenderDomainPatch` for the new PATCH endpoint:**

```python
class SenderDomainPatch(BaseModel):
    """Body for PATCH /sender-domains/{id}.

    Only the user/org prefix pair is mutable today — DNS verification
    state, DKIM records, and provider metadata are server-managed and
    never editable from outside.
    """

    model_config = ConfigDict(extra="forbid")

    local_prefix_user: str | None = None
    local_prefix_org: str | None = None

    @field_validator("local_prefix_user", "local_prefix_org")
    @classmethod
    def _validate(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if not LOCAL_PREFIX.match(v):
            raise ValueError(
                "must be 1–20 chars of lowercase a-z, 0-9, or '-', "
                "no leading or trailing '-'"
            )
        return v

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if self.local_prefix_user is None and self.local_prefix_org is None:
            raise ValueError("at least one of local_prefix_user / local_prefix_org must be set")
        return self
```

- [ ] **Step 4: Surface the prefixes on `SenderDomainResponse`:**

```python
class SenderDomainResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    kind: SenderDomainKind
    domain: str
    local_prefix_user: str | None
    local_prefix_org: str | None
    verification_status: SenderDomainVerificationStatus
    dkim_records: list[DkimRecordSchema]
    mail_from_domain: str | None
    provider: str
    verified_at: datetime | None
    created_at: datetime
    updated_at: datetime
```

---

## Task 3 — Model columns + CHECK

**Files:**

- Modify: `core/hailhq/core/models.py`

- [ ] **Step 1: Add the two columns to `SenderDomain` (just before `verified_at`):**

```python
    # Both NULL for kind='custom'; both required and validated for
    # kind='hail_mail'. The full sending address (`domain` column) is
    # computed at write time and kept in sync — these two columns are
    # the source of truth, `domain` is the convenience materialization.
    local_prefix_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_prefix_org: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 2: Add the CHECK constraint inside `__table_args__`:**

```python
        CheckConstraint(
            "(kind = 'hail_mail' AND local_prefix_user IS NOT NULL "
            "AND local_prefix_org IS NOT NULL) OR kind = 'custom'",
            name="sender_domains_hail_mail_prefixes_required",
        ),
```

---

## Task 4 — Migration update

**Files:**

- Modify: `api/migrations/versions/0005_emails.py`

- [ ] **Step 1: Insert the two columns into the `sender_domains` CREATE TABLE:**

```sql
CREATE TABLE sender_domains (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id      UUID NOT NULL,
  kind                 TEXT NOT NULL CHECK (kind IN ('hail_mail','custom')),
  domain               TEXT NOT NULL,
  local_prefix_user    TEXT,
  local_prefix_org     TEXT,
  verification_status  TEXT NOT NULL DEFAULT 'pending'
                       CHECK (verification_status IN ('pending','verified','failed')),
  dkim_records         JSONB NOT NULL DEFAULT '[]'::jsonb,
  mail_from_domain     TEXT,
  provider             TEXT NOT NULL DEFAULT 'ses',
  provider_resource_id TEXT,
  verified_at          TIMESTAMPTZ,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT sender_domains_org_domain_unique UNIQUE (organization_id, domain),
  CONSTRAINT sender_domains_hail_mail_prefixes_required CHECK (
    (kind = 'hail_mail' AND local_prefix_user IS NOT NULL AND local_prefix_org IS NOT NULL)
    OR kind = 'custom'
  )
);
```

- [ ] **Step 2: Verify the migration applies cleanly**

Run: `cd api && DATABASE_URL=postgresql://test@localhost/test alembic upgrade head`
Expected: no errors. (Use a testcontainers Postgres if there is no local DB.)

---

## Task 5 — POST /sender-domains: address composition

**Files:**

- Modify: `api/hailhq/api/routes/sender_domains.py`

- [ ] **Step 1: Replace `_mint_hail_mail_address`** so it consumes the validated prefixes and falls back to settings defaults:

```python
def _resolve_hail_mail_prefixes(
    body_user: str | None,
    body_org: str | None,
) -> tuple[str, str]:
    """Pick prefixes from the body or fall back to env defaults.

    Returns ``(user_prefix, org_prefix)`` already validated by Pydantic.
    Raises HTTPException 503 if a prefix is required but neither the
    body nor the env supplied one — same shape as the missing-base-domain
    response so the operator gets a single failure mode to debug.
    """
    user = body_user or settings.hail_mail_default_user_prefix
    org = body_org or settings.hail_mail_default_org_prefix
    missing = []
    if not user:
        missing.append("local_prefix_user (or HAIL_MAIL_DEFAULT_USER_PREFIX)")
    if not org:
        missing.append("local_prefix_org (or HAIL_MAIL_DEFAULT_ORG_PREFIX)")
    if missing:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "hail-mail prefixes are not configured: missing "
                + ", ".join(missing)
            ),
        )
    return user, org


def _compose_hail_mail_address(user_prefix: str, org_prefix: str) -> str:
    base = settings.hail_mail_base_domain
    if not base:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "hail-mail is not configured on this server; set "
                "HAIL_MAIL_BASE_DOMAIN to enable, or register a custom domain "
                "with kind='custom'"
            ),
        )
    return f"{user_prefix}+{org_prefix}@{base}"
```

- [ ] **Step 2: Update the POST handler's hail_mail branch:**

```python
    if body.kind == "hail_mail":
        user_prefix, org_prefix = _resolve_hail_mail_prefixes(
            body.local_prefix_user, body.local_prefix_org
        )
        address = _compose_hail_mail_address(user_prefix, org_prefix)
        sd = SenderDomain(
            organization_id=principal.organization_id,
            kind="hail_mail",
            domain=address,
            local_prefix_user=user_prefix,
            local_prefix_org=org_prefix,
            verification_status="verified",
            dkim_records=[],
            mail_from_domain=None,
            provider="ses",
            provider_resource_id=settings.hail_mail_base_domain or None,
            verified_at=datetime.now(timezone.utc),
        )
        db.add(sd)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail=f"hail-mail address {address!r} is already registered for this organization",
            ) from exc
        await db.refresh(sd)
        response.headers["Location"] = f"/sender-domains/{sd.id}"
        return SenderDomainResponse.model_validate(sd)
```

- [ ] **Step 3: Update the auto-mint path in `emails.py`** (`_resolve_sender`'s last-resort fallback) so it uses the env-var defaults the same way the explicit POST does. Lift `_compose_hail_mail_address` + `_resolve_hail_mail_prefixes` into a small helper module (or keep importing from `sender_domains` — the existing test fixtures already do).

```python
    address_user, address_org = _resolve_hail_mail_prefixes(None, None)
    address = _compose_hail_mail_address(address_user, address_org)
    sd = SenderDomain(
        organization_id=organization_id,
        kind="hail_mail",
        domain=address,
        local_prefix_user=address_user,
        local_prefix_org=address_org,
        verification_status="verified",
        dkim_records=[],
        mail_from_domain=None,
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
```

---

## Task 6 — PATCH /sender-domains/{id}

**Files:**

- Modify: `api/hailhq/api/routes/sender_domains.py`

- [ ] **Step 1: Add the route** (after the GET-by-id handler):

```python
@router.patch("/{domain_id}", response_model=SenderDomainResponse)
async def patch_sender_domain(
    domain_id: UUID,
    body: SenderDomainPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SenderDomainResponse:
    """Edit the user/org prefix on a hail-mail row.

    Today only ``kind='hail_mail'`` rows accept edits — there's nothing
    in a custom-domain row that the tenant should be free to mutate via
    this surface (DNS, DKIM, and verification state are all
    provider-driven). Hitting this endpoint on a custom row returns 422.
    """
    stmt = select(SenderDomain).where(
        SenderDomain.id == domain_id,
        SenderDomain.organization_id == principal.organization_id,
    )
    sd = (await db.execute(stmt)).scalar_one_or_none()
    if sd is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="sender domain not found",
        )

    if sd.kind != "hail_mail":
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="prefix edits are only allowed on kind='hail_mail' rows",
        )

    new_user = body.local_prefix_user or sd.local_prefix_user
    new_org = body.local_prefix_org or sd.local_prefix_org
    new_address = _compose_hail_mail_address(new_user, new_org)

    try:
        await db.execute(
            update(SenderDomain)
            .where(SenderDomain.id == sd.id)
            .values(
                local_prefix_user=new_user,
                local_prefix_org=new_org,
                domain=new_address,
            )
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"hail-mail address {new_address!r} is already registered for this organization",
        ) from exc

    await db.refresh(sd)
    return SenderDomainResponse.model_validate(sd)
```

---

## Task 7 — Tests

**Files:**

- Modify: `api/tests/test_sender_domains_api.py`
- Modify: `api/tests/test_emails_api.py`

- [ ] **Step 1: Replace the existing hail-mail POST test** with the prefix-aware shape:

```python
async def test_post_hail_mail_uses_explicit_prefixes(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain = org_and_key
    resp = await client.post(
        "/sender-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "alice",
            "local_prefix_org": "acme",
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["domain"] == "alice+acme@mail.hail.so"
    assert body["local_prefix_user"] == "alice"
    assert body["local_prefix_org"] == "acme"
    assert body["verification_status"] == "verified"
    assert body["dkim_records"] == []


async def test_post_hail_mail_falls_back_to_env_defaults(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "admin")
    monkeypatch.setattr(settings, "hail_mail_default_org_prefix", "selfhost")
    _, _, plain = org_and_key
    resp = await client.post(
        "/sender-domains",
        json={"kind": "hail_mail"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["domain"] == "admin+selfhost@mail.hail.so"


async def test_post_hail_mail_503_when_prefix_missing_and_no_default(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "")
    monkeypatch.setattr(settings, "hail_mail_default_org_prefix", "")
    _, _, plain = org_and_key
    resp = await client.post(
        "/sender-domains",
        json={"kind": "hail_mail"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 503
    assert "local_prefix" in resp.text


async def test_post_hail_mail_rejects_invalid_prefix(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain = org_and_key
    resp = await client.post(
        "/sender-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "Alice!",  # invalid: uppercase + '!'
            "local_prefix_org": "acme",
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422


async def test_patch_hail_mail_updates_prefixes_and_domain(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/sender-domains",
        json={"kind": "hail_mail", "local_prefix_user": "u1", "local_prefix_org": "o1"},
        headers=headers,
    )
    domain_id = created.json()["id"]

    resp = await client.patch(
        f"/sender-domains/{domain_id}",
        json={"local_prefix_user": "alice", "local_prefix_org": "acme"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["domain"] == "alice+acme@mail.hail.so"


async def test_patch_on_custom_domain_returns_422(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/sender-domains", json={"kind": "custom", "domain": "acme.com"}, headers=headers
    )
    resp = await client.patch(
        f"/sender-domains/{created.json()['id']}",
        json={"local_prefix_user": "alice"},
        headers=headers,
    )
    assert resp.status_code == 422
```

- [ ] **Step 2: Drop or replace the now-stale `test_post_hail_mail_mints_address` and `test_post_hail_mail_rejects_domain_in_body`** with the variants above.

- [ ] **Step 3: Update the auto-mint hail-mail test in `test_emails_api.py`:**

```python
async def test_post_emails_auto_mints_hail_mail_when_no_sender_exists(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
    async_session: AsyncSession,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "admin")
    monkeypatch.setattr(settings, "hail_mail_default_org_prefix", "selfhost")
    _, _, plain = org_and_key
    resp = await client.post(
        "/emails",
        json={"to": ["alice@example.com"], "subject": "hi", "body_text": "body"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["from_address"] == "admin+selfhost@mail.hail.so"
```

- [ ] **Step 4: Run the test suite**

Run: `cd api && uv run pytest -q`
Expected: all green.

---

## Task 8 — Docs

**Files:**

- Modify: `docs/architecture.md`
- Modify: `docs/setup/aws-ses.md`

- [ ] **Step 1: Append "Outbound email" section to `architecture.md`** describing:
  - Two flavors (hail-mail, custom) and their lifecycle.
  - Self-hosted vs managed table comparing auth, user/org existence, prefix source.
  - The send-time resolution order: explicit `from` → verified org-owned domain → auto-mint hail-mail.

- [ ] **Step 2: Replace the hail-mail section in `setup/aws-ses.md`** with:
  - A bare-domain SES verification step (unchanged from current copy).
  - The new env vars + their validation regex.
  - A worked example: `mail.hail.so` + `admin` + `selfhost` → `admin+selfhost@mail.hail.so`.
  - A short "managed users edit through the console" callout that resolves to the PATCH endpoint.

---

## Task 9 — Regenerate openapi.yaml + final pass

- [ ] **Step 1: Re-dump the spec**

```bash
cd /Users/r/playground/hail
uv run python -c "
import yaml
from hailhq.api.main import app

class I(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)

with open('openapi/openapi.yaml', 'w') as f:
    yaml.dump(app.openapi(), f, Dumper=I, sort_keys=False, default_flow_style=False, width=88)
"
```

- [ ] **Step 2: Lint + format + test all three packages**

```bash
uv run ruff check api core
uv run ruff format --check api core
(cd api && uv run pytest -q) && (cd core && uv run pytest -q) && (cd voicebot && uv run pytest -q)
```

Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat(email): hail-mail addressing with configurable prefixes

…
"
```

---

## Self-Review Notes

- **Spec coverage:** the user asked for (1) self-hosted-vs-managed documentation — Task 8 covers it across two doc files; (2) `<user>+<org>@base` format with limits + validation — Task 2 regex + Task 4 CHECK constraint; (3) visible/configurable — Tasks 5 and 6 add env-var fallback and the PATCH endpoint; (4) test instructions — separate from this plan, included in the chat reply.
- **Placeholder scan:** none — every step has either a code block or an explicit command.
- **Type consistency:** `local_prefix_user` / `local_prefix_org` named identically across schema, model, migration, route, response, tests.
