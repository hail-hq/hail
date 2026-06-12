# Inbound Email Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Receive email at Hail-controlled hail-mail addresses, persist it alongside outbound, and let tenants react via forwarding and/or webhooks. Source spec: `docs/superpowers/specs/2026-06-06-inbound-email-design.md`.

**Architecture:** SES Receipt Rule → S3 (raw MIME) + Lambda (notification bridge) → HMAC-signed POST to `api`'s `/internal/ses-events` → MIME parse → org route → persist (`Email` row + `email_attachments` rows) → per-domain forward and/or webhook + org-wide webhook subscription fan-out. Schema is reshaped first (`sender_domains` → `email_domains` rename, `Email.direction` column), then behavior, then operator provisioning (Terraform module + Lambda).

**Tech stack:** Python 3.11 + FastAPI + SQLAlchemy 2.0 async + Alembic + Pydantic v2 for the API/core; AWS SES + S3 + SNS + Lambda for ingress; Terraform for provisioning; Go (Cobra) for the `hail` CLI; pytest for tests.

---

## File map

**Migrations** (Alembic, in `api/migrations/versions/`):

- `0006_email_domains_rename.py` — rename `sender_domains` → `email_domains`, propagate FK + index names.
- `0007_email_inbound_schema.py` — `Email.direction` + inbound columns, `email_attachments`, `email_domains` action columns, unique idempotency index, status check expansion.
- `0008_webhook_subscriptions.py` — `webhook_subscriptions` and `webhook_deliveries`.

**Core models** (`core/hailhq/core/models.py`):

- Rename `SenderDomain` → `EmailDomain`. Add `EmailAttachment`, `WebhookSubscription`, `WebhookDelivery`. Extend `Email` with inbound columns.

**Core schemas** (`core/hailhq/core/schemas.py`):

- Rename `SenderDomain*` → `EmailDomain*` pydantic schemas. Add `EmailAttachmentResponse`, `InboundEmailFields`, `WebhookSubscriptionCreate/Response/List`, `WebhookDeliveryResponse`.

**Inbound provider** (`core/hailhq/core/providers/email/inbound/`):

- `base.py` — `InboundProvider` ABC + `InboundMessage` dataclass.
- `ses.py` — `SesInboundProvider` (parses Lambda's JSON, fetches raw MIME from S3, HMAC verify).
- `smtp.py` — stub raising `NotImplementedError`.
- `__init__.py` — re-exports.

**MIME and routing helpers** (`core/hailhq/core/`):

- `email_mime.py` — stdlib-`email` MIME parser, attachment extraction.
- `email_routing.py` — hail-mail local-part router (`alice+acme@mail.hail.so` → org slug).
- `email_forwarding.py` — header-rewrite outbound builder + loop guards.
- `webhooks.py` — HMAC signing, payload builder.
- `webhook_worker.py` — async background polling worker.
- `s3_inbound.py` — S3 fetch + presign helper for the inbound bucket.

**API routes** (`api/hailhq/api/routes/`):

- Rename `sender_domains.py` → `email_domains.py`. Add `inbound_enabled`/`forward_to`/`webhook_url` PATCH support + rotate-secret endpoint.
- Extend `emails.py`: `?direction` filter, `GET /emails/{id}/raw`, `GET /emails/{id}/attachments/{aid}`.
- New `webhooks.py` — CRUD for subscriptions + deliveries listing + redeliver.
- New `internal/ses_events.py` — Lambda → API ingest endpoint.

**Wiring**:

- `api/hailhq/api/main.py` — mount new routers; start webhook worker on lifespan.
- `core/hailhq/core/config.py` — new env vars.

**Infra** (`infra/`):

- `terraform/{versions,variables,main,s3_inbound,ses_inbound,lambda_ingest,outputs}.tf`, `hail.tfvars.example`.
- `ses-ingest-lambda/handler.py`, `README.md`, `test_handler.py`.

**CLI** (`cli/internal/cmd/`):

- Rename `sender_domain.go` / `sender_domain_test.go` → `email_domain.go` / `email_domain_test.go`. Add inbound subcommands.
- New `webhooks.go`, `webhooks_test.go`.
- Update `email.go` for `--direction`.
- Regenerate `cli/internal/client/client.gen.go` from updated OpenAPI.

**Docs** (`docs/`):

- `setup/aws-ses.md` — new Inbound section.
- `setup/smtp-inbound.md` — placeholder for the deferred provider.
- `architecture.md` — append inbound block.
- `.env.example` — new vars.

**OpenAPI** (`openapi/openapi.yaml`):

- Regenerate after each route-touching phase. Final pass at the end of Phase 10.

---

## Phase 0 — Pre-flight

### Task 0.1: Confirm clean baseline

**Files:** none.

- [ ] **Step 1: Verify migrations head**

Run: `cd api && uv run alembic current`
Expected: `0005 (head)`.

- [ ] **Step 2: Run the existing test suite green**

Run: `cd api && uv run pytest -q` and `cd core && uv run pytest -q`.
Expected: all pass.

- [ ] **Step 3: Note the baseline commit SHA**

Run: `git rev-parse HEAD`
Record it — every phase ends with a commit, and you should be able to bisect from this anchor.

---

## Phase 1 — Rename `sender_domains` → `email_domains`

Goal: surface rename only. No behavior change. End state: outbound email still works; routes / model / OpenAPI / CLI all use `email_domains`.

### Task 1.1: Migration to rename table, indexes, FKs

**Files:**

- Create: `api/migrations/versions/0006_email_domains_rename.py`

- [ ] **Step 1: List the names alembic needs to rename**

Run: `psql "$DATABASE_URL" -c "\d sender_domains" | head -40` (in a dev shell) and `psql "$DATABASE_URL" -c "\d emails" | grep sender_domain`.
Expected: pkey is `sender_domains_pkey`, FK on `emails` is `emails_sender_domain_id_fkey`, unique constraint on `(organization_id, domain)` is `sender_domains_org_domain_uq` (or whatever migration 0005 named it — note any divergence).

- [ ] **Step 2: Write the migration**

```python
"""rename sender_domains to email_domains

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-06
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("sender_domains", "email_domains")
    op.execute("ALTER INDEX sender_domains_pkey RENAME TO email_domains_pkey")
    op.execute(
        "ALTER INDEX sender_domains_org_domain_uq RENAME TO email_domains_org_domain_uq"
    )
    op.alter_column("emails", "sender_domain_id", new_column_name="email_domain_id")
    op.execute(
        "ALTER TABLE emails RENAME CONSTRAINT emails_sender_domain_id_fkey "
        "TO emails_email_domain_id_fkey"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE emails RENAME CONSTRAINT emails_email_domain_id_fkey "
        "TO emails_sender_domain_id_fkey"
    )
    op.alter_column("emails", "email_domain_id", new_column_name="sender_domain_id")
    op.execute(
        "ALTER INDEX email_domains_org_domain_uq RENAME TO sender_domains_org_domain_uq"
    )
    op.execute("ALTER INDEX email_domains_pkey RENAME TO sender_domains_pkey")
    op.rename_table("email_domains", "sender_domains")
```

- [ ] **Step 3: Apply and roll back to prove reversibility**

Run: `cd api && uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: no errors. `psql ... -c "\d email_domains"` shows the renamed shape.

### Task 1.2: Rename ORM class and update imports

**Files:**

- Modify: `core/hailhq/core/models.py` (lines containing `SenderDomain` class and `sender_domain_id` column)
- Modify: every file that imports `SenderDomain` from `hailhq.core.models` (use grep to enumerate)

- [ ] **Step 1: Locate every reference**

Run: `rg -n 'SenderDomain|sender_domain' --type py`
Expected: every match must be either renamed or noted as part of a later step.

- [ ] **Step 2: Rename in `core/hailhq/core/models.py`**

In `class SenderDomain(Base):`:

- Rename to `class EmailDomain(Base):`.
- Change `__tablename__ = "sender_domains"` to `__tablename__ = "email_domains"`.
- Locate the `__table_args__` if any contain `name="sender_domains_org_domain_uq"` — rename to `name="email_domains_org_domain_uq"`.
- In `class Email`, rename `sender_domain_id: Mapped[uuid.UUID]` to `email_domain_id: Mapped[uuid.UUID]`. Update the `ForeignKey("sender_domains.id", ...)` to `ForeignKey("email_domains.id", ...)`.

- [ ] **Step 3: Add a back-compat alias for one revision**

At the bottom of `models.py`, **after** the `EmailDomain` definition:

```python
# Back-compat alias removed at the end of this milestone — kept here so
# any in-flight branch importing the old name still resolves while the
# rename PR lands.
SenderDomain = EmailDomain
```

- [ ] **Step 4: Update every non-test importer**

Run: `rg -n 'SenderDomain' --type py -l`
For each file (other than `core/hailhq/core/models.py`), rewrite imports + identifiers to `EmailDomain`. Don't leave any usage of `SenderDomain` in committed code outside the alias.

- [ ] **Step 5: Run core tests**

Run: `cd core && uv run pytest -q`
Expected: pass. If anything still uses `sender_domain_id` SQL, fix it now.

### Task 1.3: Rename API route file + endpoint paths

**Files:**

- Move: `api/hailhq/api/routes/sender_domains.py` → `api/hailhq/api/routes/email_domains.py`
- Modify: `api/hailhq/api/main.py` (or wherever routers are registered)
- Modify: any test importing the route module

- [ ] **Step 1: Move the file and rename the router prefix**

Run: `git mv api/hailhq/api/routes/sender_domains.py api/hailhq/api/routes/email_domains.py` (or just `mv` if not staged yet — the user will commit at end of phase).

In the moved file, change:

```python
router = APIRouter(prefix="/sender-domains", tags=["sender-domains"])
```

to:

```python
router = APIRouter(prefix="/email-domains", tags=["email-domains"])
```

Rename any helper symbols whose names contain `sender_domain` to `email_domain` (e.g. `get_email_provider` is fine, but `_sender_domain_for_org` → `_email_domain_for_org`).

- [ ] **Step 2: Update the importer**

Run: `rg -n 'from hailhq.api.routes.sender_domains' --type py`
For each match, change to `from hailhq.api.routes.email_domains` and update `app.include_router(sender_domains.router)` to `app.include_router(email_domains.router)`.

- [ ] **Step 3: Update emails.py importers**

`api/hailhq/api/routes/emails.py` imports `from hailhq.api.routes.sender_domains import ...`. Update to `from hailhq.api.routes.email_domains import ...`. Helper names also need their `sender_domain` → `email_domain` rename (e.g. references to `sd: SenderDomain` become `sd: EmailDomain`).

- [ ] **Step 4: Run API tests**

Run: `cd api && uv run pytest -q`
Expected: pass. Anything that 404s on `/sender-domains` is a test that needs updating to `/email-domains`.

### Task 1.4: Rename pydantic schemas

**Files:**

- Modify: `core/hailhq/core/schemas.py`

- [ ] **Step 1: Find schema classes to rename**

Run: `rg -n 'class SenderDomain' core/hailhq/core/schemas.py`
Typical matches: `SenderDomainCreate`, `SenderDomainResponse`, `SenderDomainSummary`, `SenderDomainListResponse`, `SenderDomainPatch` (whichever the file currently has).

- [ ] **Step 2: Rename each class and update references**

For each `SenderDomain<X>`:

- Rename to `EmailDomain<X>`.
- Search for usages (`rg 'SenderDomain<X>' --type py`) and rename.
- Add back-compat aliases at the bottom of `schemas.py`:

```python
# Back-compat aliases — removed at the end of this milestone.
SenderDomainCreate = EmailDomainCreate
SenderDomainResponse = EmailDomainResponse
SenderDomainSummary = EmailDomainSummary
SenderDomainListResponse = EmailDomainListResponse
```

(Keep the alias set matching whatever classes actually existed.)

- [ ] **Step 3: Run all tests**

Run: `cd core && uv run pytest -q && cd ../api && uv run pytest -q && cd ../sdk && uv run pytest -q`
Expected: pass.

### Task 1.5: Rename CLI command file

**Files:**

- Move: `cli/internal/cmd/sender_domain.go` → `cli/internal/cmd/email_domain.go`
- Move: `cli/internal/cmd/sender_domain_test.go` → `cli/internal/cmd/email_domain_test.go`
- Modify: `cli/internal/cmd/root.go` (or wherever the command is registered)

- [ ] **Step 1: Move the files**

```bash
git mv cli/internal/cmd/sender_domain.go cli/internal/cmd/email_domain.go
git mv cli/internal/cmd/sender_domain_test.go cli/internal/cmd/email_domain_test.go
```

- [ ] **Step 2: Rename the Cobra command and symbols**

Inside `email_domain.go`, change any identifier matching `senderDomain*` to `emailDomain*`. Rename the Cobra `Use:` string from `"sender-domain"` to `"email-domain"`. Update the parent command attach in `root.go` (or `email.go` if it's a subcommand).

- [ ] **Step 3: Update test fixtures**

Inside `email_domain_test.go`, update any expected route paths / fixture JSON keys from `sender_domains` / `/sender-domains` to `email_domains` / `/email-domains`.

- [ ] **Step 4: Compile and test**

Run: `cd cli && go build ./... && go test ./...`
Expected: pass. Any reference to `SenderDomain*` in the codegen client will be regenerated in Task 1.6.

### Task 1.6: Regenerate OpenAPI + CLI client

**Files:**

- Modify: `openapi/openapi.yaml`
- Modify: `cli/internal/client/client.gen.go`

- [ ] **Step 1: Regenerate the OpenAPI document**

Run the repo's OpenAPI export command (whatever `docs/contributing.md` or repo scripts define; commonly `cd api && uv run python -m hailhq.api.openapi > ../openapi/openapi.yaml`). If unsure, run `rg -n 'openapi.yaml' --type md` to find the documented command.

- [ ] **Step 2: Diff the output**

Run: `git diff openapi/openapi.yaml`
Expected: every `sender-domains` path replaced by `email-domains`, schema names `SenderDomain*` replaced by `EmailDomain*`. No other changes.

- [ ] **Step 3: Regenerate the CLI client**

Run the repo's codegen command (commonly `cd cli && go generate ./...`).
Expected: `client.gen.go` updates in lockstep.

- [ ] **Step 4: Final compile + tests**

Run: `cd cli && go build ./... && go test ./...` and `cd api && uv run pytest -q`.
Expected: green.

### Task 1.7: Commit Phase 1

- [ ] **Step 1: Stage + commit**

```bash
git add api/migrations/versions/0006_email_domains_rename.py \
        core/hailhq/core/models.py core/hailhq/core/schemas.py \
        api/hailhq/api/routes/email_domains.py api/hailhq/api/main.py \
        api/hailhq/api/routes/emails.py \
        cli/internal/cmd/email_domain.go cli/internal/cmd/email_domain_test.go \
        cli/internal/cmd/root.go cli/internal/client/client.gen.go \
        openapi/openapi.yaml
git status   # confirm sender_domains.py / sender_domain.go are gone
git commit -m "refactor(api): rename sender_domains to email_domains

Surface rename only — no behavior change. Prep for inbound email,
where the same row carries both directions. Adds migration 0006
plus back-compat aliases on SenderDomain/SenderDomain* schemas (to
be removed at the end of the inbound-email milestone)."
```

---

## Phase 2 — Inbound schema additions

Goal: schema can store inbound emails, attachments, per-domain inbound actions, and webhook subscriptions. No new behavior yet — the columns exist but nothing writes them.

### Task 2.1: Migration — inbound columns + email_attachments

**Files:**

- Create: `api/migrations/versions/0007_email_inbound_schema.py`

- [ ] **Step 1: Write the migration**

```python
"""inbound email schema: direction, attachments, action columns

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-06
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- emails: direction + inbound columns ----
    op.add_column(
        "emails",
        sa.Column("direction", sa.Text(), nullable=False, server_default="outbound"),
    )
    op.create_check_constraint(
        "emails_direction_check", "emails", "direction IN ('outbound','inbound')"
    )
    op.alter_column("emails", "email_domain_id", nullable=True)
    op.create_check_constraint(
        "emails_outbound_has_domain",
        "emails",
        "direction = 'inbound' OR email_domain_id IS NOT NULL",
    )
    op.add_column(
        "emails", sa.Column("provider_received_at", sa.TIMESTAMP(timezone=True))
    )
    op.add_column("emails", sa.Column("message_id", sa.Text()))
    op.add_column("emails", sa.Column("in_reply_to", sa.Text()))
    op.add_column(
        "emails", sa.Column("references_ids", postgresql.ARRAY(sa.Text()))
    )
    op.add_column("emails", sa.Column("raw_s3_key", sa.Text()))
    op.add_column("emails", sa.Column("spam_verdict", sa.Text()))
    op.add_column("emails", sa.Column("virus_verdict", sa.Text()))
    op.add_column("emails", sa.Column("dkim_verdict", sa.Text()))
    op.add_column("emails", sa.Column("spf_verdict", sa.Text()))
    op.add_column("emails", sa.Column("dmarc_verdict", sa.Text()))

    op.drop_constraint("emails_status_check", "emails", type_="check")
    op.create_check_constraint(
        "emails_status_check",
        "emails",
        "status IN ('queued','sent','failed','bounced','complained','received')",
    )

    op.create_index(
        "emails_org_direction_created_idx",
        "emails",
        ["organization_id", "direction", sa.text("created_at DESC")],
    )
    op.create_index("emails_message_id_idx", "emails", ["message_id"])
    op.create_index(
        "emails_inbound_message_id_uq",
        "emails",
        ["organization_id", "message_id"],
        unique=True,
        postgresql_where=sa.text(
            "direction = 'inbound' AND message_id IS NOT NULL"
        ),
    )

    # ---- email_domains: per-domain inbound action columns ----
    op.add_column(
        "email_domains",
        sa.Column(
            "inbound_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "email_domains", sa.Column("forward_to", postgresql.ARRAY(sa.Text()))
    )
    op.add_column("email_domains", sa.Column("webhook_url", sa.Text()))
    op.add_column("email_domains", sa.Column("webhook_secret_hash", sa.Text()))
    op.add_column(
        "email_domains", sa.Column("forward_rate_per_hour", sa.Integer())
    )
    op.create_check_constraint(
        "email_domains_inbound_action",
        "email_domains",
        "NOT inbound_enabled OR forward_to IS NOT NULL OR webhook_url IS NOT NULL",
    )
    op.create_check_constraint(
        "email_domains_webhook_pair",
        "email_domains",
        "(webhook_url IS NULL) = (webhook_secret_hash IS NULL)",
    )

    # ---- email_attachments ----
    op.create_table(
        "email_attachments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "email_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("emails.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("content_id", sa.Text()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "email_attachments_email_id_idx", "email_attachments", ["email_id"]
    )


def downgrade() -> None:
    op.drop_index("email_attachments_email_id_idx", table_name="email_attachments")
    op.drop_table("email_attachments")

    op.drop_constraint(
        "email_domains_webhook_pair", "email_domains", type_="check"
    )
    op.drop_constraint(
        "email_domains_inbound_action", "email_domains", type_="check"
    )
    for col in (
        "forward_rate_per_hour",
        "webhook_secret_hash",
        "webhook_url",
        "forward_to",
        "inbound_enabled",
    ):
        op.drop_column("email_domains", col)

    op.drop_index("emails_inbound_message_id_uq", table_name="emails")
    op.drop_index("emails_message_id_idx", table_name="emails")
    op.drop_index("emails_org_direction_created_idx", table_name="emails")

    op.drop_constraint("emails_status_check", "emails", type_="check")
    op.create_check_constraint(
        "emails_status_check",
        "emails",
        "status IN ('queued','sent','failed','bounced','complained')",
    )

    for col in (
        "dmarc_verdict",
        "spf_verdict",
        "dkim_verdict",
        "virus_verdict",
        "spam_verdict",
        "raw_s3_key",
        "references_ids",
        "in_reply_to",
        "message_id",
        "provider_received_at",
    ):
        op.drop_column("emails", col)

    op.drop_constraint("emails_outbound_has_domain", "emails", type_="check")
    op.alter_column("emails", "email_domain_id", nullable=False)
    op.drop_constraint("emails_direction_check", "emails", type_="check")
    op.drop_column("emails", "direction")
```

- [ ] **Step 2: Apply, downgrade, re-apply**

Run: `cd api && uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: no errors. `psql ... -c "\d emails"` shows the new columns; `psql ... -c "\d email_attachments"` exists.

### Task 2.2: Extend ORM models

**Files:**

- Modify: `core/hailhq/core/models.py`

- [ ] **Step 1: Update `Email` class**

In `class Email(Base):`, add (after `metadata_`):

```python
    direction: Mapped[str] = mapped_column(
        Text, server_default="outbound", nullable=False
    )
    provider_received_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    references_ids: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    raw_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    spam_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    virus_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    dkim_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    spf_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    dmarc_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Update `email_domain_id` mapping to drop `nullable=False`:

```python
    email_domain_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_domains.id", ondelete="RESTRICT"),
        nullable=True,
    )
```

Update `__table_args__` to include new check constraints + indexes (mirror the migration):

```python
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','sent','failed','bounced','complained','received')",
            name="emails_status_check",
        ),
        CheckConstraint(
            "array_length(to_addresses, 1) >= 1",
            name="emails_to_addresses_nonempty",
        ),
        CheckConstraint(
            "body_text IS NOT NULL OR body_html IS NOT NULL",
            name="emails_body_required",
        ),
        CheckConstraint(
            "direction IN ('outbound','inbound')",
            name="emails_direction_check",
        ),
        CheckConstraint(
            "direction = 'inbound' OR email_domain_id IS NOT NULL",
            name="emails_outbound_has_domain",
        ),
    )
```

- [ ] **Step 2: Add `EmailAttachment` class**

After the `Email` class:

```python
class EmailAttachment(Base):
    __tablename__ = "email_attachments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    email_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("emails.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
```

- [ ] **Step 3: Add inbound-action columns to `EmailDomain`**

In `class EmailDomain(Base):`, add (after the existing columns):

```python
    inbound_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    forward_to: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_secret_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    forward_rate_per_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

Make sure `Boolean` is imported at the top of the file (`from sqlalchemy import Boolean`).

- [ ] **Step 4: Write a model-shape test**

Create `core/tests/models/test_email_inbound_shape.py`:

```python
from hailhq.core.models import Email, EmailAttachment, EmailDomain


def test_email_has_inbound_columns():
    cols = {c.name for c in Email.__table__.columns}
    assert {
        "direction",
        "message_id",
        "in_reply_to",
        "references_ids",
        "raw_s3_key",
        "spam_verdict",
        "virus_verdict",
        "dkim_verdict",
        "spf_verdict",
        "dmarc_verdict",
        "raw_s3_key",
        "provider_received_at",
    } <= cols


def test_email_domain_id_nullable():
    assert Email.__table__.c.email_domain_id.nullable is True


def test_email_domain_has_action_columns():
    cols = {c.name for c in EmailDomain.__table__.columns}
    assert {
        "inbound_enabled",
        "forward_to",
        "webhook_url",
        "webhook_secret_hash",
        "forward_rate_per_hour",
    } <= cols


def test_email_attachments_table_exists():
    assert EmailAttachment.__table__.name == "email_attachments"
```

- [ ] **Step 5: Run the test**

Run: `cd core && uv run pytest tests/models/test_email_inbound_shape.py -v`
Expected: pass.

### Task 2.3: Extend pydantic schemas

**Files:**

- Modify: `core/hailhq/core/schemas.py`

- [ ] **Step 1: Add `EmailAttachmentResponse` + inbound fields to `EmailResponse`**

In `core/hailhq/core/schemas.py`, just before `class EmailResponse`:

```python
class EmailAttachmentResponse(BaseModel):
    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    content_id: str | None = None
    url: str  # API URL that 302s to a presigned S3 URL

    model_config = ConfigDict(from_attributes=True)
```

Extend `class EmailResponse` (or `EmailSummary` if shared fields live there) to include:

```python
    direction: Literal["outbound", "inbound"] = "outbound"
    message_id: str | None = None
    in_reply_to: str | None = None
    references_ids: list[str] | None = None
    spam_verdict: str | None = None
    virus_verdict: str | None = None
    dkim_verdict: str | None = None
    spf_verdict: str | None = None
    dmarc_verdict: str | None = None
    provider_received_at: datetime | None = None
    raw_url: str | None = None  # only set when direction='inbound'
    attachments: list[EmailAttachmentResponse] = []
```

Make sure `Literal`, `datetime`, `UUID`, `ConfigDict` are imported.

- [ ] **Step 2: Add `EmailDomain` action fields to its response schema**

Find `EmailDomainResponse` (renamed in Task 1.4). Add:

```python
    inbound_enabled: bool = False
    forward_to: list[str] | None = None
    webhook_url: str | None = None
    forward_rate_per_hour: int | None = None
    # Note: webhook_secret_hash is never returned. Plaintext is returned
    # once at create/rotate via a separate endpoint.
```

Add an `EmailDomainPatch` if it doesn't exist yet, exposing settable fields:

```python
class EmailDomainPatch(BaseModel):
    inbound_enabled: bool | None = None
    forward_to: list[str] | None = None
    webhook_url: str | None = None
    forward_rate_per_hour: int | None = None
    # local-part prefixes for hail-mail rows already live here if applicable
```

- [ ] **Step 3: Smoke-test schema imports**

Create `core/tests/schemas/test_email_inbound_schemas.py`:

```python
from hailhq.core.schemas import (
    EmailAttachmentResponse,
    EmailDomainPatch,
    EmailDomainResponse,
    EmailResponse,
)


def test_email_response_has_inbound_fields():
    fields = EmailResponse.model_fields
    for name in (
        "direction",
        "message_id",
        "in_reply_to",
        "references_ids",
        "raw_url",
        "attachments",
        "spam_verdict",
    ):
        assert name in fields, name


def test_email_domain_response_has_action_fields():
    fields = EmailDomainResponse.model_fields
    for name in ("inbound_enabled", "forward_to", "webhook_url"):
        assert name in fields


def test_email_domain_patch_allows_partial_updates():
    p = EmailDomainPatch(inbound_enabled=True)
    assert p.inbound_enabled is True
    assert p.forward_to is None


def test_attachment_response_round_trip():
    a = EmailAttachmentResponse(
        id="00000000-0000-0000-0000-000000000001",
        filename="a.pdf",
        content_type="application/pdf",
        size_bytes=10,
        content_id=None,
        url="https://api.hail.so/emails/x/attachments/y",
    )
    assert a.filename == "a.pdf"
```

Run: `cd core && uv run pytest tests/schemas/test_email_inbound_schemas.py -v`
Expected: pass.

### Task 2.4: Commit Phase 2

- [ ] **Step 1: Stage + commit**

```bash
git add api/migrations/versions/0007_email_inbound_schema.py \
        core/hailhq/core/models.py core/hailhq/core/schemas.py \
        core/tests/models/test_email_inbound_shape.py \
        core/tests/schemas/test_email_inbound_schemas.py
git commit -m "feat(core): add inbound email schema (no behavior yet)

Migration 0007 adds Email.direction + inbound columns,
email_attachments, email_domains action columns
(inbound_enabled, forward_to, webhook_url, webhook_secret_hash,
forward_rate_per_hour), plus the inbound idempotency partial unique
index. Models and pydantic schemas updated. No route or worker
changes — those land in subsequent phases."
```

---

## Phase 3 — `InboundProvider` interface, MIME parser, routing

Goal: pure-`core/` units that can turn raw MIME bytes into a structured Inbound payload, classify recipients into orgs, and stub the future SMTP provider. No DB or HTTP at this layer.

### Task 3.1: `InboundProvider` ABC + `InboundMessage` model

**Files:**

- Create: `core/hailhq/core/providers/email/inbound/__init__.py`
- Create: `core/hailhq/core/providers/email/inbound/base.py`

- [ ] **Step 1: Write the failing test**

Create `core/tests/providers/email/inbound/test_base.py`:

```python
from collections.abc import Mapping

import pytest

from hailhq.core.providers.email.inbound import (
    InboundMessage,
    InboundProvider,
)


def test_inbound_message_required_fields():
    msg = InboundMessage(
        provider_message_id="abc",
        envelope_from="alice@example.com",
        envelope_recipients=["bob+acme@mail.hail.so"],
        raw_s3_bucket="hail-inbound",
        raw_s3_key="raw/abc",
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        received_at=None,
    )
    assert msg.provider_message_id == "abc"


def test_inbound_provider_is_abstract():
    with pytest.raises(TypeError):
        InboundProvider()  # type: ignore[abstract]
```

- [ ] **Step 2: Run the test (expect ImportError → FAIL)**

Run: `cd core && uv run pytest tests/providers/email/inbound/test_base.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write the module**

`core/hailhq/core/providers/email/inbound/base.py`:

```python
"""Provider-neutral inbound email plumbing.

An ``InboundProvider`` accepts a raw notification (Lambda invoke for SES,
LMTP/SMTP for the future SMTP listener) and produces a single
``InboundMessage`` — the contract the rest of the pipeline consumes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel

__all__ = ["InboundMessage", "InboundProvider"]


class InboundMessage(BaseModel):
    """One inbound mail event, normalized across providers."""

    provider_message_id: str
    envelope_from: str
    envelope_recipients: list[str]
    raw_s3_bucket: str
    raw_s3_key: str
    spam_verdict: str | None = None
    virus_verdict: str | None = None
    spf_verdict: str | None = None
    dkim_verdict: str | None = None
    dmarc_verdict: str | None = None
    received_at: datetime | None = None


class InboundProvider(ABC):
    """How raw MIME reaches Hail."""

    @abstractmethod
    async def verify_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> bool:
        """Confirm the notification originated from the configured provider."""

    @abstractmethod
    async def parse_notification(self, body: bytes) -> InboundMessage:
        """Decode the notification body into an ``InboundMessage``."""
```

`core/hailhq/core/providers/email/inbound/__init__.py`:

```python
from hailhq.core.providers.email.inbound.base import (
    InboundMessage,
    InboundProvider,
)

__all__ = ["InboundMessage", "InboundProvider"]
```

- [ ] **Step 4: Run the test (expect PASS)**

Run: `cd core && uv run pytest tests/providers/email/inbound/test_base.py -v`
Expected: pass.

### Task 3.2: `SesInboundProvider` — notification parsing + HMAC

**Files:**

- Create: `core/hailhq/core/providers/email/inbound/ses.py`

- [ ] **Step 1: Write failing tests**

Create `core/tests/providers/email/inbound/test_ses.py`:

```python
import asyncio
import hashlib
import hmac
import json

import pytest

from hailhq.core.providers.email.inbound.ses import SesInboundProvider


SAMPLE = {
    "message_id": "abc123",
    "envelope_from": "alice@example.com",
    "recipients": ["bob+acme@mail.hail.so"],
    "verdicts": {
        "spam": "PASS",
        "virus": "PASS",
        "spf": "PASS",
        "dkim": "PASS",
        "dmarc": "PASS",
    },
    "s3_bucket": "hail-inbound",
    "s3_key": "raw/abc123",
    "timestamp": "2026-06-06T10:11:12Z",
}


def _signed(body: bytes, secret: str) -> dict[str, str]:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hail-Signature": f"sha256={sig}"}


def test_verify_notification_accepts_valid_signature():
    p = SesInboundProvider(hmac_secret="s3cret")
    body = json.dumps(SAMPLE).encode()
    headers = _signed(body, "s3cret")
    assert asyncio.run(p.verify_notification(headers, body)) is True


def test_verify_notification_rejects_bad_signature():
    p = SesInboundProvider(hmac_secret="s3cret")
    body = json.dumps(SAMPLE).encode()
    headers = {"X-Hail-Signature": "sha256=deadbeef"}
    assert asyncio.run(p.verify_notification(headers, body)) is False


def test_verify_notification_rejects_missing_header():
    p = SesInboundProvider(hmac_secret="s3cret")
    body = json.dumps(SAMPLE).encode()
    assert asyncio.run(p.verify_notification({}, body)) is False


def test_parse_notification_round_trip():
    p = SesInboundProvider(hmac_secret="s3cret")
    msg = asyncio.run(p.parse_notification(json.dumps(SAMPLE).encode()))
    assert msg.provider_message_id == "abc123"
    assert msg.envelope_recipients == ["bob+acme@mail.hail.so"]
    assert msg.spam_verdict == "PASS"
    assert msg.raw_s3_bucket == "hail-inbound"
    assert msg.raw_s3_key == "raw/abc123"
```

- [ ] **Step 2: Run to confirm failure**

Run: `cd core && uv run pytest tests/providers/email/inbound/test_ses.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

`core/hailhq/core/providers/email/inbound/ses.py`:

```python
"""SES-backed inbound provider.

Decodes the small JSON envelope our ses-ingest-lambda sends and
verifies the shared-secret HMAC. Raw MIME stays in S3; this adapter
does not fetch it — that's the ingest endpoint's job.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime

from hailhq.core.providers.email.inbound.base import (
    InboundMessage,
    InboundProvider,
)

__all__ = ["SesInboundProvider"]


class SesInboundProvider(InboundProvider):
    def __init__(self, *, hmac_secret: str) -> None:
        if not hmac_secret:
            raise ValueError("SesInboundProvider requires a non-empty hmac_secret")
        self._secret = hmac_secret.encode()

    async def verify_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> bool:
        header = headers.get("X-Hail-Signature") or headers.get("x-hail-signature")
        if not header or not header.startswith("sha256="):
            return False
        provided = header.split("=", 1)[1]
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, expected)

    async def parse_notification(self, body: bytes) -> InboundMessage:
        data = json.loads(body)
        verdicts = data.get("verdicts") or {}
        ts = data.get("timestamp")
        received = (
            datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
        )
        return InboundMessage(
            provider_message_id=data["message_id"],
            envelope_from=data["envelope_from"],
            envelope_recipients=list(data["recipients"]),
            raw_s3_bucket=data["s3_bucket"],
            raw_s3_key=data["s3_key"],
            spam_verdict=verdicts.get("spam"),
            virus_verdict=verdicts.get("virus"),
            spf_verdict=verdicts.get("spf"),
            dkim_verdict=verdicts.get("dkim"),
            dmarc_verdict=verdicts.get("dmarc"),
            received_at=received,
        )
```

- [ ] **Step 4: Re-run tests (expect PASS)**

Run: `cd core && uv run pytest tests/providers/email/inbound/test_ses.py -v`
Expected: pass.

### Task 3.3: `SmtpInboundProvider` stub

**Files:**

- Create: `core/hailhq/core/providers/email/inbound/smtp.py`

- [ ] **Step 1: Write the test**

Create `core/tests/providers/email/inbound/test_smtp_stub.py`:

```python
import asyncio

import pytest

from hailhq.core.providers.email.inbound.smtp import SmtpInboundProvider


def test_smtp_provider_raises_not_implemented():
    p = SmtpInboundProvider()
    with pytest.raises(NotImplementedError):
        asyncio.run(p.verify_notification({}, b""))
    with pytest.raises(NotImplementedError):
        asyncio.run(p.parse_notification(b""))
```

- [ ] **Step 2: Write the stub**

`core/hailhq/core/providers/email/inbound/smtp.py`:

```python
"""SMTP-listener inbound provider — placeholder.

The cloud-agnostic / OSS-only ingress path. Deferred to a follow-up
milestone (see docs/setup/smtp-inbound.md). The class exists so the
provider registry and tests have a stable identifier to import; every
method raises NotImplementedError.
"""
from __future__ import annotations

from collections.abc import Mapping

from hailhq.core.providers.email.inbound.base import (
    InboundMessage,
    InboundProvider,
)

__all__ = ["SmtpInboundProvider"]


class SmtpInboundProvider(InboundProvider):
    async def verify_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> bool:
        raise NotImplementedError(
            "SmtpInboundProvider is not yet implemented — "
            "see docs/setup/smtp-inbound.md"
        )

    async def parse_notification(self, body: bytes) -> InboundMessage:
        raise NotImplementedError(
            "SmtpInboundProvider is not yet implemented — "
            "see docs/setup/smtp-inbound.md"
        )
```

- [ ] **Step 3: Run tests**

Run: `cd core && uv run pytest tests/providers/email/inbound/ -v`
Expected: pass.

### Task 3.4: MIME parser

**Files:**

- Create: `core/hailhq/core/email_mime.py`
- Create: `core/tests/test_email_mime.py`
- Create: fixture: `core/tests/fixtures/inbound/simple.eml`
- Create: fixture: `core/tests/fixtures/inbound/multipart_attachment.eml`
- Create: fixture: `core/tests/fixtures/inbound/threaded.eml`

- [ ] **Step 1: Drop in the fixtures**

`core/tests/fixtures/inbound/simple.eml`:

```
From: Alice <alice@example.com>
To: Bob <bob+acme@mail.hail.so>
Subject: Hello
Message-ID: <m1@example.com>
Date: Sat, 06 Jun 2026 10:11:12 +0000
Content-Type: text/plain; charset="utf-8"

Hi Bob, hello from Alice.
```

`core/tests/fixtures/inbound/multipart_attachment.eml`:

```
From: Alice <alice@example.com>
To: Bob <bob+acme@mail.hail.so>
Subject: With attachment
Message-ID: <m2@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUNDARY"

--BOUNDARY
Content-Type: text/plain; charset="utf-8"

See attached.

--BOUNDARY
Content-Type: application/pdf; name="report.pdf"
Content-Disposition: attachment; filename="report.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQKJeLjz9MKCg==

--BOUNDARY--
```

`core/tests/fixtures/inbound/threaded.eml`:

```
From: Carol <carol@example.com>
To: Bob <bob+acme@mail.hail.so>
Subject: Re: Hello
Message-ID: <m3@example.com>
In-Reply-To: <m1@example.com>
References: <m1@example.com> <m2@example.com>
Content-Type: text/plain; charset="utf-8"

Replying to your message.
```

- [ ] **Step 2: Write failing tests**

`core/tests/test_email_mime.py`:

```python
from pathlib import Path

from hailhq.core.email_mime import ParsedMime, parse_mime

FIX = Path(__file__).parent / "fixtures" / "inbound"


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def test_parse_simple():
    p = parse_mime(_read("simple.eml"))
    assert p.from_address == "alice@example.com"
    assert "bob+acme@mail.hail.so" in p.to_addresses
    assert p.subject == "Hello"
    assert p.message_id == "<m1@example.com>"
    assert p.body_text and "Alice" in p.body_text
    assert p.body_html is None
    assert p.attachments == []


def test_parse_multipart_with_attachment():
    p = parse_mime(_read("multipart_attachment.eml"))
    assert p.body_text and "See attached" in p.body_text
    assert len(p.attachments) == 1
    a = p.attachments[0]
    assert a.filename == "report.pdf"
    assert a.content_type == "application/pdf"
    assert a.payload.startswith(b"%PDF-1.4")


def test_parse_threaded():
    p = parse_mime(_read("threaded.eml"))
    assert p.in_reply_to == "<m1@example.com>"
    assert p.references_ids == ["<m1@example.com>", "<m2@example.com>"]
```

- [ ] **Step 3: Run (expect FAIL)**

Run: `cd core && uv run pytest tests/test_email_mime.py -v`
Expected: FAIL — module missing.

- [ ] **Step 4: Implement parser**

`core/hailhq/core/email_mime.py`:

```python
"""Stdlib-`email` MIME parser tailored to inbound ingestion.

The parser keeps the API tight: a single ``parse_mime`` entry point
returns a ``ParsedMime`` dataclass with the fields the inbound pipeline
needs (envelope-like header derivatives, body parts, attachments as raw
bytes). Storing attachments to S3 is the caller's job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message
from email.utils import getaddresses

__all__ = ["ParsedMime", "ParsedAttachment", "parse_mime"]


@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    payload: bytes
    content_id: str | None = None


@dataclass
class ParsedMime:
    from_address: str
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    message_id: str | None
    in_reply_to: str | None
    references_ids: list[str] | None
    body_text: str | None
    body_html: str | None
    attachments: list[ParsedAttachment] = field(default_factory=list)


def _addresses(msg: Message, header: str) -> list[str]:
    raw = msg.get_all(header) or []
    return [addr for _name, addr in getaddresses(raw) if addr]


def _references(msg: Message) -> list[str] | None:
    raw = msg.get("References")
    if not raw:
        return None
    parts = [p.strip() for p in raw.split() if p.strip()]
    return parts or None


def _walk_bodies(
    msg: Message,
) -> tuple[str | None, str | None, list[ParsedAttachment]]:
    text: str | None = None
    html: str | None = None
    atts: list[ParsedAttachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disp == "attachment" or filename:
            payload = part.get_payload(decode=True) or b""
            atts.append(
                ParsedAttachment(
                    filename=filename or "attachment",
                    content_type=ctype,
                    payload=payload,
                    content_id=(part.get("Content-ID") or "").strip("<>") or None,
                )
            )
            continue
        if ctype == "text/plain" and text is None:
            text = part.get_content()
        elif ctype == "text/html" and html is None:
            html = part.get_content()
    return text, html, atts


def parse_mime(raw: bytes) -> ParsedMime:
    msg = message_from_bytes(raw)
    text, html, atts = _walk_bodies(msg)
    from_list = _addresses(msg, "From")
    return ParsedMime(
        from_address=from_list[0] if from_list else "",
        to_addresses=_addresses(msg, "To"),
        cc_addresses=_addresses(msg, "Cc"),
        subject=msg.get("Subject", ""),
        message_id=(msg.get("Message-ID") or msg.get("Message-Id")),
        in_reply_to=msg.get("In-Reply-To"),
        references_ids=_references(msg),
        body_text=text,
        body_html=html,
        attachments=atts,
    )
```

- [ ] **Step 5: Run tests (expect PASS)**

Run: `cd core && uv run pytest tests/test_email_mime.py -v`
Expected: pass.

### Task 3.5: Hail-mail local-part router

**Files:**

- Create: `core/hailhq/core/email_routing.py`
- Create: `core/tests/test_email_routing.py`

- [ ] **Step 1: Write failing tests**

`core/tests/test_email_routing.py`:

```python
from hailhq.core.email_routing import (
    HAIL_MAIL_PREFIX_PATTERN,
    classify_hail_mail_recipient,
    HailMailRecipient,
)


def test_classify_full_address():
    r = classify_hail_mail_recipient("alice+acme@mail.hail.so", "mail.hail.so")
    assert r == HailMailRecipient(
        user_prefix="alice", org_prefix="acme", base_domain="mail.hail.so"
    )


def test_unknown_domain_returns_none():
    assert (
        classify_hail_mail_recipient("alice+acme@other.example", "mail.hail.so")
        is None
    )


def test_missing_plus_returns_none():
    assert (
        classify_hail_mail_recipient("alice@mail.hail.so", "mail.hail.so")
        is None
    )


def test_postmaster_returns_none():
    assert (
        classify_hail_mail_recipient("postmaster@mail.hail.so", "mail.hail.so")
        is None
    )


def test_pattern_rejects_uppercase():
    import re

    assert not re.match(HAIL_MAIL_PREFIX_PATTERN, "Alice")
    assert re.match(HAIL_MAIL_PREFIX_PATTERN, "alice")
```

- [ ] **Step 2: Run (expect FAIL)**

Run: `cd core && uv run pytest tests/test_email_routing.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`core/hailhq/core/email_routing.py`:

```python
"""Hail-mail local-part routing.

Mirrors the prefix grammar enforced on outbound:
``^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$``. The classifier returns
None for any recipient that isn't a well-formed ``<user>+<org>@<base>``
on the configured hail-mail base domain — including postmaster/abuse
aliases — so the caller can decide to drop or log.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "HAIL_MAIL_PREFIX_PATTERN",
    "HailMailRecipient",
    "classify_hail_mail_recipient",
]


HAIL_MAIL_PREFIX_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$"
_PREFIX_RE = re.compile(HAIL_MAIL_PREFIX_PATTERN)


@dataclass(frozen=True)
class HailMailRecipient:
    user_prefix: str
    org_prefix: str
    base_domain: str


def classify_hail_mail_recipient(
    address: str, base_domain: str
) -> HailMailRecipient | None:
    if "@" not in address:
        return None
    local, _, domain = address.partition("@")
    if domain.lower() != base_domain.lower():
        return None
    if "+" not in local:
        return None
    user, _, org = local.partition("+")
    if not _PREFIX_RE.match(user) or not _PREFIX_RE.match(org):
        return None
    return HailMailRecipient(
        user_prefix=user, org_prefix=org, base_domain=domain.lower()
    )
```

- [ ] **Step 4: Run (expect PASS)**

Run: `cd core && uv run pytest tests/test_email_routing.py -v`
Expected: pass.

### Task 3.6: Commit Phase 3

```bash
git add core/hailhq/core/providers/email/inbound/ \
        core/hailhq/core/email_mime.py \
        core/hailhq/core/email_routing.py \
        core/tests/providers/email/inbound/ \
        core/tests/test_email_mime.py \
        core/tests/test_email_routing.py \
        core/tests/fixtures/inbound/
git commit -m "feat(core): inbound provider interface, MIME parser, routing

Pure-core building blocks for the inbound pipeline. SesInboundProvider
verifies the Lambda HMAC and parses the small JSON notification;
SmtpInboundProvider is a placeholder. parse_mime turns raw MIME bytes
into ParsedMime + attachments. classify_hail_mail_recipient turns a
hail-mail address into the (user_prefix, org_prefix) tuple — anything
non-conforming returns None so callers can drop it."
```

---

## Phase 4 — Inbound S3 helper + `/internal/ses-events` endpoint

Goal: end-to-end "Lambda POSTs notification → DB row appears" works for the happy path, suppression paths, and idempotency. Forwarding and webhook fan-out land in later phases — this phase only persists.

### Task 4.1: Config additions

**Files:**

- Modify: `core/hailhq/core/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Add settings fields**

In `core/hailhq/core/config.py`, in the `Settings` class:

```python
    # ─── Inbound email (SES) ───
    hail_inbound_enabled: bool = False
    hail_inbound_bucket: str = ""
    hail_inbound_hmac_secret: str = ""
    hail_forward_max_hops: int = 3
    hail_forward_rate_per_hour: int = 200
    hail_webhook_allow_private_networks: bool = False
    hail_inbound_org_rate_per_hour: int = 1000
```

- [ ] **Step 2: Append to `.env.example`**

Add a new section after the existing AWS SES block:

```bash
# ─── AWS SES (inbound email) ─────────────────────────────────────────────────
# Turn the /internal/ses-events ingest endpoint on. Off by default so a
# misconfigured Lambda can't write rows into a deployment that hasn't opted
# into inbound yet.
HAIL_INBOUND_ENABLED=false
# S3 bucket Lambda writes raw MIME into and that the API reads back for
# parsing + presigned downloads. Created by infra/terraform.
HAIL_INBOUND_BUCKET=
# Shared secret between ses-ingest-lambda and the API. Lambda signs every
# POST with HMAC-SHA256 of the body; API verifies with this value.
HAIL_INBOUND_HMAC_SECRET=
# Forwarding controls — see docs/superpowers/specs/2026-06-06-inbound-email-design.md §6.2.
HAIL_FORWARD_MAX_HOPS=3
HAIL_FORWARD_RATE_PER_HOUR=200
# Inbound rate cap per org per hour. Beyond it, persist but skip fan-out.
HAIL_INBOUND_ORG_RATE_PER_HOUR=1000
# Self-host convenience: allow webhook targets pointing at localhost / RFC-1918 / link-local
# addresses. Leave false in production.
HAIL_WEBHOOK_ALLOW_PRIVATE_NETWORKS=false
```

- [ ] **Step 3: Smoke-test the config load**

Run: `cd core && uv run python -c "from hailhq.core.config import settings; print(settings.hail_inbound_enabled, settings.hail_forward_max_hops)"`
Expected: prints `False 3`.

### Task 4.2: S3 inbound helper

**Files:**

- Create: `core/hailhq/core/s3_inbound.py`
- Create: `core/tests/test_s3_inbound.py`

- [ ] **Step 1: Write failing tests**

```python
import asyncio
from unittest.mock import MagicMock

from hailhq.core.s3_inbound import S3InboundClient


def _stub_client(payload: bytes) -> MagicMock:
    client = MagicMock()
    body = MagicMock()
    body.read.return_value = payload
    client.get_object.return_value = {"Body": body}
    client.generate_presigned_url.return_value = "https://signed.example/foo"
    return client


def test_fetch_raw_returns_bytes():
    stub = _stub_client(b"raw bytes")
    client = S3InboundClient(client=stub, bucket="hail-inbound")
    result = asyncio.run(client.fetch_raw("raw/abc"))
    assert result == b"raw bytes"
    stub.get_object.assert_called_with(Bucket="hail-inbound", Key="raw/abc")


def test_put_attachment_writes():
    stub = _stub_client(b"")
    client = S3InboundClient(client=stub, bucket="hail-inbound")
    asyncio.run(client.put_attachment("attachments/e/1", b"pdfbytes", "application/pdf"))
    stub.put_object.assert_called_once()
    kwargs = stub.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "hail-inbound"
    assert kwargs["Key"] == "attachments/e/1"
    assert kwargs["Body"] == b"pdfbytes"
    assert kwargs["ContentType"] == "application/pdf"


def test_presign_returns_url():
    stub = _stub_client(b"")
    client = S3InboundClient(client=stub, bucket="hail-inbound")
    url = asyncio.run(client.presign_get("raw/abc", ttl_seconds=300))
    assert url == "https://signed.example/foo"
    stub.generate_presigned_url.assert_called_with(
        "get_object",
        Params={"Bucket": "hail-inbound", "Key": "raw/abc"},
        ExpiresIn=300,
    )
```

- [ ] **Step 2: Run (expect FAIL)**

Run: `cd core && uv run pytest tests/test_s3_inbound.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
"""S3 client wrapper for inbound MIME + attachment objects.

boto3 is sync; everything goes through asyncio.to_thread so FastAPI
handlers can await without blocking the loop. Same pattern as
SesEmailProvider.
"""
from __future__ import annotations

import asyncio
from typing import Any

import boto3

from hailhq.core.config import settings

__all__ = ["S3InboundClient", "build_default_client"]


def build_default_client() -> Any:
    return boto3.client(
        "s3",
        region_name=settings.aws_region or None,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
    )


class S3InboundClient:
    def __init__(self, *, client: Any | None = None, bucket: str) -> None:
        if not bucket:
            raise ValueError("S3InboundClient requires a bucket")
        self._client = client if client is not None else build_default_client()
        self._bucket = bucket

    async def fetch_raw(self, key: str) -> bytes:
        def _do() -> bytes:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read()

        return await asyncio.to_thread(_do)

    async def put_attachment(
        self, key: str, payload: bytes, content_type: str
    ) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=payload,
            ContentType=content_type,
        )

    async def presign_get(self, key: str, *, ttl_seconds: int = 300) -> str:
        def _do() -> str:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=ttl_seconds,
            )

        return await asyncio.to_thread(_do)
```

- [ ] **Step 4: Run (expect PASS)**

Run: `cd core && uv run pytest tests/test_s3_inbound.py -v`
Expected: pass.

### Task 4.3: Ingest persistence service

**Files:**

- Create: `core/hailhq/core/email_ingest.py`
- Create: `core/tests/test_email_ingest.py`

Pure service that takes an `InboundMessage` + raw MIME bytes + an S3 client and writes the `Email` rows + attachments. No FastAPI, no fan-out (yet).

- [ ] **Step 1: Write failing test against a sqlite + monkeypatch fixture**

The repo already has `core/tests/conftest.py` with an async session fixture against a test Postgres or sqlite shim — reuse it. If not, the fixture you need:

```python
# core/tests/test_email_ingest.py
import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hailhq.core.email_ingest import IngestResult, ingest_inbound
from hailhq.core.models import Email, EmailAttachment, EmailDomain, Organization
from hailhq.core.providers.email.inbound.base import InboundMessage

FIX = Path(__file__).parent / "fixtures" / "inbound"


@pytest.mark.asyncio
async def test_ingest_persists_inbound_row(async_session, sample_org):
    domain = EmailDomain(
        organization_id=sample_org.id,
        kind="hail_mail",
        domain=f"alice+{sample_org.slug}@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org=sample_org.slug,
        verification_status="verified",
        provider="ses",
        inbound_enabled=True,
    )
    async_session.add(domain)
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="abc123",
        envelope_from="alice@example.com",
        envelope_recipients=[f"alice+{sample_org.slug}@mail.hail.so"],
        raw_s3_bucket="b",
        raw_s3_key="raw/abc123",
        spam_verdict="PASS",
        virus_verdict="PASS",
        spf_verdict="PASS",
        dkim_verdict="PASS",
        dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )

    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "multipart_attachment.eml").read_bytes()
    s3.put_attachment.return_value = None

    result = await ingest_inbound(
        async_session,
        message=msg,
        s3=s3,
        hail_mail_base_domain="mail.hail.so",
    )
    assert isinstance(result, IngestResult)
    assert len(result.email_ids) == 1

    row = (
        await async_session.execute(
            Email.__table__.select().where(Email.id == result.email_ids[0])
        )
    ).first()
    assert row is not None
    assert row.direction == "inbound"
    assert row.status == "received"
    assert row.from_address == "alice@example.com"
    assert row.message_id == "<m2@example.com>"
    assert row.email_domain_id == domain.id
    # attachment row + put_attachment was called
    assert s3.put_attachment.call_count == 1
    atts = (
        await async_session.execute(
            EmailAttachment.__table__.select().where(
                EmailAttachment.email_id == row.id
            )
        )
    ).all()
    assert len(atts) == 1


@pytest.mark.asyncio
async def test_ingest_is_idempotent(async_session, sample_org):
    # ... (set up domain as above)
    msg = InboundMessage(
        provider_message_id="dup",
        envelope_from="x@example.com",
        envelope_recipients=[f"alice+{sample_org.slug}@mail.hail.so"],
        raw_s3_bucket="b",
        raw_s3_key="raw/dup",
        received_at=None,
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    r1 = await ingest_inbound(
        async_session, message=msg, s3=s3, hail_mail_base_domain="mail.hail.so"
    )
    r2 = await ingest_inbound(
        async_session, message=msg, s3=s3, hail_mail_base_domain="mail.hail.so"
    )
    assert r1.email_ids == r2.email_ids  # second call short-circuits


@pytest.mark.asyncio
async def test_ingest_suppresses_spam(async_session, sample_org):
    msg = InboundMessage(
        provider_message_id="spam1",
        envelope_from="x@spam.example",
        envelope_recipients=[f"alice+{sample_org.slug}@mail.hail.so"],
        raw_s3_bucket="b",
        raw_s3_key="raw/spam1",
        spam_verdict="FAIL",
        received_at=None,
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    result = await ingest_inbound(
        async_session, message=msg, s3=s3, hail_mail_base_domain="mail.hail.so"
    )
    row = (
        await async_session.execute(
            Email.__table__.select().where(Email.id == result.email_ids[0])
        )
    ).first()
    assert row.metadata_ == {"suppressed": "spam"}
    assert result.suppressed_reasons == ["spam"]
```

Adjust fixture names (`async_session`, `sample_org`) to whatever `core/tests/conftest.py` already provides; if conftest is missing those, add them as part of this task using the existing patterns in `core/tests/`.

- [ ] **Step 2: Run (expect FAIL)**

Run: `cd core && uv run pytest tests/test_email_ingest.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# core/hailhq/core/email_ingest.py
"""Inbound persistence service.

Orchestrates: fetch raw MIME from S3 → parse → resolve owning org per
recipient → write one Email row per org → extract attachments into S3
and write email_attachments rows. Idempotency is enforced by the
emails_inbound_message_id_uq partial unique index — the second insert
with the same (organization_id, message_id) hits IntegrityError, which
we treat as "already ingested, find it and return its id".

Forwarding and webhook fan-out are NOT triggered here. The ingest
service only persists. The caller (the API endpoint) decides what to
do next, with the result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.email_mime import ParsedAttachment, ParsedMime, parse_mime
from hailhq.core.email_routing import classify_hail_mail_recipient
from hailhq.core.models import Email, EmailAttachment, EmailDomain
from hailhq.core.providers.email.inbound.base import InboundMessage
from hailhq.core.s3_inbound import S3InboundClient

__all__ = ["IngestResult", "ingest_inbound"]


@dataclass
class IngestResult:
    email_ids: list[UUID] = field(default_factory=list)
    suppressed_reasons: list[str] = field(default_factory=list)
    skipped_recipients: list[str] = field(default_factory=list)


def _suppress_reason(message: InboundMessage) -> str | None:
    if message.virus_verdict == "FAIL":
        return "virus"
    if message.spam_verdict == "FAIL":
        return "spam"
    return None


async def _find_domain_for_recipient(
    db: AsyncSession, recipient: str, base_domain: str
) -> EmailDomain | None:
    classified = classify_hail_mail_recipient(recipient, base_domain)
    if classified is None:
        return None
    stmt = (
        select(EmailDomain)
        .where(EmailDomain.kind == "hail_mail")
        .where(EmailDomain.local_prefix_user == classified.user_prefix)
        .where(EmailDomain.local_prefix_org == classified.org_prefix)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _persist_attachments(
    db: AsyncSession,
    *,
    email_id: UUID,
    attachments: list[ParsedAttachment],
    bucket: str,
    s3: S3InboundClient,
) -> None:
    for parsed in attachments:
        att_id = uuid4()
        key = f"attachments/{email_id}/{att_id}"
        await s3.put_attachment(key, parsed.payload, parsed.content_type)
        row = EmailAttachment(
            id=att_id,
            email_id=email_id,
            filename=parsed.filename,
            content_type=parsed.content_type,
            size_bytes=len(parsed.payload),
            s3_key=key,
            content_id=parsed.content_id,
        )
        db.add(row)


async def _persist_one(
    db: AsyncSession,
    *,
    parsed: ParsedMime,
    message: InboundMessage,
    domain: EmailDomain,
    suppress: str | None,
    s3: S3InboundClient,
) -> UUID | None:
    metadata: dict[str, str] = {}
    if suppress:
        metadata["suppressed"] = suppress

    email = Email(
        organization_id=domain.organization_id,
        email_domain_id=domain.id,
        direction="inbound",
        from_address=parsed.from_address,
        to_addresses=parsed.to_addresses or list(message.envelope_recipients),
        cc_addresses=parsed.cc_addresses or None,
        subject=parsed.subject or "",
        body_text=parsed.body_text,
        body_html=parsed.body_html,
        status="received",
        provider="ses",
        provider_message_id=message.provider_message_id,
        message_id=parsed.message_id,
        in_reply_to=parsed.in_reply_to,
        references_ids=parsed.references_ids,
        raw_s3_key=message.raw_s3_key,
        spam_verdict=message.spam_verdict,
        virus_verdict=message.virus_verdict,
        spf_verdict=message.spf_verdict,
        dkim_verdict=message.dkim_verdict,
        dmarc_verdict=message.dmarc_verdict,
        provider_received_at=message.received_at,
        metadata_=metadata,
    )
    db.add(email)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        stmt = select(Email).where(
            Email.organization_id == domain.organization_id,
            Email.message_id == parsed.message_id,
            Email.direction == "inbound",
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()
        return existing.id if existing else None

    if suppress is None:
        await _persist_attachments(
            db,
            email_id=email.id,
            attachments=parsed.attachments,
            bucket=message.raw_s3_bucket,
            s3=s3,
        )
    await db.flush()
    return email.id


async def ingest_inbound(
    db: AsyncSession,
    *,
    message: InboundMessage,
    s3: S3InboundClient,
    hail_mail_base_domain: str,
) -> IngestResult:
    result = IngestResult()
    raw = await s3.fetch_raw(message.raw_s3_key)
    parsed = parse_mime(raw)
    suppress = _suppress_reason(message)
    if suppress:
        result.suppressed_reasons.append(suppress)

    seen_orgs: set[UUID] = set()
    for recipient in message.envelope_recipients:
        domain = await _find_domain_for_recipient(
            db, recipient, hail_mail_base_domain
        )
        if domain is None:
            result.skipped_recipients.append(recipient)
            continue
        if domain.organization_id in seen_orgs:
            continue
        seen_orgs.add(domain.organization_id)

        email_id = await _persist_one(
            db,
            parsed=parsed,
            message=message,
            domain=domain,
            suppress=suppress,
            s3=s3,
        )
        if email_id is not None:
            result.email_ids.append(email_id)

    await db.commit()
    return result
```

- [ ] **Step 4: Run (expect PASS)**

Run: `cd core && uv run pytest tests/test_email_ingest.py -v`
Expected: pass.

### Task 4.4: `/internal/ses-events` route

**Files:**

- Create: `api/hailhq/api/routes/internal/__init__.py`
- Create: `api/hailhq/api/routes/internal/ses_events.py`
- Modify: `api/hailhq/api/main.py`

- [ ] **Step 1: Write a failing route test**

`api/tests/test_internal_ses_events.py`:

```python
import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

FIX = (
    Path(__file__).parent.parent.parent / "core" / "tests" / "fixtures" / "inbound"
)


def _signed(body: bytes, secret: str) -> dict[str, str]:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hail-Signature": f"sha256={sig}"}


@pytest.mark.asyncio
async def test_rejects_missing_signature(api_client: AsyncClient, settings_override):
    settings_override(hail_inbound_enabled=True, hail_inbound_hmac_secret="s3cret")
    resp = await api_client.post("/internal/ses-events", json={"any": "thing"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_rejects_when_disabled(api_client: AsyncClient, settings_override):
    settings_override(hail_inbound_enabled=False, hail_inbound_hmac_secret="s3cret")
    body = json.dumps({}).encode()
    resp = await api_client.post(
        "/internal/ses-events", content=body, headers=_signed(body, "s3cret")
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_happy_path_inserts_row(
    api_client, settings_override, fixed_email_domain, fake_s3, monkeypatch
):
    settings_override(
        hail_inbound_enabled=True,
        hail_inbound_hmac_secret="s3cret",
        hail_inbound_bucket="hail-inbound",
        hail_mail_base_domain="mail.hail.so",
    )
    fake_s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()

    payload = {
        "message_id": "abc",
        "envelope_from": "alice@example.com",
        "recipients": [fixed_email_domain.domain],
        "verdicts": {"spam": "PASS", "virus": "PASS",
                     "spf": "PASS", "dkim": "PASS", "dmarc": "PASS"},
        "s3_bucket": "hail-inbound",
        "s3_key": "raw/abc",
        "timestamp": "2026-06-06T10:11:12Z",
    }
    body = json.dumps(payload).encode()
    resp = await api_client.post(
        "/internal/ses-events", content=body, headers=_signed(body, "s3cret")
    )
    assert resp.status_code == 202
    assert resp.json()["email_ids"]  # one or more
```

`fixed_email_domain`, `fake_s3`, `settings_override`, and `api_client` are conftest helpers — add them under `api/tests/conftest.py` if not already present, modeled on existing fixtures used by `test_emails_api.py`.

- [ ] **Step 2: Run (expect FAIL)**

Run: `cd api && uv run pytest tests/test_internal_ses_events.py -v`
Expected: FAIL — route not mounted.

- [ ] **Step 3: Implement the route**

`api/hailhq/api/routes/internal/__init__.py`:

```python
"""Operator/internal endpoints. Not exposed in the public OpenAPI spec."""
```

`api/hailhq/api/routes/internal/ses_events.py`:

```python
"""POST /internal/ses-events — ses-ingest-lambda → API.

HMAC-signed by the Lambda over the raw body (X-Hail-Signature header).
On a valid notification:
- fetch raw MIME from S3 via S3InboundClient
- run the ingest service (email_ingest.ingest_inbound)
- return 202 with the new row ids

Fan-out (forwarding, webhooks) lands in subsequent phases.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.email_ingest import ingest_inbound
from hailhq.core.providers.email.inbound.ses import SesInboundProvider
from hailhq.core.s3_inbound import S3InboundClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


def _provider() -> SesInboundProvider:
    return SesInboundProvider(hmac_secret=settings.hail_inbound_hmac_secret)


def _s3_client() -> S3InboundClient:
    return S3InboundClient(bucket=settings.hail_inbound_bucket)


@router.post("/ses-events")
async def receive_ses_event(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SesInboundProvider, Depends(_provider)],
    s3: Annotated[S3InboundClient, Depends(_s3_client)],
    x_hail_signature: Annotated[str | None, Header()] = None,
) -> dict[str, list[str]]:
    if not settings.hail_inbound_enabled:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="inbound disabled",
        )
    body = await request.body()
    headers = {"X-Hail-Signature": x_hail_signature or ""}
    if not await provider.verify_notification(headers, body):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="invalid signature",
        )
    message = await provider.parse_notification(body)
    result = await ingest_inbound(
        db,
        message=message,
        s3=s3,
        hail_mail_base_domain=settings.hail_mail_base_domain,
    )
    return {
        "email_ids": [str(x) for x in result.email_ids],
        "skipped_recipients": result.skipped_recipients,
        "suppressed_reasons": result.suppressed_reasons,
    }
```

- [ ] **Step 4: Mount the router**

In `api/hailhq/api/main.py`, add:

```python
from hailhq.api.routes.internal import ses_events as internal_ses_events
# ...
app.include_router(internal_ses_events.router)
```

- [ ] **Step 5: Run (expect PASS)**

Run: `cd api && uv run pytest tests/test_internal_ses_events.py -v`
Expected: pass.

### Task 4.5: Commit Phase 4

```bash
git add core/hailhq/core/config.py .env.example \
        core/hailhq/core/s3_inbound.py core/hailhq/core/email_ingest.py \
        api/hailhq/api/routes/internal/ \
        api/hailhq/api/main.py \
        core/tests/test_s3_inbound.py core/tests/test_email_ingest.py \
        api/tests/test_internal_ses_events.py \
        api/tests/conftest.py
git commit -m "feat(api): inbound ingest pipeline (persistence only)

POST /internal/ses-events accepts HMAC-signed notifications from
ses-ingest-lambda. Verifies signature, fetches raw MIME from S3, parses,
routes by hail-mail local-part, persists one Email row per matched org
plus attachments. Idempotency via the partial unique index — duplicate
SES messageIds short-circuit. Spam/virus FAIL verdicts persist the row
but mark metadata.suppressed. Forwarding and webhook fan-out land in
Phase 5+."
```

---

## Phase 5 — `webhook_subscriptions` + `webhook_deliveries` tables and CRUD

Goal: org-wide webhook subscription registry shipped. Delivery rows can be inserted, but the worker doesn't exist yet — that's Phase 6.

### Task 5.1: Migration

**Files:**

- Create: `api/migrations/versions/0008_webhook_subscriptions.py`

- [ ] **Step 1: Write**

```python
"""webhook subscriptions + deliveries

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-06
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column(
            "event_types",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_success_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_failure_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "webhook_subscriptions_status_check",
        "webhook_subscriptions",
        "status IN ('active','disabled')",
    )
    op.create_check_constraint(
        "webhook_subscriptions_event_types_nonempty",
        "webhook_subscriptions",
        "cardinality(event_types) >= 1",
    )
    op.create_index(
        "webhook_subscriptions_org_idx",
        "webhook_subscriptions",
        ["organization_id"],
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "email_domain_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("email_domains.id", ondelete="CASCADE"),
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "event_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("response_status", sa.Integer()),
        sa.Column("response_body", sa.Text()),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("succeeded_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "webhook_deliveries_target_check",
        "webhook_deliveries",
        "subscription_id IS NOT NULL OR email_domain_id IS NOT NULL",
    )
    op.create_check_constraint(
        "webhook_deliveries_status_check",
        "webhook_deliveries",
        "status IN ('pending','succeeded','failed','dead')",
    )
    op.create_index(
        "webhook_deliveries_pending_idx",
        "webhook_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "webhook_deliveries_pending_idx", table_name="webhook_deliveries"
    )
    op.drop_table("webhook_deliveries")
    op.drop_index(
        "webhook_subscriptions_org_idx", table_name="webhook_subscriptions"
    )
    op.drop_table("webhook_subscriptions")
```

- [ ] **Step 2: Apply + roll back**

Run: `cd api && uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: clean.

### Task 5.2: Models

**Files:**

- Modify: `core/hailhq/core/models.py`

- [ ] **Step 1: Add classes**

```python
class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    event_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, server_default="active", nullable=False
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )
    last_success_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','disabled')",
            name="webhook_subscriptions_status_check",
        ),
        CheckConstraint(
            "cardinality(event_types) >= 1",
            name="webhook_subscriptions_event_types_nonempty",
        ),
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=True,
    )
    email_domain_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_domains.id", ondelete="CASCADE"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    attempt: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, server_default="pending", nullable=False
    )
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    succeeded_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "subscription_id IS NOT NULL OR email_domain_id IS NOT NULL",
            name="webhook_deliveries_target_check",
        ),
        CheckConstraint(
            "status IN ('pending','succeeded','failed','dead')",
            name="webhook_deliveries_status_check",
        ),
    )
```

- [ ] **Step 2: Smoke-test imports**

Create `core/tests/models/test_webhook_models.py`:

```python
from hailhq.core.models import WebhookDelivery, WebhookSubscription


def test_subscription_columns():
    cols = {c.name for c in WebhookSubscription.__table__.columns}
    assert {
        "target_url",
        "secret_hash",
        "event_types",
        "status",
        "consecutive_failures",
    } <= cols


def test_delivery_columns():
    cols = {c.name for c in WebhookDelivery.__table__.columns}
    assert {
        "subscription_id",
        "email_domain_id",
        "event_type",
        "event_id",
        "payload",
        "next_attempt_at",
        "status",
    } <= cols
```

Run: `cd core && uv run pytest tests/models/test_webhook_models.py -v`
Expected: pass.

### Task 5.3: Pydantic schemas

**Files:**

- Modify: `core/hailhq/core/schemas.py`

- [ ] **Step 1: Add schemas**

```python
WebhookEventType = Literal[
    "email.received",
    "email.bounced",
    "email.complained",
    "email.received.suppressed",
]


class WebhookSubscriptionCreate(BaseModel):
    target_url: HttpUrl
    event_types: list[WebhookEventType] = Field(min_length=1)


class WebhookSubscriptionResponse(BaseModel):
    id: UUID
    organization_id: UUID
    target_url: str
    event_types: list[str]
    status: Literal["active", "disabled"]
    consecutive_failures: int
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # Only populated by the create + rotate-secret endpoints.
    secret: str | None = None

    model_config = ConfigDict(from_attributes=True)


class WebhookSubscriptionListResponse(BaseModel):
    items: list[WebhookSubscriptionResponse]
    next_cursor: str | None = None


class WebhookSubscriptionPatch(BaseModel):
    target_url: HttpUrl | None = None
    event_types: list[WebhookEventType] | None = None
    status: Literal["active", "disabled"] | None = None


class WebhookDeliveryResponse(BaseModel):
    id: UUID
    subscription_id: UUID | None
    email_domain_id: UUID | None
    event_type: str
    event_id: UUID
    attempt: int
    status: Literal["pending", "succeeded", "failed", "dead"]
    response_status: int | None = None
    response_body: str | None = None
    next_attempt_at: datetime
    succeeded_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WebhookDeliveryListResponse(BaseModel):
    items: list[WebhookDeliveryResponse]
    next_cursor: str | None = None
```

Make sure `HttpUrl`, `Field`, `Literal` are imported.

### Task 5.4: CRUD route

**Files:**

- Create: `api/hailhq/api/routes/webhooks.py`
- Modify: `api/hailhq/api/main.py`

- [ ] **Step 1: Write failing tests**

`api/tests/test_webhooks_api.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_create_subscription_returns_secret_once(api_client, auth_headers):
    resp = await api_client.post(
        "/webhooks",
        json={
            "target_url": "https://hooks.example.com/ingest",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["secret"].startswith("whs_")
    sub_id = body["id"]

    # Subsequent GET must NOT echo the secret.
    g = await api_client.get(f"/webhooks/{sub_id}", headers=auth_headers)
    assert g.status_code == 200
    assert "secret" not in g.json() or g.json()["secret"] is None


@pytest.mark.asyncio
async def test_list_paginates(api_client, auth_headers):
    for i in range(3):
        await api_client.post(
            "/webhooks",
            json={
                "target_url": f"https://example.com/{i}",
                "event_types": ["email.received"],
            },
            headers=auth_headers,
        )
    r = await api_client.get("/webhooks?limit=2", headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2


@pytest.mark.asyncio
async def test_rotate_secret_returns_new_value(api_client, auth_headers):
    create = await api_client.post(
        "/webhooks",
        json={
            "target_url": "https://example.com/x",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    sub_id = create.json()["id"]
    first_secret = create.json()["secret"]

    rot = await api_client.post(
        f"/webhooks/{sub_id}/rotate-secret", headers=auth_headers
    )
    assert rot.status_code == 200
    assert rot.json()["secret"] != first_secret


@pytest.mark.asyncio
async def test_patch_disables(api_client, auth_headers):
    create = await api_client.post(
        "/webhooks",
        json={
            "target_url": "https://example.com/x",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    sub_id = create.json()["id"]
    p = await api_client.patch(
        f"/webhooks/{sub_id}",
        json={"status": "disabled"},
        headers=auth_headers,
    )
    assert p.json()["status"] == "disabled"
```

- [ ] **Step 2: Run (expect FAIL)**

Run: `cd api && uv run pytest tests/test_webhooks_api.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# api/hailhq/api/routes/webhooks.py
"""CRUD for org-wide webhook subscriptions + delivery listing/redelivery."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.deps import Principal, get_current_principal
from hailhq.core.db import get_session
from hailhq.core.models import WebhookDelivery, WebhookSubscription
from hailhq.core.schemas import (
    WebhookDeliveryListResponse,
    WebhookDeliveryResponse,
    WebhookSubscriptionCreate,
    WebhookSubscriptionListResponse,
    WebhookSubscriptionPatch,
    WebhookSubscriptionResponse,
    decode_cursor,
    encode_cursor,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _new_secret() -> str:
    return "whs_" + secrets.token_urlsafe(24)


def _hash(secret: str) -> str:
    return bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()


def _to_response(
    sub: WebhookSubscription, *, secret: str | None = None
) -> WebhookSubscriptionResponse:
    response = WebhookSubscriptionResponse.model_validate(sub)
    if secret is not None:
        response = response.model_copy(update={"secret": secret})
    return response


@router.post(
    "",
    response_model=WebhookSubscriptionResponse,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_subscription(
    body: WebhookSubscriptionCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookSubscriptionResponse:
    secret = _new_secret()
    sub = WebhookSubscription(
        organization_id=principal.organization_id,
        target_url=str(body.target_url),
        secret_hash=_hash(secret),
        event_types=list(body.event_types),
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return _to_response(sub, secret=secret)


@router.get("", response_model=WebhookSubscriptionListResponse)
async def list_subscriptions(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> WebhookSubscriptionListResponse:
    stmt = (
        select(WebhookSubscription)
        .where(WebhookSubscription.organization_id == principal.organization_id)
    )
    if cursor:
        cur_ts, cur_id = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(WebhookSubscription.created_at, WebhookSubscription.id)
            < tuple_(cur_ts, cur_id)
        )
    stmt = stmt.order_by(
        WebhookSubscription.created_at.desc(), WebhookSubscription.id.desc()
    ).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_cursor(last.created_at, last.id)
        rows = rows[:limit]
    return WebhookSubscriptionListResponse(
        items=[_to_response(s) for s in rows],
        next_cursor=next_cursor,
    )


@router.get("/{sub_id}", response_model=WebhookSubscriptionResponse)
async def get_subscription(
    sub_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookSubscriptionResponse:
    sub = await _load_owned(db, sub_id, principal.organization_id)
    return _to_response(sub)


@router.patch("/{sub_id}", response_model=WebhookSubscriptionResponse)
async def patch_subscription(
    sub_id: UUID,
    body: WebhookSubscriptionPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookSubscriptionResponse:
    sub = await _load_owned(db, sub_id, principal.organization_id)
    updates: dict = {}
    if body.target_url is not None:
        updates["target_url"] = str(body.target_url)
    if body.event_types is not None:
        updates["event_types"] = list(body.event_types)
    if body.status is not None:
        updates["status"] = body.status
        if body.status == "active":
            updates["consecutive_failures"] = 0
    if updates:
        updates["updated_at"] = datetime.now(timezone.utc)
        await db.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == sub.id)
            .values(**updates)
        )
        await db.commit()
        await db.refresh(sub)
    return _to_response(sub)


@router.delete("/{sub_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_subscription(
    sub_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    sub = await _load_owned(db, sub_id, principal.organization_id)
    await db.delete(sub)
    await db.commit()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post(
    "/{sub_id}/rotate-secret", response_model=WebhookSubscriptionResponse
)
async def rotate_secret(
    sub_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookSubscriptionResponse:
    sub = await _load_owned(db, sub_id, principal.organization_id)
    secret = _new_secret()
    await db.execute(
        update(WebhookSubscription)
        .where(WebhookSubscription.id == sub.id)
        .values(secret_hash=_hash(secret), updated_at=datetime.now(timezone.utc))
    )
    await db.commit()
    await db.refresh(sub)
    return _to_response(sub, secret=secret)


@router.get(
    "/{sub_id}/deliveries", response_model=WebhookDeliveryListResponse
)
async def list_deliveries(
    sub_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> WebhookDeliveryListResponse:
    await _load_owned(db, sub_id, principal.organization_id)  # auth gate
    stmt = select(WebhookDelivery).where(
        WebhookDelivery.subscription_id == sub_id
    )
    if cursor:
        cur_ts, cur_id = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(WebhookDelivery.created_at, WebhookDelivery.id)
            < tuple_(cur_ts, cur_id)
        )
    stmt = stmt.order_by(
        WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc()
    ).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_cursor(last.created_at, last.id)
        rows = rows[:limit]
    return WebhookDeliveryListResponse(
        items=[WebhookDeliveryResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.post(
    "/{sub_id}/deliveries/{delivery_id}/redeliver",
    response_model=WebhookDeliveryResponse,
)
async def redeliver(
    sub_id: UUID,
    delivery_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookDeliveryResponse:
    await _load_owned(db, sub_id, principal.organization_id)
    stmt = select(WebhookDelivery).where(
        WebhookDelivery.id == delivery_id,
        WebhookDelivery.subscription_id == sub_id,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="delivery not found",
        )
    await db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == row.id)
        .values(
            status="pending",
            attempt=0,
            next_attempt_at=datetime.now(timezone.utc),
            response_status=None,
            response_body=None,
            succeeded_at=None,
        )
    )
    await db.commit()
    await db.refresh(row)
    return WebhookDeliveryResponse.model_validate(row)


async def _load_owned(
    db: AsyncSession, sub_id: UUID, org_id: UUID
) -> WebhookSubscription:
    stmt = select(WebhookSubscription).where(
        WebhookSubscription.id == sub_id,
        WebhookSubscription.organization_id == org_id,
    )
    sub = (await db.execute(stmt)).scalar_one_or_none()
    if sub is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="subscription not found",
        )
    return sub
```

- [ ] **Step 4: Mount + run**

In `api/hailhq/api/main.py`:

```python
from hailhq.api.routes import webhooks
# ...
app.include_router(webhooks.router)
```

Add `bcrypt` to `api/pyproject.toml` if not already present (likely is; check first with `rg bcrypt api/pyproject.toml core/pyproject.toml`).

Run: `cd api && uv run pytest tests/test_webhooks_api.py -v`
Expected: pass.

### Task 5.5: Commit Phase 5

```bash
git add api/migrations/versions/0008_webhook_subscriptions.py \
        core/hailhq/core/models.py core/hailhq/core/schemas.py \
        api/hailhq/api/routes/webhooks.py api/hailhq/api/main.py \
        core/tests/models/test_webhook_models.py \
        api/tests/test_webhooks_api.py
git commit -m "feat(api): webhook subscriptions + deliveries schema and CRUD

Org-wide webhook subscription registry (POST/GET/PATCH/DELETE on /webhooks,
plus rotate-secret + deliveries listing + redeliver). secret_hash is
bcrypt; plaintext returned once at create/rotate. webhook_deliveries
ships empty — the background worker that fills it lands in Phase 6."
```

---

## Phase 6 — Webhook signing + delivery worker

Goal: deliveries enqueued by other code get POSTed to their targets, with retries and auto-disable. Per-domain webhook fan-out and inbound integration land in Phase 8; this phase produces a worker that runs against any row in `webhook_deliveries`.

### Task 6.1: Signing + payload builder

**Files:**

- Create: `core/hailhq/core/webhooks.py`
- Create: `core/tests/test_webhooks.py`

- [ ] **Step 1: Failing tests**

```python
import hashlib
import hmac
import json

from hailhq.core.webhooks import (
    RETRY_SCHEDULE_SECONDS,
    build_event_payload,
    next_attempt_delay,
    sign_payload,
)


def test_sign_payload_uses_dotted_message():
    body = b'{"a":1}'
    secret = "topsecret"
    header = sign_payload(body, secret, timestamp=1717_000_000)
    assert header.startswith("t=1717000000,v1=")
    sig_hex = header.split("v1=")[1]
    expected = hmac.new(
        secret.encode(), b"1717000000." + body, hashlib.sha256
    ).hexdigest()
    assert sig_hex == expected


def test_build_event_payload_minimal():
    payload = build_event_payload(
        delivery_id="00000000-0000-0000-0000-000000000001",
        event_type="email.received",
        api_version="2026-06-06",
        organization_id="00000000-0000-0000-0000-000000000002",
        data={"id": "x"},
    )
    parsed = json.loads(payload)
    assert parsed["type"] == "email.received"
    assert parsed["api_version"] == "2026-06-06"
    assert parsed["data"] == {"id": "x"}


def test_retry_schedule_matches_spec():
    # 0s, 30s, 2m, 10m, 1h, 6h, 24h
    assert RETRY_SCHEDULE_SECONDS == [0, 30, 120, 600, 3600, 21600, 86400]


def test_next_attempt_delay_returns_each_slot():
    for i, expected in enumerate(RETRY_SCHEDULE_SECONDS):
        assert next_attempt_delay(i) == expected
    # past the end → None (dead)
    assert next_attempt_delay(len(RETRY_SCHEDULE_SECONDS)) is None
```

- [ ] **Step 2: Run (expect FAIL)**

Run: `cd core && uv run pytest tests/test_webhooks.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
"""Webhook signing, retry schedule, and payload assembly.

Signing is Stripe-style: t=<unix>,v1=<hex_hmac_sha256>, signed over
"t.body". The retry schedule is fixed (0, 30s, 2m, 10m, 1h, 6h, 24h)
for v1; per-subscription overrides land in a follow-up.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

__all__ = [
    "RETRY_SCHEDULE_SECONDS",
    "build_event_payload",
    "build_signature_header",
    "next_attempt_delay",
    "sign_payload",
]

RETRY_SCHEDULE_SECONDS: list[int] = [0, 30, 120, 600, 3600, 21600, 86400]


def sign_payload(body: bytes, secret: str, *, timestamp: int | None = None) -> str:
    """Return the value for the X-Hail-Signature header."""
    ts = timestamp if timestamp is not None else int(time.time())
    message = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def build_signature_header(body: bytes, secret: str) -> str:
    return sign_payload(body, secret)


def build_event_payload(
    *,
    delivery_id: str | UUID,
    event_type: str,
    api_version: str,
    organization_id: str | UUID,
    data: dict[str, Any],
    created_at: datetime | None = None,
) -> bytes:
    payload = {
        "id": str(delivery_id),
        "type": event_type,
        "api_version": api_version,
        "created_at": (created_at or datetime.now(timezone.utc)).isoformat(),
        "organization_id": str(organization_id),
        "data": data,
    }
    return json.dumps(payload, separators=(",", ":")).encode()


def next_attempt_delay(attempt_index: int) -> int | None:
    """Return seconds to wait before attempt N (0-indexed). None = dead."""
    if attempt_index >= len(RETRY_SCHEDULE_SECONDS):
        return None
    return RETRY_SCHEDULE_SECONDS[attempt_index]
```

- [ ] **Step 4: Run (expect PASS)**

Run: `cd core && uv run pytest tests/test_webhooks.py -v`
Expected: pass.

### Task 6.2: Delivery worker

**Files:**

- Create: `core/hailhq/core/webhook_worker.py`
- Create: `core/tests/test_webhook_worker.py`

- [ ] **Step 1: Failing tests**

```python
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from hailhq.core.models import WebhookDelivery, WebhookSubscription
from hailhq.core.webhook_worker import WebhookWorker, _next_delivery_state


def _mk_delivery(**kw) -> WebhookDelivery:
    return WebhookDelivery(
        id=uuid4(),
        organization_id=uuid4(),
        event_type="email.received",
        event_id=uuid4(),
        payload={"hello": "world"},
        attempt=0,
        status="pending",
        next_attempt_at=datetime.now(timezone.utc),
        **kw,
    )


def test_next_state_on_success_marks_succeeded():
    delivery = _mk_delivery()
    new_status, next_at, attempt = _next_delivery_state(delivery, ok=True)
    assert new_status == "succeeded"
    assert next_at is None
    assert attempt == 1


def test_next_state_on_failure_schedules_retry():
    delivery = _mk_delivery(attempt=0)
    new_status, next_at, attempt = _next_delivery_state(delivery, ok=False)
    assert new_status == "pending"
    assert attempt == 1
    assert next_at is not None
    assert next_at > datetime.now(timezone.utc)


def test_next_state_after_last_retry_is_dead():
    from hailhq.core.webhooks import RETRY_SCHEDULE_SECONDS

    delivery = _mk_delivery(attempt=len(RETRY_SCHEDULE_SECONDS))
    new_status, next_at, _ = _next_delivery_state(delivery, ok=False)
    assert new_status == "dead"
    assert next_at is None


@pytest.mark.asyncio
async def test_worker_processes_one_pending_delivery(
    async_session, sample_org, stub_http_target
):
    sub = WebhookSubscription(
        organization_id=sample_org.id,
        target_url=stub_http_target.url,
        secret_hash="$2b$12$abcdef",  # dummy bcrypt
        event_types=["email.received"],
    )
    async_session.add(sub)
    await async_session.commit()
    delivery = WebhookDelivery(
        organization_id=sample_org.id,
        subscription_id=sub.id,
        event_type="email.received",
        event_id=uuid4(),
        payload={"id": "evt_x"},
    )
    async_session.add(delivery)
    await async_session.commit()

    worker = WebhookWorker(
        session_factory=lambda: async_session,
        http_post=stub_http_target.post,
        plain_secret_resolver=lambda _sub_id: "plain",
    )
    await worker.tick()

    await async_session.refresh(delivery)
    assert delivery.status == "succeeded"
    assert stub_http_target.calls
```

(Provide a `stub_http_target` fixture in conftest that exposes `.url`, `.post`, `.calls`.)

- [ ] **Step 2: Run (expect FAIL)**

Run: `cd core && uv run pytest tests/test_webhook_worker.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
"""Background webhook delivery worker.

A single asyncio task in the api service polls pending deliveries,
claims them with SELECT ... FOR UPDATE SKIP LOCKED, posts, and updates
status. Concurrency capped by an asyncio.Semaphore.

The worker doesn't know how to mint payloads — Phase 7 (fan-out) writes
the row with payload + target_url already chosen. The worker only
delivers.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import WebhookDelivery, WebhookSubscription
from hailhq.core.webhooks import (
    RETRY_SCHEDULE_SECONDS,
    next_attempt_delay,
    sign_payload,
)

logger = logging.getLogger(__name__)

API_VERSION = "2026-06-06"
MAX_CONSECUTIVE_FAILURES = 50
RESPONSE_BODY_CAP = 4096
DEFAULT_CONCURRENCY = 32
POLL_BATCH = 100

HttpPostFn = Callable[
    [str, bytes, dict[str, str]], Awaitable[tuple[int, str]]
]


def _next_delivery_state(
    delivery: WebhookDelivery, *, ok: bool
) -> tuple[str, datetime | None, int]:
    attempt = delivery.attempt + 1
    if ok:
        return "succeeded", None, attempt
    delay = next_attempt_delay(delivery.attempt + 1)
    if delay is None:
        return "dead", None, attempt
    return (
        "pending",
        datetime.now(timezone.utc) + timedelta(seconds=delay),
        attempt,
    )


class WebhookWorker:
    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        http_post: HttpPostFn,
        plain_secret_resolver: Callable[[UUID | None], str | None],
        concurrency: int = DEFAULT_CONCURRENCY,
        poll_interval: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._http_post = http_post
        self._secret_for = plain_secret_resolver
        self._sem = asyncio.Semaphore(concurrency)
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.tick()
            except Exception:  # pragma: no cover
                logger.exception("webhook worker tick failed")
                processed = 0
            if not processed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

    async def tick(self) -> int:
        async with self._session_scope() as db:
            claimed = await self._claim_batch(db)
            for delivery in claimed:
                await self._sem.acquire()
                asyncio.create_task(self._deliver(delivery.id))
            return len(claimed)

    async def stop(self) -> None:
        self._stop.set()

    @asynccontextmanager
    async def _session_scope(self):
        sess = self._session_factory()
        try:
            yield sess
        finally:
            try:
                await sess.close()  # type: ignore[func-returns-value]
            except Exception:  # pragma: no cover
                pass

    async def _claim_batch(self, db: AsyncSession) -> list[WebhookDelivery]:
        stmt = (
            select(WebhookDelivery)
            .where(WebhookDelivery.status == "pending")
            .where(WebhookDelivery.next_attempt_at <= datetime.now(timezone.utc))
            .order_by(WebhookDelivery.next_attempt_at.asc())
            .limit(POLL_BATCH)
            .with_for_update(skip_locked=True)
        )
        rows = list((await db.execute(stmt)).scalars().all())
        if not rows:
            return []
        # Move to in-flight 'pending' with a far-future next_attempt_at so
        # other workers don't re-claim while we POST.
        now = datetime.now(timezone.utc)
        deferred = now + timedelta(minutes=10)
        for r in rows:
            await db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == r.id)
                .values(next_attempt_at=deferred)
            )
        await db.commit()
        return rows

    async def _deliver(self, delivery_id: UUID) -> None:
        try:
            async with self._session_scope() as db:
                row = (
                    await db.execute(
                        select(WebhookDelivery).where(
                            WebhookDelivery.id == delivery_id
                        )
                    )
                ).scalar_one()

                secret = self._secret_for(row.subscription_id)
                if secret is None:
                    await self._mark(db, row, ok=False, status_code=None, body="no secret")
                    return

                import json as _json

                body = _json.dumps(row.payload, separators=(",", ":")).encode()
                sig = sign_payload(body, secret)
                target_url = await self._resolve_target_url(db, row)
                if target_url is None:
                    await self._mark(db, row, ok=False, status_code=None, body="no target")
                    return
                headers = {
                    "Content-Type": "application/json",
                    "X-Hail-Signature": sig,
                    "X-Hail-Event": row.event_type,
                    "X-Hail-Delivery": str(row.id),
                }
                if row.subscription_id:
                    headers["X-Hail-Subscription"] = str(row.subscription_id)
                if row.email_domain_id:
                    headers["X-Hail-Email-Domain"] = str(row.email_domain_id)

                try:
                    status, resp_body = await self._http_post(
                        target_url, body, headers
                    )
                except Exception as exc:  # pragma: no cover
                    await self._mark(
                        db,
                        row,
                        ok=False,
                        status_code=None,
                        body=str(exc)[:RESPONSE_BODY_CAP],
                    )
                    return

                ok = 200 <= status < 300
                await self._mark(
                    db,
                    row,
                    ok=ok,
                    status_code=status,
                    body=(resp_body or "")[:RESPONSE_BODY_CAP],
                )
        finally:
            self._sem.release()

    async def _resolve_target_url(
        self, db: AsyncSession, row: WebhookDelivery
    ) -> str | None:
        if row.subscription_id:
            sub = (
                await db.execute(
                    select(WebhookSubscription).where(
                        WebhookSubscription.id == row.subscription_id
                    )
                )
            ).scalar_one_or_none()
            if sub is None or sub.status != "active":
                return None
            return sub.target_url
        if row.email_domain_id:
            from hailhq.core.models import EmailDomain

            dom = (
                await db.execute(
                    select(EmailDomain).where(
                        EmailDomain.id == row.email_domain_id
                    )
                )
            ).scalar_one_or_none()
            return dom.webhook_url if dom else None
        return None

    async def _mark(
        self,
        db: AsyncSession,
        row: WebhookDelivery,
        *,
        ok: bool,
        status_code: int | None,
        body: str,
    ) -> None:
        status, next_at, attempt = _next_delivery_state(row, ok=ok)
        values = {
            "status": status,
            "attempt": attempt,
            "response_status": status_code,
            "response_body": body,
        }
        if next_at is not None:
            values["next_attempt_at"] = next_at
        if status == "succeeded":
            values["succeeded_at"] = datetime.now(timezone.utc)
        await db.execute(
            update(WebhookDelivery).where(WebhookDelivery.id == row.id).values(**values)
        )
        if row.subscription_id is not None:
            await self._update_subscription_counters(
                db, row.subscription_id, ok=ok, status=status
            )
        await db.commit()

    async def _update_subscription_counters(
        self,
        db: AsyncSession,
        sub_id: UUID,
        *,
        ok: bool,
        status: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        if ok:
            await db.execute(
                update(WebhookSubscription)
                .where(WebhookSubscription.id == sub_id)
                .values(consecutive_failures=0, last_success_at=now)
            )
            return
        if status != "dead":
            await db.execute(
                update(WebhookSubscription)
                .where(WebhookSubscription.id == sub_id)
                .values(last_failure_at=now)
            )
            return
        # dead → bump consecutive_failures, maybe auto-disable
        sub = (
            await db.execute(
                select(WebhookSubscription).where(
                    WebhookSubscription.id == sub_id
                )
            )
        ).scalar_one()
        new_failures = sub.consecutive_failures + 1
        values: dict[str, Any] = {
            "consecutive_failures": new_failures,
            "last_failure_at": now,
        }
        if new_failures >= MAX_CONSECUTIVE_FAILURES:
            values["status"] = "disabled"
        await db.execute(
            update(WebhookSubscription).where(WebhookSubscription.id == sub_id).values(**values)
        )
```

- [ ] **Step 4: Run (expect PASS)**

Run: `cd core && uv run pytest tests/test_webhook_worker.py -v`
Expected: pass.

### Task 6.3: HTTP post adapter + private-network guard

**Files:**

- Create: `core/hailhq/core/http_post.py`
- Create: `core/tests/test_http_post.py`

- [ ] **Step 1: Failing tests**

```python
import asyncio

import httpx
import pytest

from hailhq.core.http_post import (
    PrivateNetworkBlockedError,
    httpx_post,
    is_private_url,
)


def test_is_private_url_localhost():
    assert is_private_url("http://localhost:8080/x")
    assert is_private_url("https://127.0.0.1/x")
    assert is_private_url("http://10.0.0.1/x")
    assert is_private_url("http://192.168.1.1/x")
    assert is_private_url("http://169.254.1.1/x")


def test_is_private_url_public():
    assert not is_private_url("https://api.example.com/x")


@pytest.mark.asyncio
async def test_httpx_post_blocks_private_by_default():
    with pytest.raises(PrivateNetworkBlockedError):
        await httpx_post(
            "http://127.0.0.1:8080/x",
            b"{}",
            {},
            allow_private_networks=False,
        )


@pytest.mark.asyncio
async def test_httpx_post_returns_status_and_body(httpx_mock):
    httpx_mock.add_response(status_code=204, text="")
    status, body = await httpx_post(
        "https://example.com/x", b"{}", {}, allow_private_networks=False
    )
    assert status == 204
    assert body == ""
```

(Uses pytest-httpx fixture — add `pytest-httpx` dev dep if not already present.)

- [ ] **Step 2: Implement**

```python
"""HTTP POST adapter for webhook deliveries.

Wraps httpx with the private-network guard so misconfigured tenants
can't aim a delivery at internal infrastructure. Self-hosters can
disable the guard via HAIL_WEBHOOK_ALLOW_PRIVATE_NETWORKS=true.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

__all__ = ["PrivateNetworkBlockedError", "httpx_post", "is_private_url"]


class PrivateNetworkBlockedError(RuntimeError):
    """Refused to POST: target resolves to a private / local address."""


def _is_private(host: str) -> bool:
    if host in {"localhost", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        except (socket.gaierror, ValueError):
            return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
    )


def is_private_url(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.hostname:
        return False
    return _is_private(parsed.hostname)


async def httpx_post(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    allow_private_networks: bool,
    timeout_seconds: float = 10.0,
) -> tuple[int, str]:
    if not allow_private_networks and is_private_url(url):
        raise PrivateNetworkBlockedError(url)
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.post(url, content=body, headers=headers)
        return resp.status_code, resp.text
```

- [ ] **Step 3: Run (expect PASS)**

Run: `cd core && uv run pytest tests/test_http_post.py -v`
Expected: pass.

### Task 6.4: Wire the worker into API startup

**Files:**

- Modify: `api/hailhq/api/main.py`
- Create: `api/hailhq/api/worker_lifespan.py`

- [ ] **Step 1: Write the lifespan helper**

```python
"""Webhook worker lifecycle hook for FastAPI.

Starts a single WebhookWorker on app startup and signals it to stop
on shutdown. Plaintext secrets needed for signing are not stored —
the lifespan registers a resolver that pulls them from an in-process
LRU cache populated on /webhooks creation / rotation.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import partial
from uuid import UUID

from fastapi import FastAPI

from hailhq.core.config import settings
from hailhq.core.db import session_scope
from hailhq.core.http_post import httpx_post
from hailhq.core.webhook_worker import WebhookWorker

logger = logging.getLogger(__name__)

_SECRET_CACHE: dict[UUID, str] = {}


def remember_secret(sub_id: UUID, plain: str) -> None:
    _SECRET_CACHE[sub_id] = plain


def forget_secret(sub_id: UUID) -> None:
    _SECRET_CACHE.pop(sub_id, None)


def _resolver(sub_id: UUID | None) -> str | None:
    if sub_id is None:
        return None
    return _SECRET_CACHE.get(sub_id)


async def _session_factory():
    async with session_scope() as s:
        return s


@asynccontextmanager
async def webhook_worker_lifespan(app: FastAPI):
    worker = WebhookWorker(
        session_factory=_session_factory,
        http_post=partial(
            httpx_post,
            allow_private_networks=settings.hail_webhook_allow_private_networks,
        ),
        plain_secret_resolver=_resolver,
    )
    task = asyncio.create_task(worker.run_forever())
    app.state.webhook_worker = worker
    try:
        yield
    finally:
        await worker.stop()
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
```

- [ ] **Step 2: Mount on the FastAPI app**

In `api/hailhq/api/main.py`, switch the `lifespan=` argument of `FastAPI(...)` to compose existing lifespans with this one — or, if no lifespan exists yet, set `app = FastAPI(lifespan=webhook_worker_lifespan)`.

In `api/hailhq/api/routes/webhooks.py`, after the bcrypt hash is computed on create/rotate, call `remember_secret(sub.id, secret)`. On DELETE, call `forget_secret(sub.id)`.

The cache is in-process; when the API restarts, the first failed delivery prompts a fetch through normal request flow (we cache on first successful read at create/rotate time only). This is fine for v1 — deliveries that miss the cache fail until secrets are rotated. Note this caveat in `docs/setup/aws-ses.md` ingest section.

- [ ] **Step 3: Smoke test**

Run: `cd api && uv run pytest tests/test_webhooks_api.py -v` and confirm the new fixture didn't break anything.

### Task 6.5: Commit Phase 6

```bash
git add core/hailhq/core/webhooks.py core/hailhq/core/webhook_worker.py \
        core/hailhq/core/http_post.py \
        api/hailhq/api/worker_lifespan.py api/hailhq/api/main.py \
        api/hailhq/api/routes/webhooks.py \
        core/tests/test_webhooks.py core/tests/test_webhook_worker.py \
        core/tests/test_http_post.py
git commit -m "feat(core): webhook delivery worker with signed POSTs and retries

Adds the asyncio worker that polls webhook_deliveries with SKIP LOCKED,
signs payloads (t=<unix>,v1=<hmac> Stripe-style), POSTs via httpx, and
schedules retries on the 0/30s/2m/10m/1h/6h/24h ladder. After the last
retry, the row is marked 'dead' and the subscription's consecutive_failures
increments — auto-disabled at 50. Private-network guard blocks tenants
from aiming deliveries at internal infrastructure unless
HAIL_WEBHOOK_ALLOW_PRIVATE_NETWORKS=true."
```

---

## Phase 7 — Forwarding (header rewrite + loop guards)

Goal: on inbound, if `email_domains.forward_to` is set, build a forwarded outbound `Email` row per target and enqueue it through the existing outbound send path. Loop guards in place. Per-domain webhook fan-out + org-wide subscription fan-out happen in Phase 8.

### Task 7.1: Header-rewrite builder

**Files:**

- Create: `core/hailhq/core/email_forwarding.py`
- Create: `core/tests/test_email_forwarding.py`

- [ ] **Step 1: Failing tests**

```python
from datetime import datetime, timezone
from uuid import uuid4

from hailhq.core.email_forwarding import (
    LoopDetected,
    build_forwarded,
    detect_loop,
)
from hailhq.core.email_mime import ParsedMime


def _parsed(message_id="<m1@example.com>") -> ParsedMime:
    return ParsedMime(
        from_address="alice@example.com",
        to_addresses=["bob+acme@mail.hail.so"],
        cc_addresses=[],
        subject="Hello",
        message_id=message_id,
        in_reply_to=None,
        references_ids=None,
        body_text="hi",
        body_html=None,
    )


def test_build_forwarded_rewrites_from_and_reply_to():
    inbound_id = uuid4()
    fwd = build_forwarded(
        parsed=_parsed(),
        target="team@acme.com",
        forwarder_address="forwarder+acme@mail.hail.so",
        inbound_id=inbound_id,
        hops=0,
    )
    assert fwd.from_address == "forwarder+acme@mail.hail.so"
    assert fwd.reply_to == "alice@example.com"
    assert fwd.to_addresses == ["team@acme.com"]
    assert fwd.subject.startswith("Fwd:")
    assert fwd.body_text and "Forwarded message" in fwd.body_text
    assert fwd.headers["X-Hail-Forwarded-From"] == "alice@example.com"
    assert fwd.headers["X-Hail-Original-Message-Id"] == "<m1@example.com>"
    assert fwd.headers["X-Hail-Inbound-Id"] == str(inbound_id)
    assert fwd.headers["X-Hail-Forward-Hops"] == "1"
    assert fwd.headers["Auto-Submitted"] == "auto-forwarded"


def test_build_forwarded_does_not_double_prefix_subject():
    parsed = _parsed()
    parsed.subject = "Fwd: already"
    fwd = build_forwarded(
        parsed=parsed,
        target="team@acme.com",
        forwarder_address="forwarder+acme@mail.hail.so",
        inbound_id=uuid4(),
        hops=0,
    )
    assert fwd.subject == "Fwd: already"


def test_detect_loop_rejects_base_domain_self_forward():
    with pytest.raises(LoopDetected):  # noqa: F821
        detect_loop(
            target="someone@mail.hail.so",
            hops=0,
            base_domain="mail.hail.so",
            max_hops=3,
        )


def test_detect_loop_rejects_at_max_hops():
    with pytest.raises(LoopDetected):  # noqa: F821
        detect_loop(
            target="ops@acme.com",
            hops=3,
            base_domain="mail.hail.so",
            max_hops=3,
        )


def test_detect_loop_passes_at_under_max():
    detect_loop(
        target="ops@acme.com",
        hops=2,
        base_domain="mail.hail.so",
        max_hops=3,
    )
```

Add `import pytest` at the top.

- [ ] **Step 2: Implement**

```python
"""Header-rewrite forwarding builder + loop guards.

Forwarded mail is sent as Hail's own address (SPF/DKIM aligned with the
forwarder domain). Reply-To carries the original sender so the forward
target's reply lands in the original conversation. The original
Message-ID is preserved in References to keep threading.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from hailhq.core.email_mime import ParsedMime

__all__ = [
    "Forwarded",
    "LoopDetected",
    "build_forwarded",
    "detect_loop",
]


class LoopDetected(RuntimeError):
    pass


@dataclass
class Forwarded:
    from_address: str
    to_addresses: list[str]
    reply_to: str
    subject: str
    body_text: str | None
    body_html: str | None
    headers: dict[str, str] = field(default_factory=dict)


_FWD_PREFIXES = ("fwd:", "fw:")


def _subject_with_prefix(subject: str) -> str:
    s = (subject or "").strip()
    if any(s.lower().startswith(p) for p in _FWD_PREFIXES):
        return s
    return f"Fwd: {s}".strip()


def _preamble(parsed: ParsedMime) -> str:
    lines = ["---------- Forwarded message ----------"]
    if parsed.from_address:
        lines.append(f"From: {parsed.from_address}")
    if parsed.subject:
        lines.append(f"Subject: {parsed.subject}")
    if parsed.to_addresses:
        lines.append(f"To: {', '.join(parsed.to_addresses)}")
    lines.append("")
    return "\n".join(lines)


def build_forwarded(
    *,
    parsed: ParsedMime,
    target: str,
    forwarder_address: str,
    inbound_id: UUID,
    hops: int,
) -> Forwarded:
    new_subject = _subject_with_prefix(parsed.subject)
    body_text = None
    if parsed.body_text is not None:
        body_text = _preamble(parsed) + parsed.body_text
    body_html = parsed.body_html  # leave untouched; consumer may concatenate
    headers = {
        "X-Hail-Forwarded-From": parsed.from_address or "",
        "X-Hail-Original-Message-Id": parsed.message_id or "",
        "X-Hail-Inbound-Id": str(inbound_id),
        "X-Hail-Forward-Hops": str(hops + 1),
        "Auto-Submitted": "auto-forwarded",
    }
    return Forwarded(
        from_address=forwarder_address,
        to_addresses=[target],
        reply_to=parsed.from_address or "",
        subject=new_subject,
        body_text=body_text,
        body_html=body_html,
        headers=headers,
    )


def detect_loop(
    *, target: str, hops: int, base_domain: str, max_hops: int
) -> None:
    if hops >= max_hops:
        raise LoopDetected(f"max forwarding hops ({max_hops}) reached")
    _, _, dom = target.rpartition("@")
    if dom.lower() == base_domain.lower():
        raise LoopDetected(
            f"forward target {target!r} is on hail-mail base domain"
        )
```

- [ ] **Step 3: Run (expect PASS)**

Run: `cd core && uv run pytest tests/test_email_forwarding.py -v`
Expected: pass.

### Task 7.2: Per-domain forward-rate limiter

**Files:**

- Create: `core/hailhq/core/forward_limiter.py`
- Create: `core/tests/test_forward_limiter.py`

- [ ] **Step 1: Failing test**

```python
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from hailhq.core.forward_limiter import ForwardLimiter
from hailhq.core.models import Email


@pytest.mark.asyncio
async def test_under_cap_allows(async_session, sample_org):
    limiter = ForwardLimiter(default_per_hour=10)
    allowed = await limiter.can_forward(
        async_session, organization_id=sample_org.id, override=None
    )
    assert allowed is True


@pytest.mark.asyncio
async def test_at_cap_denies(async_session, sample_org):
    # Insert 10 forwarded rows within the last hour
    for _ in range(10):
        async_session.add(
            Email(
                organization_id=sample_org.id,
                from_address="forwarder+acme@mail.hail.so",
                to_addresses=["x@example.com"],
                subject="Fwd: t",
                body_text="x",
                status="sent",
                provider="ses",
                direction="outbound",
                metadata_={"forwarded_from": str(uuid4())},
            )
        )
    await async_session.commit()
    limiter = ForwardLimiter(default_per_hour=10)
    allowed = await limiter.can_forward(
        async_session, organization_id=sample_org.id, override=None
    )
    assert allowed is False
```

- [ ] **Step 2: Implement**

```python
"""Per-domain forward rate cap.

Counts forwarded outbound rows in the last hour for the org (identified
by metadata.forwarded_from being populated). Cap is the email_domains
override, falling back to settings.hail_forward_rate_per_hour.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import Email

__all__ = ["ForwardLimiter"]


class ForwardLimiter:
    def __init__(self, *, default_per_hour: int) -> None:
        self._default = default_per_hour

    async def can_forward(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        override: int | None,
    ) -> bool:
        cap = override if override is not None else self._default
        if cap <= 0:
            return False
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        stmt = (
            select(func.count())
            .select_from(Email)
            .where(Email.organization_id == organization_id)
            .where(Email.direction == "outbound")
            .where(Email.created_at >= since)
            .where(Email.metadata_["forwarded_from"].astext.isnot(None))
        )
        used = (await db.execute(stmt)).scalar_one()
        return used < cap
```

- [ ] **Step 3: Run (expect PASS)**

Run: `cd core && uv run pytest tests/test_forward_limiter.py -v`
Expected: pass.

### Task 7.3: Wire forwarding into ingest

**Files:**

- Modify: `core/hailhq/core/email_ingest.py`
- Modify: `core/tests/test_email_ingest.py` (add forwarding tests)

- [ ] **Step 1: Add forwarding tests**

Append to `core/tests/test_email_ingest.py`:

```python
@pytest.mark.asyncio
async def test_forward_enqueues_outbound_per_target(
    async_session, sample_org, fake_s3, fake_outbound_queue
):
    domain = EmailDomain(
        organization_id=sample_org.id,
        kind="hail_mail",
        domain=f"alice+{sample_org.slug}@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org=sample_org.slug,
        verification_status="verified",
        provider="ses",
        inbound_enabled=True,
        forward_to=["ops@acme.com", "billing@acme.com"],
    )
    async_session.add(domain)
    await async_session.commit()

    fake_s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    msg = InboundMessage(
        provider_message_id="fwd1",
        envelope_from="alice@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/fwd1",
    )
    await ingest_inbound(
        async_session,
        message=msg,
        s3=fake_s3,
        hail_mail_base_domain="mail.hail.so",
        outbound_queue=fake_outbound_queue,
    )
    assert fake_outbound_queue.targets == ["ops@acme.com", "billing@acme.com"]


@pytest.mark.asyncio
async def test_loop_header_suppresses_forward(
    async_session, sample_org, fake_s3, fake_outbound_queue
):
    domain = EmailDomain(
        organization_id=sample_org.id,
        kind="hail_mail",
        domain=f"alice+{sample_org.slug}@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org=sample_org.slug,
        verification_status="verified",
        provider="ses",
        inbound_enabled=True,
        forward_to=["ops@acme.com"],
    )
    async_session.add(domain)
    await async_session.commit()

    # MIME with X-Hail-Forward-Hops at the cap.
    raw = (
        b"From: x@example.com\r\n"
        b"To: " + domain.domain.encode() + b"\r\n"
        b"Subject: loop\r\n"
        b"Message-ID: <loop1@example.com>\r\n"
        b"X-Hail-Forward-Hops: 3\r\n"
        b"\r\n"
        b"body"
    )
    fake_s3.fetch_raw.return_value = raw
    msg = InboundMessage(
        provider_message_id="loop1",
        envelope_from="x@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/loop1",
    )
    result = await ingest_inbound(
        async_session,
        message=msg,
        s3=fake_s3,
        hail_mail_base_domain="mail.hail.so",
        outbound_queue=fake_outbound_queue,
    )
    assert "forward_loop" in result.suppressed_reasons
    assert fake_outbound_queue.targets == []
```

`fake_outbound_queue` is a tiny conftest stub:

```python
@pytest.fixture
def fake_outbound_queue():
    class _Q:
        targets: list[str] = []

        async def enqueue(self, **kwargs):
            self.targets.append(kwargs["to"])

    return _Q()
```

- [ ] **Step 2: Implement forwarding in `ingest_inbound`**

In `core/hailhq/core/email_ingest.py`, change the signature to accept an `outbound_queue` callable + `settings`-derived knobs:

```python
from collections.abc import Awaitable, Callable

OutboundEnqueue = Callable[..., Awaitable[None]]


async def ingest_inbound(
    db: AsyncSession,
    *,
    message: InboundMessage,
    s3: S3InboundClient,
    hail_mail_base_domain: str,
    outbound_queue: OutboundEnqueue | None = None,
    forward_max_hops: int = 3,
    forward_rate_per_hour_default: int = 200,
) -> IngestResult:
    ...
```

Inside the per-recipient loop, after a row is persisted (the existing `_persist_one`):

```python
        if email_id is None or suppress is not None:
            continue
        # forward fan-out
        forwards = list(domain.forward_to or [])
        if not forwards or outbound_queue is None:
            continue
        # parse incoming Forward-Hops if present in the raw headers
        hops_header = parsed.from_address_headers.get("X-Hail-Forward-Hops") if hasattr(parsed, "from_address_headers") else None
        # Simpler — re-parse just the header from raw bytes
        from email import message_from_bytes
        m = message_from_bytes(raw)
        try:
            hops = int(m.get("X-Hail-Forward-Hops", "0"))
        except ValueError:
            hops = 0
        from hailhq.core.email_forwarding import LoopDetected, build_forwarded, detect_loop

        forwarder_address = f"forwarder+{domain.local_prefix_org}@{hail_mail_base_domain}"
        for target in forwards:
            try:
                detect_loop(
                    target=target,
                    hops=hops,
                    base_domain=hail_mail_base_domain,
                    max_hops=forward_max_hops,
                )
            except LoopDetected:
                result.suppressed_reasons.append("forward_loop")
                break
            fwd = build_forwarded(
                parsed=parsed,
                target=target,
                forwarder_address=forwarder_address,
                inbound_id=email_id,
                hops=hops,
            )
            await outbound_queue(
                organization_id=domain.organization_id,
                from_address=fwd.from_address,
                to=fwd.to_addresses[0],
                reply_to=fwd.reply_to,
                subject=fwd.subject,
                body_text=fwd.body_text,
                body_html=fwd.body_html,
                headers=fwd.headers,
                metadata={"forwarded_from": str(email_id), **fwd.headers},
            )
```

(Keep `raw` accessible — refactor so the raw bytes are fetched once at the top of `ingest_inbound`, not inside `_persist_one`.)

- [ ] **Step 3: Wire the real outbound queue in the API**

Where `/internal/ses-events` calls `ingest_inbound`, inject an outbound enqueue function backed by `Email` row inserts on the outbound side:

```python
# api/hailhq/api/routes/internal/ses_events.py (updated)
from hailhq.api.outbound_queue import enqueue_outbound  # new

result = await ingest_inbound(
    db,
    message=message,
    s3=s3,
    hail_mail_base_domain=settings.hail_mail_base_domain,
    outbound_queue=enqueue_outbound,
    forward_max_hops=settings.hail_forward_max_hops,
    forward_rate_per_hour_default=settings.hail_forward_rate_per_hour,
)
```

`api/hailhq/api/outbound_queue.py`:

```python
"""Queue interface for forward-driven outbound sends.

Synchronously inserts an Email row with status='queued'. The existing
outbound send loop in /emails handles the actual SES call; this is
just the insertion side.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import Email


async def enqueue_outbound(
    db: AsyncSession,
    *,
    organization_id: UUID,
    from_address: str,
    to: str,
    reply_to: str | None,
    subject: str,
    body_text: str | None,
    body_html: str | None,
    headers: dict[str, str],
    metadata: dict[str, str],
) -> None:
    email = Email(
        organization_id=organization_id,
        from_address=from_address,
        to_addresses=[to],
        reply_to=reply_to,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        status="queued",
        provider="ses",
        direction="outbound",
        metadata_={**metadata, "forward_headers": headers},
    )
    db.add(email)
    await db.flush()
```

(Note: the existing outbound send path is synchronous-per-request in POST /emails; for forwards, the row stays `queued` and is picked up by the new background worker added in the next sub-task. If the outbound surface stays synchronous-only in v1, append a small "queued outbound sender" worker that drains `Email` rows with `status='queued' AND direction='outbound'` — keep it parallel to the webhook worker. Add a TODO marker referencing this if you choose to defer it; otherwise implement now.)

Decision for this plan: implement a minimal `OutboundEmailWorker` in the same file as `webhook_worker.py` pattern, but only if there's no existing queue mechanism. Search first: `rg -n "status == \"queued\"" api/`. If you find one, hook into it; if not, add a tiny worker analogous to the webhook worker that polls `queued` outbound rows and calls `email_provider.send_email`.

For now, mark this as the canonical TODO inside `email_forwarding.py`:

```python
# NOTE: forwarded rows insert with status='queued' and rely on the
# outbound send loop to drain them. If no such loop exists yet, see
# api/hailhq/api/outbound_worker.py (added alongside this milestone).
```

- [ ] **Step 4: Run (expect PASS)**

Run: `cd core && uv run pytest tests/test_email_ingest.py -v && cd ../api && uv run pytest tests/test_internal_ses_events.py -v`
Expected: pass.

### Task 7.4: Commit Phase 7

```bash
git add core/hailhq/core/email_forwarding.py \
        core/hailhq/core/forward_limiter.py \
        core/hailhq/core/email_ingest.py \
        api/hailhq/api/outbound_queue.py \
        api/hailhq/api/routes/internal/ses_events.py \
        core/tests/test_email_forwarding.py \
        core/tests/test_forward_limiter.py \
        core/tests/test_email_ingest.py
git commit -m "feat(core): forward inbound mail via header-rewrite outbound

Adds the forwarding builder (Reply-To carries the original sender;
From becomes forwarder+<org>@<base_domain>; original Message-ID echoed
in References for threading), the loop guard (3-hop max via
X-Hail-Forward-Hops + reject targets on the hail-mail base domain), and
the per-org rate limiter. Wired into the ingest path so a configured
email_domains.forward_to triggers one outbound Email row per target
through the existing send loop."
```

---

## Phase 8 — Webhook fan-out + reading inbound

Goal: ingest enqueues both per-domain webhooks and org-wide subscription deliveries. Tenants can read inbound emails, the raw MIME, and attachments.

### Task 8.1: Fan-out service

**Files:**

- Create: `core/hailhq/core/webhook_fanout.py`
- Create: `core/tests/test_webhook_fanout.py`

- [ ] **Step 1: Failing test**

```python
import pytest
from sqlalchemy import select

from hailhq.core.models import EmailDomain, WebhookDelivery, WebhookSubscription
from hailhq.core.webhook_fanout import build_event_data, fanout_email_event


@pytest.mark.asyncio
async def test_fanout_creates_per_domain_and_subscription_deliveries(
    async_session, sample_org
):
    domain = EmailDomain(
        organization_id=sample_org.id,
        kind="hail_mail",
        domain="alice+acme@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org="acme",
        verification_status="verified",
        provider="ses",
        inbound_enabled=True,
        webhook_url="https://example.com/per-domain",
        webhook_secret_hash="$2b$12$xyz",
    )
    sub = WebhookSubscription(
        organization_id=sample_org.id,
        target_url="https://example.com/firehose",
        secret_hash="$2b$12$abc",
        event_types=["email.received"],
    )
    async_session.add_all([domain, sub])
    await async_session.commit()

    await fanout_email_event(
        async_session,
        organization_id=sample_org.id,
        email_domain_id=domain.id,
        event_type="email.received",
        event_id=domain.id,  # placeholder UUID
        data={"id": "evt"},
    )

    rows = (
        await async_session.execute(select(WebhookDelivery))
    ).scalars().all()
    targets = {(r.subscription_id, r.email_domain_id) for r in rows}
    assert (sub.id, None) in targets
    assert (None, domain.id) in targets
    assert len(rows) == 2


def test_build_event_data_includes_attachments():
    data = build_event_data(
        email_id="00000000-0000-0000-0000-000000000001",
        direction="inbound",
        from_address="a@b",
        to_addresses=["c@d"],
        subject="s",
        message_id="<m>",
        in_reply_to=None,
        verdicts={
            "spam_verdict": "PASS",
            "virus_verdict": "PASS",
            "spf_verdict": "PASS",
            "dkim_verdict": "PASS",
            "dmarc_verdict": "PASS",
        },
        raw_url="https://api.hail.so/emails/x/raw",
        attachments=[
            {
                "id": "a1",
                "filename": "f.pdf",
                "content_type": "application/pdf",
                "size_bytes": 10,
                "content_id": None,
                "url": "https://api.hail.so/emails/x/attachments/a1",
            }
        ],
    )
    assert data["attachments"][0]["filename"] == "f.pdf"
    assert data["spam_verdict"] == "PASS"
```

- [ ] **Step 2: Implement**

```python
"""Fan-out service: enqueue webhook_deliveries rows for an email event.

The worker (Phase 6) does the POSTing — this service only writes rows.
Per-domain webhook + matching org-wide subscriptions both get a row.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import EmailDomain, WebhookDelivery, WebhookSubscription

__all__ = ["build_event_data", "fanout_email_event"]

API_VERSION = "2026-06-06"


def build_event_data(
    *,
    email_id: str,
    direction: str,
    from_address: str,
    to_addresses: list[str],
    subject: str,
    message_id: str | None,
    in_reply_to: str | None,
    verdicts: dict[str, str | None],
    raw_url: str | None,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": email_id,
        "direction": direction,
        "from_address": from_address,
        "to_addresses": to_addresses,
        "subject": subject,
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        **verdicts,
        "raw_url": raw_url,
        "attachments": attachments,
    }


async def fanout_email_event(
    db: AsyncSession,
    *,
    organization_id: UUID,
    email_domain_id: UUID | None,
    event_type: str,
    event_id: UUID,
    data: dict[str, Any],
) -> int:
    """Insert delivery rows. Returns the number of rows inserted."""
    payloads: list[dict[str, Any]] = []

    # Per-domain webhook (if domain configured).
    if email_domain_id is not None:
        dom = (
            await db.execute(
                select(EmailDomain).where(EmailDomain.id == email_domain_id)
            )
        ).scalar_one_or_none()
        if dom is not None and dom.webhook_url and dom.inbound_enabled:
            payloads.append({"email_domain_id": dom.id, "subscription_id": None})

    # Org-wide subscriptions.
    subs = (
        await db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.organization_id == organization_id,
                WebhookSubscription.status == "active",
            )
        )
    ).scalars().all()
    for sub in subs:
        if event_type in (sub.event_types or []):
            payloads.append({"subscription_id": sub.id, "email_domain_id": None})

    for spec in payloads:
        db.add(
            WebhookDelivery(
                subscription_id=spec["subscription_id"],
                email_domain_id=spec["email_domain_id"],
                event_type=event_type,
                event_id=event_id,
                payload={
                    "type": event_type,
                    "api_version": API_VERSION,
                    "organization_id": str(organization_id),
                    "data": data,
                },
            )
        )
    await db.flush()
    return len(payloads)
```

- [ ] **Step 3: Run (expect PASS)**

Run: `cd core && uv run pytest tests/test_webhook_fanout.py -v`
Expected: pass.

### Task 8.2: Wire fan-out into ingest

**Files:**

- Modify: `core/hailhq/core/email_ingest.py`

- [ ] **Step 1: Update ingest signature + flow**

Add a `fanout` callable to `ingest_inbound`, defaulting to None:

```python
FanoutFn = Callable[..., Awaitable[None]]


async def ingest_inbound(
    db: AsyncSession,
    *,
    message: InboundMessage,
    s3: S3InboundClient,
    hail_mail_base_domain: str,
    outbound_queue: OutboundEnqueue | None = None,
    fanout: FanoutFn | None = None,
    ...
) -> IngestResult:
```

After each `email_id` is persisted (and not suppressed), call:

```python
if fanout is not None and suppress is None:
    presigned_base = ...  # passed in or constructed; see step 2
    raw_url = f"{api_base}/emails/{email_id}/raw"
    attachments_for_payload = [
        {
            "id": str(att.id),
            "filename": att.filename,
            "content_type": att.content_type,
            "size_bytes": att.size_bytes,
            "content_id": att.content_id,
            "url": f"{api_base}/emails/{email_id}/attachments/{att.id}",
        }
        for att in await _attachments_for(db, email_id)
    ]
    data = build_event_data(
        email_id=str(email_id),
        direction="inbound",
        from_address=parsed.from_address,
        to_addresses=parsed.to_addresses or list(message.envelope_recipients),
        subject=parsed.subject or "",
        message_id=parsed.message_id,
        in_reply_to=parsed.in_reply_to,
        verdicts={
            "spam_verdict": message.spam_verdict,
            "virus_verdict": message.virus_verdict,
            "spf_verdict": message.spf_verdict,
            "dkim_verdict": message.dkim_verdict,
            "dmarc_verdict": message.dmarc_verdict,
        },
        raw_url=raw_url,
        attachments=attachments_for_payload,
    )
    await fanout(
        db,
        organization_id=domain.organization_id,
        email_domain_id=domain.id,
        event_type="email.received",
        event_id=email_id,
        data=data,
    )
```

Pass the API base URL via a new keyword param (the API caller constructs URLs using `hailhq.core.urls.canonical_url`):

```python
async def ingest_inbound(
    ...,
    api_base_url: str,
    ...
):
```

- [ ] **Step 2: Update caller**

In `api/hailhq/api/routes/internal/ses_events.py`:

```python
from hailhq.core.urls import canonical_url
from hailhq.core.webhook_fanout import fanout_email_event

result = await ingest_inbound(
    db,
    message=message,
    s3=s3,
    hail_mail_base_domain=settings.hail_mail_base_domain,
    outbound_queue=enqueue_outbound,
    fanout=fanout_email_event,
    api_base_url=canonical_url(settings.hail_api_url),
    forward_max_hops=settings.hail_forward_max_hops,
    forward_rate_per_hour_default=settings.hail_forward_rate_per_hour,
)
```

(`hail_api_url` is another setting field — add to `Settings` if missing.)

- [ ] **Step 3: Update tests**

Add to `core/tests/test_email_ingest.py`:

```python
@pytest.mark.asyncio
async def test_ingest_calls_fanout(
    async_session, sample_org, fake_s3
):
    domain = EmailDomain(
        organization_id=sample_org.id,
        kind="hail_mail",
        domain=f"alice+{sample_org.slug}@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org=sample_org.slug,
        verification_status="verified",
        provider="ses",
        inbound_enabled=True,
        webhook_url="https://example.com/x",
        webhook_secret_hash="$2b$12$abc",
    )
    async_session.add(domain)
    await async_session.commit()

    fake_s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    msg = InboundMessage(
        provider_message_id="fo1",
        envelope_from="a@b",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/fo1",
    )
    calls = []

    async def fanout(db, **kw):
        calls.append(kw)

    await ingest_inbound(
        async_session,
        message=msg,
        s3=fake_s3,
        hail_mail_base_domain="mail.hail.so",
        fanout=fanout,
        api_base_url="https://api.hail.so",
    )
    assert len(calls) == 1
    assert calls[0]["event_type"] == "email.received"
    assert calls[0]["data"]["raw_url"].startswith("https://api.hail.so/emails/")
```

Run: `cd core && uv run pytest tests/test_email_ingest.py tests/test_webhook_fanout.py -v`
Expected: pass.

### Task 8.3: Reading endpoints — direction filter, /raw, /attachments

**Files:**

- Modify: `api/hailhq/api/routes/emails.py`

- [ ] **Step 1: Failing tests**

`api/tests/test_emails_inbound_reads.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_list_filter_by_direction(
    api_client, auth_headers, seeded_inbound, seeded_outbound
):
    r = await api_client.get(
        "/emails?direction=inbound", headers=auth_headers
    )
    assert r.status_code == 200
    ids = [e["id"] for e in r.json()["items"]]
    assert seeded_inbound.id in ids
    assert seeded_outbound.id not in ids


@pytest.mark.asyncio
async def test_raw_redirects_for_inbound(api_client, auth_headers, seeded_inbound):
    r = await api_client.get(
        f"/emails/{seeded_inbound.id}/raw",
        headers=auth_headers,
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["Location"].startswith("https://")


@pytest.mark.asyncio
async def test_raw_404_for_outbound(api_client, auth_headers, seeded_outbound):
    r = await api_client.get(
        f"/emails/{seeded_outbound.id}/raw",
        headers=auth_headers,
        follow_redirects=False,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_attachment_redirects(
    api_client, auth_headers, seeded_inbound_with_attachment
):
    e, att = seeded_inbound_with_attachment
    r = await api_client.get(
        f"/emails/{e.id}/attachments/{att.id}",
        headers=auth_headers,
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["Location"].startswith("https://")
```

- [ ] **Step 2: Implement**

In `api/hailhq/api/routes/emails.py`:

- Add `direction: Literal["outbound","inbound"] | None = Query(default=None)` to `list_emails` and apply `.where(Email.direction == direction)` when set.
- Add new routes:

```python
@router.get("/{email_id}/raw")
async def get_raw(
    email_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3InboundClient, Depends(_s3_client)],
) -> Response:
    stmt = select(Email).where(
        Email.id == email_id,
        Email.organization_id == principal.organization_id,
    )
    email = (await db.execute(stmt)).scalar_one_or_none()
    if email is None or email.direction != "inbound" or not email.raw_s3_key:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="raw MIME not available",
        )
    url = await s3.presign_get(email.raw_s3_key, ttl_seconds=300)
    return RedirectResponse(url=url, status_code=302)


@router.get("/{email_id}/attachments/{attachment_id}")
async def get_attachment(
    email_id: UUID,
    attachment_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3InboundClient, Depends(_s3_client)],
) -> Response:
    stmt = (
        select(EmailAttachment, Email)
        .join(Email, Email.id == EmailAttachment.email_id)
        .where(EmailAttachment.id == attachment_id)
        .where(Email.id == email_id)
        .where(Email.organization_id == principal.organization_id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="attachment not found",
        )
    att = row[0]
    url = await s3.presign_get(att.s3_key, ttl_seconds=300)
    return RedirectResponse(url=url, status_code=302)


def _s3_client() -> S3InboundClient:
    return S3InboundClient(bucket=settings.hail_inbound_bucket)
```

Imports to add: `from fastapi.responses import RedirectResponse`, `from hailhq.core.models import EmailAttachment`, `from hailhq.core.s3_inbound import S3InboundClient`, `from hailhq.core.config import settings`.

- [ ] **Step 3: Extend `EmailResponse` serialization**

When building an `EmailResponse` for inbound rows, fill `raw_url` and `attachments`:

```python
def _serialize(email: Email, attachments: list[EmailAttachment], api_base: str):
    response = EmailResponse.model_validate(email)
    if email.direction == "inbound":
        response = response.model_copy(
            update={
                "raw_url": f"{api_base}/emails/{email.id}/raw",
                "attachments": [
                    EmailAttachmentResponse(
                        id=att.id,
                        filename=att.filename,
                        content_type=att.content_type,
                        size_bytes=att.size_bytes,
                        content_id=att.content_id,
                        url=f"{api_base}/emails/{email.id}/attachments/{att.id}",
                    )
                    for att in attachments
                ],
            }
        )
    return response
```

Use this helper in `get_email` and `list_emails`. Compute `api_base` via `canonical_url(settings.hail_api_url)`.

- [ ] **Step 4: Run (expect PASS)**

Run: `cd api && uv run pytest tests/test_emails_inbound_reads.py -v`
Expected: pass.

### Task 8.4: PATCH `/email-domains/{id}` action fields + rotate-secret

**Files:**

- Modify: `api/hailhq/api/routes/email_domains.py`

- [ ] **Step 1: Failing test**

`api/tests/test_email_domains_inbound_patch.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_patch_sets_forward_and_webhook(
    api_client, auth_headers, hail_mail_domain
):
    r = await api_client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={
            "inbound_enabled": True,
            "forward_to": ["ops@acme.com"],
            "webhook_url": "https://example.com/in",
        },
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["forward_to"] == ["ops@acme.com"]
    assert r.json()["webhook_url"] == "https://example.com/in"
    # secret was generated implicitly when webhook_url was set
    rotate = await api_client.post(
        f"/email-domains/{hail_mail_domain.id}/rotate-webhook-secret",
        headers=auth_headers,
    )
    assert rotate.status_code == 200
    assert rotate.json()["webhook_secret"].startswith("whd_")


@pytest.mark.asyncio
async def test_inbound_action_required_when_enabled(
    api_client, auth_headers, hail_mail_domain
):
    r = await api_client.patch(
        f"/email-domains/{hail_mail_domain.id}",
        json={"inbound_enabled": True},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert "forward" in r.json()["detail"].lower() or "webhook" in r.json()["detail"].lower()
```

- [ ] **Step 2: Implement**

In `api/hailhq/api/routes/email_domains.py`, add the PATCH:

```python
@router.patch("/{domain_id}", response_model=EmailDomainResponse)
async def patch_email_domain(
    domain_id: UUID,
    body: EmailDomainPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> EmailDomainResponse:
    sd = await _load_owned(db, domain_id, principal.organization_id)

    updates: dict = {}
    if body.inbound_enabled is not None:
        updates["inbound_enabled"] = body.inbound_enabled
    if body.forward_to is not None:
        updates["forward_to"] = body.forward_to or None
    if body.webhook_url is not None:
        updates["webhook_url"] = body.webhook_url or None
        if body.webhook_url:
            from hailhq.api.routes.webhooks import _hash, _new_secret

            secret = "whd_" + secrets.token_urlsafe(24)
            updates["webhook_secret_hash"] = _hash(secret)
            remember_secret(sd.id, secret)  # piggyback on the worker's cache
    if body.forward_rate_per_hour is not None:
        updates["forward_rate_per_hour"] = body.forward_rate_per_hour

    final_enabled = updates.get("inbound_enabled", sd.inbound_enabled)
    final_forward = updates.get("forward_to", sd.forward_to)
    final_webhook = updates.get("webhook_url", sd.webhook_url)
    if final_enabled and not (final_forward or final_webhook):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="inbound_enabled requires forward_to or webhook_url",
        )

    if updates:
        await db.execute(
            update(EmailDomain).where(EmailDomain.id == sd.id).values(**updates)
        )
        await db.commit()
        await db.refresh(sd)
    return EmailDomainResponse.model_validate(sd)


@router.post(
    "/{domain_id}/rotate-webhook-secret",
    response_model=dict,
)
async def rotate_webhook_secret(
    domain_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    sd = await _load_owned(db, domain_id, principal.organization_id)
    if not sd.webhook_url:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="domain has no webhook_url configured",
        )
    from hailhq.api.routes.webhooks import _hash

    secret = "whd_" + secrets.token_urlsafe(24)
    await db.execute(
        update(EmailDomain)
        .where(EmailDomain.id == sd.id)
        .values(webhook_secret_hash=_hash(secret))
    )
    await db.commit()
    return {"webhook_secret": secret}
```

(Add `_load_owned`, `secrets`, and `update`/`UUID` imports as needed.)

- [ ] **Step 3: Cache the secret for the worker**

Per-domain webhook secrets share the same in-process cache as subscription secrets. Update `remember_secret` to accept either a subscription id or domain id:

```python
# api/hailhq/api/worker_lifespan.py
_SECRET_CACHE: dict[UUID, str] = {}  # keyed by either sub.id or email_domain.id


def remember_secret(owner_id: UUID, plain: str) -> None:
    _SECRET_CACHE[owner_id] = plain
```

And update the worker's `_resolver`:

```python
def _resolver(owner_id: UUID | None) -> str | None:
    if owner_id is None:
        return None
    return _SECRET_CACHE.get(owner_id)
```

In `webhook_worker.py`, pass the right id:

```python
if row.subscription_id is not None:
    secret = self._secret_for(row.subscription_id)
else:
    secret = self._secret_for(row.email_domain_id)
```

- [ ] **Step 4: Run (expect PASS)**

Run: `cd api && uv run pytest tests/test_email_domains_inbound_patch.py -v`
Expected: pass.

### Task 8.5: Commit Phase 8

```bash
git add core/hailhq/core/webhook_fanout.py core/hailhq/core/email_ingest.py \
        api/hailhq/api/routes/emails.py api/hailhq/api/routes/email_domains.py \
        api/hailhq/api/routes/internal/ses_events.py \
        api/hailhq/api/worker_lifespan.py core/hailhq/core/webhook_worker.py \
        core/tests/test_webhook_fanout.py core/tests/test_email_ingest.py \
        api/tests/test_emails_inbound_reads.py \
        api/tests/test_email_domains_inbound_patch.py
git commit -m "feat(api): webhook fan-out and inbound read endpoints

Inbound ingest now fans out per-domain webhook + matching org-wide
subscription deliveries (via webhook_deliveries rows; worker delivers).
Tenants can list with ?direction=inbound, fetch the raw MIME via
GET /emails/{id}/raw (302 → presigned S3), and download attachments
via GET /emails/{id}/attachments/{aid}. /email-domains PATCH accepts
inbound_enabled, forward_to, webhook_url, forward_rate_per_hour;
POST /email-domains/{id}/rotate-webhook-secret returns plaintext once."
```

---

## Phase 9 — Terraform module + Lambda handler

Goal: an operator runs `terraform apply` and gets a working SES → S3 + Lambda → API pipeline. The Lambda handler is stdlib-only, unit-tested.

### Task 9.1: Lambda handler

**Files:**

- Create: `infra/ses-ingest-lambda/handler.py`
- Create: `infra/ses-ingest-lambda/test_handler.py`
- Create: `infra/ses-ingest-lambda/README.md`

- [ ] **Step 1: Failing test**

```python
# infra/ses-ingest-lambda/test_handler.py
import hashlib
import hmac
import json
import os
from unittest.mock import patch

import handler


SES_EVENT = {
    "Records": [
        {
            "ses": {
                "mail": {
                    "messageId": "abc123",
                    "source": "alice@example.com",
                    "timestamp": "2026-06-06T10:11:12Z",
                },
                "receipt": {
                    "recipients": ["bob+acme@mail.hail.so"],
                    "spamVerdict": {"status": "PASS"},
                    "virusVerdict": {"status": "PASS"},
                    "spfVerdict": {"status": "PASS"},
                    "dkimVerdict": {"status": "PASS"},
                    "dmarcVerdict": {"status": "PASS"},
                },
            }
        }
    ]
}


def test_handler_posts_signed_payload(monkeypatch):
    monkeypatch.setenv("HAIL_API_URL", "https://api.example.com")
    monkeypatch.setenv("HAIL_INBOUND_BUCKET", "hail-inbound")
    monkeypatch.setenv("HAIL_INBOUND_HMAC_SECRET", "s3cret")

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b""

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["headers"] = dict(req.headers)
        return FakeResp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        handler.handler(SES_EVENT, None)

    assert captured["url"] == "https://api.example.com/internal/ses-events"
    payload = json.loads(captured["body"])
    assert payload["message_id"] == "abc123"
    assert payload["envelope_from"] == "alice@example.com"
    assert payload["recipients"] == ["bob+acme@mail.hail.so"]
    assert payload["verdicts"]["spam"] == "PASS"
    assert payload["s3_bucket"] == "hail-inbound"
    assert payload["s3_key"] == "raw/abc123"

    expected = hmac.new(
        b"s3cret", captured["body"], hashlib.sha256
    ).hexdigest()
    # urllib lowercases header keys.
    assert captured["headers"].get("X-hail-signature") == f"sha256={expected}"
```

- [ ] **Step 2: Implement**

`infra/ses-ingest-lambda/handler.py`:

```python
"""SES Receipt Rule Lambda — translates an SES event into a signed
POST to the Hail API.

Pure stdlib. Deploy artifact is a zip of this single file.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.request

API_PATH = "/internal/ses-events"


def _make_payload(record: dict) -> dict:
    mail = record["mail"]
    receipt = record["receipt"]

    def _verdict(name: str) -> str | None:
        v = receipt.get(name) or {}
        return v.get("status")

    return {
        "message_id": mail["messageId"],
        "envelope_from": mail["source"],
        "recipients": list(receipt.get("recipients") or []),
        "verdicts": {
            "spam": _verdict("spamVerdict"),
            "virus": _verdict("virusVerdict"),
            "spf": _verdict("spfVerdict"),
            "dkim": _verdict("dkimVerdict"),
            "dmarc": _verdict("dmarcVerdict"),
        },
        "s3_bucket": os.environ["HAIL_INBOUND_BUCKET"],
        "s3_key": f"raw/{mail['messageId']}",
        "timestamp": mail.get("timestamp"),
    }


def handler(event: dict, _context) -> dict:
    record = event["Records"][0]["ses"]
    payload = _make_payload(record)
    body = json.dumps(payload, separators=(",", ":")).encode()

    secret = os.environ["HAIL_INBOUND_HMAC_SECRET"].encode()
    sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
    url = os.environ["HAIL_API_URL"].rstrip("/") + API_PATH

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hail-Signature": f"sha256={sig}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
    return {"status": "ok"}
```

`infra/ses-ingest-lambda/README.md`:

````markdown
# ses-ingest-lambda

Bridges SES Receipt Rule notifications into Hail's HMAC-signed
`/internal/ses-events` endpoint. Pure stdlib. Deployed by the
Terraform module in `../terraform/`.

## Env

| Variable                   | Description                                        |
| -------------------------- | -------------------------------------------------- |
| `HAIL_API_URL`             | Base URL of the Hail API (no trailing slash).      |
| `HAIL_INBOUND_BUCKET`      | S3 bucket the SES Receipt Rule writes raw MIME to. |
| `HAIL_INBOUND_HMAC_SECRET` | Shared secret with the API.                        |

## Test

```bash
cd infra/ses-ingest-lambda
python -m pytest test_handler.py -v
```
````

````

- [ ] **Step 3: Run (expect PASS)**

Run: `cd infra/ses-ingest-lambda && python -m pytest test_handler.py -v`
Expected: pass.

### Task 9.2: Terraform module — variables and providers

**Files:**
- Create: `infra/terraform/versions.tf`
- Create: `infra/terraform/variables.tf`
- Create: `infra/terraform/main.tf`
- Create: `infra/terraform/hail.tfvars.example`

- [ ] **Step 1: Write the files**

`infra/terraform/versions.tf`:

```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
````

`infra/terraform/variables.tf`:

```hcl
variable "name_prefix" {
  description = "Prefix for AWS resources (e.g. hail-inbound-prod)."
  type        = string
}

variable "region" {
  description = "AWS region for SES inbound. SES inbound is only available in select regions."
  type        = string
}

variable "hail_api_url" {
  description = "Base URL of the Hail API (e.g. https://api.hail.so)."
  type        = string
}

variable "hail_inbound_hmac_secret" {
  description = "Shared secret between the Lambda and the API."
  type        = string
  sensitive   = true
}

variable "hail_mail_base_domain" {
  description = "Base domain mail will be received on (e.g. mail.hail.so)."
  type        = string
}

variable "raw_object_expiration_days" {
  description = "Lifecycle expiration on raw MIME objects."
  type        = number
  default     = 90
}
```

`infra/terraform/main.tf`:

```hcl
provider "aws" {
  region = var.region
}

locals {
  bucket_name        = "${var.name_prefix}-raw"
  rule_set_name      = "${var.name_prefix}-rules"
  rule_name          = "${var.name_prefix}-deliver"
  lambda_name        = "${var.name_prefix}-ingest"
  log_group_name     = "/aws/lambda/${local.lambda_name}"
}
```

`infra/terraform/hail.tfvars.example`:

```hcl
name_prefix              = "hail-inbound-prod"
region                   = "us-east-1"
hail_api_url             = "https://api.hail.so"
hail_inbound_hmac_secret = "REPLACE_WITH_RANDOM_64_HEX"
hail_mail_base_domain    = "mail.hail.so"
```

### Task 9.3: S3 bucket

**Files:**

- Create: `infra/terraform/s3_inbound.tf`

- [ ] **Step 1: Write**

```hcl
resource "aws_s3_bucket" "inbound" {
  bucket = local.bucket_name
}

resource "aws_s3_bucket_public_access_block" "inbound" {
  bucket                  = aws_s3_bucket.inbound.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "inbound" {
  bucket = aws_s3_bucket.inbound.id
  rule {
    id     = "expire-raw"
    status = "Enabled"
    filter { prefix = "raw/" }
    expiration { days = var.raw_object_expiration_days }
  }
  rule {
    id     = "expire-attachments"
    status = "Enabled"
    filter { prefix = "attachments/" }
    expiration { days = var.raw_object_expiration_days }
  }
}

data "aws_iam_policy_document" "ses_write" {
  statement {
    sid     = "AllowSESPuts"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.inbound.arn}/raw/*"]
    principals {
      type        = "Service"
      identifiers = ["ses.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:Referer"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket_policy" "inbound" {
  bucket = aws_s3_bucket.inbound.id
  policy = data.aws_iam_policy_document.ses_write.json
}
```

### Task 9.4: Lambda + IAM

**Files:**

- Create: `infra/terraform/lambda_ingest.tf`

- [ ] **Step 1: Write**

```hcl
data "archive_file" "ingest" {
  type        = "zip"
  source_dir  = "${path.module}/../ses-ingest-lambda"
  output_path = "${path.module}/.build/ses-ingest-lambda.zip"
  excludes    = ["test_handler.py", "README.md"]
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = local.log_group_name
  retention_in_days = 30
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ingest" {
  name               = "${local.lambda_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "ingest_logs" {
  role       = aws_iam_role.ingest.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "ingest" {
  function_name    = local.lambda_name
  role             = aws_iam_role.ingest.arn
  filename         = data.archive_file.ingest.output_path
  source_code_hash = data.archive_file.ingest.output_base64sha256
  runtime          = "python3.12"
  handler          = "handler.handler"
  timeout          = 15
  memory_size      = 256

  environment {
    variables = {
      HAIL_API_URL             = var.hail_api_url
      HAIL_INBOUND_BUCKET      = aws_s3_bucket.inbound.bucket
      HAIL_INBOUND_HMAC_SECRET = var.hail_inbound_hmac_secret
    }
  }
}

resource "aws_lambda_permission" "ses_invoke" {
  statement_id  = "AllowSESInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "ses.amazonaws.com"
  source_account = data.aws_caller_identity.current.account_id
}
```

### Task 9.5: SES Receipt Rule

**Files:**

- Create: `infra/terraform/ses_inbound.tf`

- [ ] **Step 1: Write**

```hcl
# SES has a single active receipt rule set per region per account.
# We create our own rule set but DO NOT activate it. Activation is
# a manual operator step — see docs/setup/aws-ses.md.
resource "aws_ses_receipt_rule_set" "hail" {
  rule_set_name = local.rule_set_name
}

resource "aws_ses_receipt_rule" "hail" {
  name          = local.rule_name
  rule_set_name = aws_ses_receipt_rule_set.hail.rule_set_name
  enabled       = true
  scan_enabled  = true
  recipients    = [var.hail_mail_base_domain]
  tls_policy    = "Require"

  s3_action {
    bucket_name       = aws_s3_bucket.inbound.bucket
    object_key_prefix = "raw/"
    position          = 1
  }

  lambda_action {
    function_arn    = aws_lambda_function.ingest.arn
    invocation_type = "Event"
    position        = 2
  }

  depends_on = [
    aws_s3_bucket_policy.inbound,
    aws_lambda_permission.ses_invoke,
  ]
}
```

### Task 9.6: Outputs

**Files:**

- Create: `infra/terraform/outputs.tf`

- [ ] **Step 1: Write**

```hcl
output "inbound_mx_record" {
  description = "Publish at DNS for the hail-mail base domain."
  value       = "10 inbound-smtp.${var.region}.amazonaws.com"
}

output "inbound_bucket" {
  value = aws_s3_bucket.inbound.bucket
}

output "lambda_function_arn" {
  value = aws_lambda_function.ingest.arn
}

output "receipt_rule_set_name" {
  description = "Activate manually: aws sesv2 set-active-receipt-rule-set --rule-set-name <this>"
  value       = aws_ses_receipt_rule_set.hail.rule_set_name
}

output "activate_command" {
  value = "aws sesv2 set-active-receipt-rule-set --rule-set-name ${aws_ses_receipt_rule_set.hail.rule_set_name}"
}
```

### Task 9.7: Validate

- [ ] **Step 1: terraform fmt + validate**

```bash
cd infra/terraform
terraform fmt -check
terraform init -backend=false
terraform validate
```

Expected: no syntax issues; the planner can resolve all references. (You won't `terraform plan` against a real AWS account here — that's the operator's step at deploy time.)

### Task 9.8: Commit Phase 9

```bash
git add infra/
git commit -m "feat(infra): SES inbound Terraform module + ingest Lambda

Stdlib-only Lambda translates SES events into HMAC-signed POSTs to
/internal/ses-events. Terraform module provisions the raw-MIME S3 bucket
(lifecycle: 90d), the SES Receipt Rule + Rule Set (NOT activated by
default — see docs/setup/aws-ses.md), the Lambda + IAM role + log group,
and outputs the MX record the operator publishes at DNS."
```

---

## Phase 10 — OpenAPI regen + CLI commands

Goal: every new HTTP route has a CLI mirror, and the OpenAPI document plus generated client are in lockstep.

### Task 10.1: Regenerate OpenAPI

**Files:**

- Modify: `openapi/openapi.yaml`

- [ ] **Step 1: Run the export command**

Run the repo's OpenAPI export (commonly `cd api && uv run python -m hailhq.api.openapi > ../openapi/openapi.yaml` — confirm by `rg -n 'openapi' api/pyproject.toml api/hailhq/api/*.py`).

- [ ] **Step 2: Diff and sanity-check**

Run: `git diff openapi/openapi.yaml | head -200`
Expected new entries: `/email-domains/{id}` PATCH + `/rotate-webhook-secret`; `/emails/{id}/raw` + `/emails/{id}/attachments/{aid}`; full `/webhooks` CRUD + `/{id}/rotate-secret` + `/{id}/deliveries` + redeliver. The `direction` query param surfaces on `GET /emails`. `/internal/ses-events` should NOT appear (it has `include_in_schema=False`).

### Task 10.2: Regenerate CLI client + add commands

**Files:**

- Modify: `cli/internal/client/client.gen.go`
- Create: `cli/internal/cmd/webhooks.go`
- Create: `cli/internal/cmd/webhooks_test.go`
- Modify: `cli/internal/cmd/email.go` (add `--direction`)
- Modify: `cli/internal/cmd/email_domain.go` (new subcommands)

- [ ] **Step 1: Regenerate the typed client**

Run: `cd cli && go generate ./...`
Expected: `client.gen.go` updated with the new operations.

- [ ] **Step 2: Add `hail webhooks` subcommand**

In `cli/internal/cmd/webhooks.go`:

```go
package cmd

import (
    "encoding/json"
    "fmt"
    "strings"

    "github.com/spf13/cobra"
)

var webhooksCmd = &cobra.Command{
    Use:   "webhooks",
    Short: "Manage webhook subscriptions",
}

var webhooksCreateURL string
var webhooksCreateEvents string

var webhooksCreateCmd = &cobra.Command{
    Use:   "create",
    Short: "Create a webhook subscription",
    RunE: func(cmd *cobra.Command, args []string) error {
        if webhooksCreateURL == "" {
            return fmt.Errorf("--url is required")
        }
        events := strings.Split(webhooksCreateEvents, ",")
        client, err := newClient()
        if err != nil {
            return err
        }
        body := map[string]any{
            "target_url":  webhooksCreateURL,
            "event_types": events,
        }
        resp, err := client.PostWebhooks(cmd.Context(), body)
        if err != nil {
            return err
        }
        return json.NewEncoder(cmd.OutOrStdout()).Encode(resp)
    },
}

var webhooksListCmd = &cobra.Command{
    Use:   "list",
    Short: "List webhook subscriptions",
    RunE: func(cmd *cobra.Command, args []string) error {
        client, err := newClient()
        if err != nil {
            return err
        }
        resp, err := client.GetWebhooks(cmd.Context(), nil)
        if err != nil {
            return err
        }
        return json.NewEncoder(cmd.OutOrStdout()).Encode(resp)
    },
}

var webhooksRedeliverID string

var webhooksDeliveriesCmd = &cobra.Command{
    Use:   "deliveries <subscription-id>",
    Short: "List delivery attempts for a subscription",
    Args:  cobra.ExactArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        client, err := newClient()
        if err != nil {
            return err
        }
        resp, err := client.GetWebhooksSubIdDeliveries(cmd.Context(), args[0], nil)
        if err != nil {
            return err
        }
        return json.NewEncoder(cmd.OutOrStdout()).Encode(resp)
    },
}

var webhooksRedeliverCmd = &cobra.Command{
    Use:   "redeliver <subscription-id> <delivery-id>",
    Short: "Replay a webhook delivery",
    Args:  cobra.ExactArgs(2),
    RunE: func(cmd *cobra.Command, args []string) error {
        client, err := newClient()
        if err != nil {
            return err
        }
        resp, err := client.PostWebhooksSubIdDeliveriesDeliveryIdRedeliver(
            cmd.Context(), args[0], args[1],
        )
        if err != nil {
            return err
        }
        return json.NewEncoder(cmd.OutOrStdout()).Encode(resp)
    },
}

func init() {
    webhooksCreateCmd.Flags().StringVar(&webhooksCreateURL, "url", "", "Target URL")
    webhooksCreateCmd.Flags().StringVar(
        &webhooksCreateEvents, "events", "email.received",
        "Comma-separated event types",
    )
    webhooksCmd.AddCommand(webhooksCreateCmd, webhooksListCmd, webhooksDeliveriesCmd, webhooksRedeliverCmd)
    rootCmd.AddCommand(webhooksCmd)
}
```

(Adjust generated client method names — `go doc ./internal/client` will show the exact spellings.)

- [ ] **Step 3: Add `--direction` to `hail email list`**

In `cli/internal/cmd/email.go`, in the list command, add:

```go
var emailListDirection string

emailListCmd.Flags().StringVar(
    &emailListDirection, "direction", "",
    "Filter by direction: inbound | outbound",
)

// in RunE, pass direction in the query params:
params := &client.GetEmailsParams{}
if emailListDirection != "" {
    d := client.GetEmailsParamsDirection(emailListDirection)
    params.Direction = &d
}
```

- [ ] **Step 4: Add `hail email domain set-forward` / `set-webhook` / `rotate-secret`**

In `cli/internal/cmd/email_domain.go`, after existing subcommands:

```go
var setForwardTargets string

var setForwardCmd = &cobra.Command{
    Use:   "set-forward <domain-id>",
    Short: "Configure forwarding for inbound mail on this domain",
    Args:  cobra.ExactArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        targets := strings.Split(setForwardTargets, ",")
        client, err := newClient()
        if err != nil {
            return err
        }
        body := map[string]any{
            "inbound_enabled": true,
            "forward_to":      targets,
        }
        resp, err := client.PatchEmailDomainsId(cmd.Context(), args[0], body)
        if err != nil {
            return err
        }
        return json.NewEncoder(cmd.OutOrStdout()).Encode(resp)
    },
}

var setWebhookURL string

var setWebhookCmd = &cobra.Command{
    Use:   "set-webhook <domain-id>",
    Short: "Configure webhook for inbound mail on this domain",
    Args:  cobra.ExactArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        client, err := newClient()
        if err != nil {
            return err
        }
        body := map[string]any{
            "inbound_enabled": true,
            "webhook_url":     setWebhookURL,
        }
        resp, err := client.PatchEmailDomainsId(cmd.Context(), args[0], body)
        if err != nil {
            return err
        }
        return json.NewEncoder(cmd.OutOrStdout()).Encode(resp)
    },
}

var rotateSecretCmd = &cobra.Command{
    Use:   "rotate-webhook-secret <domain-id>",
    Short: "Rotate the per-domain webhook secret",
    Args:  cobra.ExactArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        client, err := newClient()
        if err != nil {
            return err
        }
        resp, err := client.PostEmailDomainsIdRotateWebhookSecret(
            cmd.Context(), args[0],
        )
        if err != nil {
            return err
        }
        return json.NewEncoder(cmd.OutOrStdout()).Encode(resp)
    },
}

func init() {
    setForwardCmd.Flags().StringVar(&setForwardTargets, "to", "", "Comma-separated addresses")
    setWebhookCmd.Flags().StringVar(&setWebhookURL, "url", "", "Webhook URL")
    emailDomainCmd.AddCommand(setForwardCmd, setWebhookCmd, rotateSecretCmd)
}
```

- [ ] **Step 5: Tests**

Add table-driven tests in `cli/internal/cmd/webhooks_test.go` mirroring the existing `email_test.go` pattern — assert the CLI builds the right request bodies against a stub HTTP server.

- [ ] **Step 6: Build + run**

Run: `cd cli && go build ./... && go test ./...`
Expected: pass.

### Task 10.3: SDK client updates

**Files:**

- Modify: `sdk/hailhq_sdk/*` (regenerate / update typed client)
- Modify: `sdk/tests/test_*.py` (test inbound list + webhook subscriptions if the SDK has higher-level wrappers)

- [ ] **Step 1: Regenerate or hand-add SDK methods**

Match what the SDK already does for outbound calls/emails. Add:

- `client.emails.list(direction="inbound")`
- `client.emails.raw_url(email_id)` (returns the API URL, not the redirect target)
- `client.webhooks.create(target_url, event_types)`, `list`, `delete`, `rotate_secret`, `redeliver`
- `client.email_domains.set_forward(domain_id, to=[...])`, `set_webhook(domain_id, url=...)`, `rotate_webhook_secret(domain_id)`

- [ ] **Step 2: Test**

Run: `cd sdk && uv run pytest -q`
Expected: pass.

### Task 10.4: Commit Phase 10

```bash
git add openapi/openapi.yaml cli/ sdk/
git commit -m "feat(cli,sdk): inbound + webhooks surface

Regenerate OpenAPI and the typed CLI client. CLI gains:
  hail email list --direction inbound
  hail email domain set-forward <id> --to ...
  hail email domain set-webhook <id> --url ...
  hail email domain rotate-webhook-secret <id>
  hail webhooks create/list/deliveries/redeliver
SDK gains matching methods. OpenAPI is the source of truth — no
hand-edits to client.gen.go survive a regen."
```

---

## Phase 11 — Docs

Goal: operator can stand up inbound from `aws-ses.md` alone; the deferred SMTP provider has a placeholder doc; the architecture doc reflects reality.

### Task 11.1: Extend `docs/setup/aws-ses.md`

**Files:**

- Modify: `docs/setup/aws-ses.md`

- [ ] **Step 1: Add an "Inbound email" section after the existing §9**

Append (keep §9's "What v1 doesn't do" bullet, but flip the inbound line):

````markdown
## 10. Inbound email

Receiving mail at `<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>` requires four things:

1. An MX record on `mail.hail.so` pointing at SES inbound.
2. An S3 bucket SES can write raw MIME into.
3. A SES Receipt Rule that writes the object and invokes a Lambda.
4. A small Lambda that signs the SES event and POSTs it to Hail.

Provisioning is automated by the Terraform module in `infra/terraform/`.
You apply it once per deployment.

### 10.1 Terraform apply

```bash
cd infra/terraform
cp hail.tfvars.example hail.tfvars   # fill in values; generate a 64-hex hmac secret
terraform init
terraform plan
terraform apply
```
````

Outputs include the MX record you publish at DNS, the bucket name, and
the `aws sesv2 set-active-receipt-rule-set` command you need to run.

### 10.2 Activate the receipt rule set (manual)

SES has **one active receipt rule set per region per AWS account**. The
module creates a rule set but does **not** activate it (activation is
destructive when an account already has one running).

- **Greenfield AWS account**: `aws sesv2 set-active-receipt-rule-set --rule-set-name hail-inbound-rules`
- **Account with existing rules**: import the existing rule set into Terraform state and merge Hail's rule into it, or skip the module's rule resource and add it manually via the AWS console.

### 10.3 Publish the MX record

At your DNS provider, set:

```
mail.hail.so  MX  10  inbound-smtp.us-east-1.amazonaws.com
```

(Use the region from the Terraform output if you deploy elsewhere.)

### 10.4 Configure Hail

In the API service `.env`:

```bash
HAIL_INBOUND_ENABLED=true
HAIL_INBOUND_BUCKET=hail-inbound-prod-raw      # from terraform output
HAIL_INBOUND_HMAC_SECRET=<same as Terraform var>
```

Restart `api`. Send a test mail to `<your-user>+<your-org>@mail.hail.so`
and confirm:

```bash
hail email list --direction inbound
```

### 10.5 Forwarding and webhooks

Tenants configure routing per email_domain row via the API or CLI:

```bash
# Forward all inbound on the org's hail-mail address to a real inbox
hail email domain set-forward <id> --to team@acme.com

# Or POST inbound events to a webhook
hail email domain set-webhook <id> --url https://hooks.acme.com/hail
hail email domain rotate-webhook-secret <id>
```

For org-wide multi-event delivery (firehose pattern), use webhook
subscriptions instead:

```bash
hail webhooks create \
  --url https://hooks.acme.com/all \
  --events email.received,email.bounced,email.complained
```

### 10.6 Known footgun: in-process secret cache

The API caches plaintext webhook secrets in process (the values
returned at create/rotate time). On API restart the cache is empty;
the first delivery attempt after restart fails until the tenant
rotates the relevant secrets. This is a known limitation for v1 —
the trade is "no plaintext in the DB" versus "first-attempt failure
after restart." Plan for it in your runbook.

````

Also update the bullet in §9 to drop "Inbound email (v1.5)" from the "doesn't do" list.

### Task 11.2: Placeholder `docs/setup/smtp-inbound.md`

**Files:**
- Create: `docs/setup/smtp-inbound.md`

- [ ] **Step 1: Write**

```markdown
# SMTP inbound — not yet implemented

The `SmtpInboundProvider` interface exists in
[`core/hailhq/core/providers/email/inbound/smtp.py`](../../core/hailhq/core/providers/email/inbound/smtp.py)
but is not implemented. It is the cloud-agnostic / OSS-only path,
deferred to a follow-up milestone.

When it lands, this page will describe:

- the `mailbot/` container (`aiosmtpd`-backed), parallel to `voicebot/`
- listen ports + TLS configuration
- the "front me with Maddy or Postfix" production recipe for SPF/DKIM/DMARC
  verification and flood resistance
- self-host quickstart

In the meantime, use the SES-backed inbound path documented in
[`docs/setup/aws-ses.md`](aws-ses.md). If you must avoid AWS, file an
issue tracking your need and we'll prioritize accordingly.

## References

- Design spec: [`docs/superpowers/specs/2026-06-06-inbound-email-design.md`](../superpowers/specs/2026-06-06-inbound-email-design.md)
- Provider interface: [`core/hailhq/core/providers/email/inbound/base.py`](../../core/hailhq/core/providers/email/inbound/base.py)
````

### Task 11.3: Update `docs/architecture.md`

**Files:**

- Modify: `docs/architecture.md`

- [ ] **Step 1: Append an Inbound email section**

After the existing "Outbound email" section:

```markdown
## Inbound email

Operators on AWS enable inbound by applying `infra/terraform/`, which
provisions an S3 bucket, an SES Receipt Rule + Rule Set, and a small
Lambda that signs and forwards SES events into Hail's
`/internal/ses-events` endpoint. The API parses raw MIME from S3,
routes the message to the owning org by parsing the hail-mail
local-part (`<user>+<org>@mail.hail.so`), persists an `Email` row with
`direction='inbound'`, and fans out events to per-domain webhooks and
org-wide subscriptions via the background delivery worker. The cloud-
agnostic SMTP path is stubbed (`SmtpInboundProvider`) and tracked in
[`docs/setup/smtp-inbound.md`](setup/smtp-inbound.md).

Per-domain routing — forward to one or more addresses and/or POST to a
webhook URL — lives on `email_domains.{forward_to, webhook_url}`. A
separate `inbound_routes` table for per-mailbox routing on custom
domains is deferred to the next milestone (when tenants point their own
MX at SES).
```

### Task 11.4: Drop back-compat aliases

**Files:**

- Modify: `core/hailhq/core/models.py`
- Modify: `core/hailhq/core/schemas.py`

- [ ] **Step 1: Remove the `SenderDomain` aliases**

```python
# DELETE this block in models.py:
# SenderDomain = EmailDomain
```

```python
# DELETE the back-compat alias block in schemas.py.
```

- [ ] **Step 2: Run the full test suite**

Run: `cd core && uv run pytest -q && cd ../api && uv run pytest -q && cd ../sdk && uv run pytest -q && cd ../cli && go test ./...`
Expected: all green. Any remaining `SenderDomain` import flushes out here.

### Task 11.5: Commit Phase 11

```bash
git add docs/ core/hailhq/core/models.py core/hailhq/core/schemas.py
git commit -m "docs(setup): inbound email runbook, SMTP placeholder, architecture

docs/setup/aws-ses.md now covers the full inbound path (Terraform apply,
receipt-rule-set activation, MX publication, env vars, forwarding +
webhook config, the in-process secret cache footgun). New
docs/setup/smtp-inbound.md is a placeholder for the deferred OSS path.
architecture.md updated with the inbound block. SenderDomain back-compat
aliases removed now that all callers are migrated."
```

---

## Self-review

After running through the plan, verify against
[`docs/superpowers/specs/2026-06-06-inbound-email-design.md`](../specs/2026-06-06-inbound-email-design.md):

- **Goals coverage**: forwarding (Phase 7) + webhooks (Phase 6/8) — yes. Persistence + routing (Phase 4). API surface for reading (Phase 8). Per-domain action columns (Phase 2 + 8.4). Operator provisioning (Phase 9). Docs (Phase 11). Open: nothing in the spec is unmapped.
- **Schema invariants**: idempotency unique index, outbound-CHECK, status check expansion, bcrypt secret_hash, non-empty event_types — all present in Tasks 2.1 / 5.1.
- **URL handling per CLAUDE.md**: `canonical_url` used in Tasks 8.2 and 8.3 where the API constructs `raw_url` / attachment URLs for the webhook payload.
- **Terraform receipt-rule-set wart**: documented in Task 11.1 and Task 9.5 comments.
- **Env vars**: every new var in Task 4.1 has a matching block in `.env.example`.

### Known sharp edges to flag during execution

1. **Conftest fixtures.** Several tasks assume `async_session`, `sample_org`, `api_client`, `auth_headers`, `fixed_email_domain`, `seeded_inbound`, etc. If `core/tests/conftest.py` or `api/tests/conftest.py` doesn't already provide them, add them as part of the task that introduces the test — model on existing fixtures in `api/tests/test_emails_api.py` and `core/tests/providers/test_ses_email.py`.
2. **Existing outbound send loop.** Task 7.3 assumes there's somewhere `Email` rows with `status='queued'` get drained. If `rg -n "status.*queued" api/` shows no such loop, add a minimal `OutboundEmailWorker` analogous to `WebhookWorker` in the same phase. Don't punt — forwarded mail won't go out otherwise.
3. **OpenAPI export command.** The exact command in Tasks 1.6 and 10.1 depends on the repo's tooling. Confirm via `rg -n 'openapi.yaml' docs/contributing.md` or by running `cd api && uv run python -c "from hailhq.api.main import app; import json; print(json.dumps(app.openapi()))" > ../openapi/openapi.json` and converting to YAML. Use whatever the contributing doc already documents.
4. **`hail_api_url` setting.** Added in Task 8.2 but not in the spec's env-var list (the spec only listed inbound/forward vars). If the setting already exists for unrelated reasons, reuse it; otherwise add it to `.env.example` alongside the inbound block.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-06-inbound-email.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration. Uses superpowers:subagent-driven-development.

2. **Inline Execution** — execute tasks in this session using superpowers:executing-plans, batch with checkpoints for review.

Which approach?
