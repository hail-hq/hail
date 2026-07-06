# Outbound Email Attachments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let callers (API, MCP, SDK, CLI) attach files to outbound emails: upload once via a new endpoint, reference the returned id from `POST /emails`, and have it ride along on the SES send.

**Architecture:** Upload-then-reference. A new `EmailAttachmentUpload` row (org-scoped, reusable, S3-backed) is created by `POST /email-attachments`; `EmailCreate.attachment_ids` references it from `POST /emails`, which fetches the bytes, passes them into the already-attachment-capable `EmailProvider.send_email(...)`, and persists `EmailAttachment` rows so the existing `GET /emails/{id}` / attachment-download machinery picks them up with no read-side changes. Storage reuses the existing inbound S3 bucket/client, renamed generically since it now serves both directions.

**Tech Stack:** FastAPI/Pydantic v2/SQLAlchemy 2.0/Alembic (api, core), httpx (MCP, SDK), Go/cobra/oapi-codegen (cli).

## Global Constraints

- Max attachment size: **10MB total per email** (body + all attachments combined), matching SES `SendRawEmail`'s default hard limit.
- Oversize rejection detail text, used verbatim everywhere (API, MCP, CLI): `"attachment(s) too large — host the file externally and include a link in the body instead"`.
- No MIME allowlist/denylist — any `content_type` is accepted.
- Uploaded attachments are reusable across multiple sends; unused ones expire after 24h.
- Storage rename: `hail_inbound_bucket`/`S3InboundClient` → `hail_mail_bucket`/`S3MailClient` — no data migration, bucket may be recreated.
- Every route change regenerates `openapi/openapi.yaml` and `cli/internal/client/client.gen.go` in the same PR (existing repo invariant).
- Provider adapters/shared models stay in `core/`; `api/`/`mcp/` never duplicate them.
- New env vars are added to `.env.example` in the same commit that introduces them.

---

### Task 1: Rename the inbound S3 client/bucket to a generic "mail" name (core layer)

No behavior change — pure rename so the client/bucket reflect that they now serve both inbound and outbound mail. Acceptable to recreate the bucket; no migration needed.

**Files:**

- Create: `core/hailhq/core/s3_mail.py` (renamed from `s3_inbound.py`)
- Delete: `core/hailhq/core/s3_inbound.py`
- Create: `core/tests/test_s3_mail.py` (renamed from `test_s3_inbound.py`)
- Delete: `core/tests/test_s3_inbound.py`
- Modify: `core/hailhq/core/config.py:94-98,212-218`
- Modify: `.env.example:66`, `.env.example:139-141`
- Modify: `core/hailhq/core/outbound_worker.py:36-38,73,82,91-93`
- Modify: `core/hailhq/core/email_ingest.py:37`

**Interfaces:**

- Produces: `hailhq.core.s3_mail.S3MailClient` (same methods as the old `S3InboundClient`: `fetch_raw`, `put_attachment`, `presign_get`, constructor `__init__(self, *, client=None, bucket)`), `hailhq.core.s3_mail.build_default_client`.
- Produces: `settings.hail_mail_name_prefix: str` and `settings.hail_mail_bucket` (computed property, `f"{prefix}-mail"`), replacing `hail_inbound_email_name_prefix`/`hail_inbound_bucket`.

- [ ] **Step 1: Rename the module and its test file with git, preserving history**

```bash
cd /Users/r/playground/hail
git mv core/hailhq/core/s3_inbound.py core/hailhq/core/s3_mail.py
git mv core/tests/test_s3_inbound.py core/tests/test_s3_mail.py
```

- [ ] **Step 2: Rename the class inside the moved module**

Edit `core/hailhq/core/s3_mail.py` — replace the docstring's first line and the class name:

```python
"""S3 client wrapper for mail MIME + attachment objects.

boto3 is sync; every call is dropped into ``asyncio.to_thread`` so
FastAPI handlers can ``await`` without blocking the event loop. Same
pattern as ``SesEmailProvider``. Backs both inbound (raw MIME + parsed
attachments) and outbound (uploaded attachment) storage in one bucket.
"""
```

Replace:

```python
__all__ = ["S3InboundClient", "build_default_client"]
```

with:

```python
__all__ = ["S3MailClient", "build_default_client"]
```

Replace:

```python
class S3InboundClient:
```

with:

```python
class S3MailClient:
```

- [ ] **Step 3: Update the renamed test file**

Edit `core/tests/test_s3_mail.py` — replace the import and every `S3InboundClient` reference:

```python
from hailhq.core.s3_mail import S3MailClient
```

Replace every occurrence of `S3InboundClient(` with `S3MailClient(` (4 occurrences: the constructor-rejects-empty-bucket test, and the three `_stub_client`-based tests). Function names (`test_fetch_raw_returns_bytes` etc.) stay as-is.

- [ ] **Step 4: Run the renamed test file, verify it passes**

```bash
cd core && uv run pytest tests/test_s3_mail.py -v
```

Expected: 4 passed, 0 failed. (This also confirms `core/tests/test_s3_inbound.py` no longer exists — pytest would otherwise collect both.)

- [ ] **Step 5: Rename the config fields**

Edit `core/hailhq/core/config.py` — replace:

```python
    hail_inbound_enabled: bool = False
    # Single source of truth for both the Terraform module and the API.
    # The raw-MIME bucket name is derived as ``{prefix}-raw``; SES Lambda
    # writes there, the API reads back from it. Set in .env / .env.example.
    hail_inbound_email_name_prefix: str = ""
    hail_inbound_hmac_secret: str = ""
```

with:

```python
    hail_inbound_enabled: bool = False
    # Single source of truth for both the Terraform module and the API.
    # The mail bucket name is derived as ``{prefix}-mail``; SES Lambda
    # writes inbound raw MIME there, outbound sends write uploaded
    # attachments there, and the API reads both back. Set in .env /
    # .env.example.
    hail_mail_name_prefix: str = ""
    hail_inbound_hmac_secret: str = ""
```

Replace:

```python
    @computed_field  # type: ignore[prop-decorator]
    @property
    def hail_inbound_bucket(self) -> str:
        """Raw-MIME bucket name. Derived to match Terraform's `${prefix}-raw`."""
        if not self.hail_inbound_email_name_prefix:
            return ""
        return f"{self.hail_inbound_email_name_prefix}-raw"
```

with:

```python
    @computed_field  # type: ignore[prop-decorator]
    @property
    def hail_mail_bucket(self) -> str:
        """Shared mail bucket name. Derived to match Terraform's `${prefix}-mail`."""
        if not self.hail_mail_name_prefix:
            return ""
        return f"{self.hail_mail_name_prefix}-mail"
```

- [ ] **Step 6: Rename the env var**

Edit `.env.example` line 66 — replace:

```
HAIL_INBOUND_EMAIL_NAME_PREFIX=hail-email-inbound
```

with:

```
HAIL_MAIL_NAME_PREFIX=hail-email-mail
```

Edit `.env.example` lines 139-141 — replace:

```
# Inbound — off by default. Bucket name is derived as `${prefix}-raw`
# from HAIL_INBOUND_EMAIL_NAME_PREFIX above; Terraform creates that bucket
# and the API reads from it — single source, no drift.
```

with:

```
# Inbound — off by default. Bucket name is derived as `${prefix}-mail`
# from HAIL_MAIL_NAME_PREFIX above; Terraform creates that bucket (shared
# by inbound raw MIME and outbound attachments) and the API reads/writes
# it — single source, no drift.
```

- [ ] **Step 7: Update `outbound_worker.py`'s import and type hints**

Edit `core/hailhq/core/outbound_worker.py` line 38 — replace:

```python
from hailhq.core.s3_inbound import S3InboundClient
```

with:

```python
from hailhq.core.s3_mail import S3MailClient
```

Replace every remaining `S3InboundClient` occurrence in the file (constructor param type `s3_factory: Callable[[], S3InboundClient]` at line 73, instance attribute `self._s3: S3InboundClient | None` at line 82, and `_get_s3(self) -> S3InboundClient` at line 91) with `S3MailClient`.

- [ ] **Step 8: Update `email_ingest.py`'s import**

Edit `core/hailhq/core/email_ingest.py` line 37 — replace:

```python
from hailhq.core.s3_inbound import S3InboundClient
```

with:

```python
from hailhq.core.s3_mail import S3MailClient
```

Replace the `s3: S3InboundClient` parameter type on `_persist_attachments` (and any other `S3InboundClient` reference in this file) with `S3MailClient`.

- [ ] **Step 9: Run the affected core test suites**

```bash
cd core && uv run pytest tests/test_s3_mail.py -v
grep -rn "S3InboundClient\|hail_inbound_bucket\|hail_inbound_email_name_prefix\|s3_inbound" --include="*.py" /Users/r/playground/hail/core
```

Expected: pytest passes; the grep returns nothing (confirms no core-layer stragglers — `api/` call sites are handled in Task 2).

- [ ] **Step 10: Commit**

```bash
git add core/hailhq/core/s3_mail.py core/tests/test_s3_mail.py core/hailhq/core/config.py .env.example core/hailhq/core/outbound_worker.py core/hailhq/core/email_ingest.py
git commit -m "refactor(core): rename inbound S3 client/bucket to generic mail naming"
```

---

### Task 2: Rename the S3 client call sites in the API layer + fix a docs bug found during exploration

**Files:**

- Modify: `api/hailhq/api/main.py:16-21,147,151`
- Modify: `api/hailhq/api/routes/emails.py` (imports, `_get_s3_inbound` helper, both its call sites)
- Modify: `api/hailhq/api/routes/internal/ses_events.py` (imports, `get_s3_inbound_client` helper, its call site)
- Modify: `api/tests/test_internal_ses_events.py`, `api/tests/test_internal_ses_events_multi_org.py`, `api/tests/test_emails_inbound_reads.py`
- Modify: `docs/setup/aws-ses.md:211-219,240-248`
- Modify: `docs/operations.md:390-420,550-599`

**Interfaces:**

- Consumes: `hailhq.core.s3_mail.S3MailClient`, `settings.hail_mail_bucket` (from Task 1).
- Produces: `_get_s3_mail()` dependency in `routes/emails.py`, moved **above** `create_email` in the file (Task 6 adds a new `Depends(_get_s3_mail)` parameter to `create_email`, which requires the helper to already be defined by then — default-argument expressions are evaluated at `def` time, not call time).

- [ ] **Step 1: Repo-wide mechanical rename of the identical strings**

These three tokens are renamed identically everywhere they appear (verified by earlier grep to be confined to `api/main.py`, `api/routes/emails.py`, `api/routes/internal/ses_events.py`, and the three test files above — `core/` was already handled in Task 1):

```bash
cd /Users/r/playground/hail
grep -rl "S3InboundClient\|hail_inbound_bucket\|hailhq\.core\.s3_inbound" \
  api/hailhq/api/main.py \
  api/hailhq/api/routes/emails.py \
  api/hailhq/api/routes/internal/ses_events.py \
  api/tests/test_internal_ses_events.py \
  api/tests/test_internal_ses_events_multi_org.py \
  api/tests/test_emails_inbound_reads.py \
  | xargs sed -i '' \
    -e 's/S3InboundClient/S3MailClient/g' \
    -e 's/hail_inbound_bucket/hail_mail_bucket/g' \
    -e 's/hailhq\.core\.s3_inbound/hailhq.core.s3_mail/g'
```

- [ ] **Step 2: Rename the two dependency-helper functions and reposition one of them**

Edit `api/hailhq/api/routes/emails.py` — the helper (now reading, after Step 1's sed pass):

```python
def _get_s3_inbound() -> S3MailClient:
    from hailhq.core.config import settings as _s

    return S3MailClient(bucket=_s.hail_mail_bucket)
```

currently sits at line ~714, **after** `create_email` (which starts at line 278). Move this whole function definition to just above `_resolve_sender` (i.e., right after the module-level constants `_DEFAULT_LIST_LIMIT`/`_MAX_LIST_LIMIT`/`_SEND_FAILED_DETAIL`, before the `# Sender resolution.` section header), and rename it to `_get_s3_mail`:

```python
def _get_s3_mail() -> S3MailClient:
    return S3MailClient(bucket=settings.hail_mail_bucket)
```

(Drop the local `from hailhq.core.config import settings as _s` re-import — `settings` is already imported at module level via `from hailhq.core.email_footer import ...`'s neighbors; if `settings` isn't already imported at the top of this file, add `from hailhq.core.config import settings` to the top-level imports.)

Update the two existing `Depends(_get_s3_inbound)` call sites (in `get_email_raw` and `get_email_attachment`) to `Depends(_get_s3_mail)`, and their parameter type annotations from `S3MailClient` (already renamed by the sed pass) referencing `_get_s3_inbound` → `_get_s3_mail`.

Edit `api/hailhq/api/routes/internal/ses_events.py` — rename:

```python
def get_s3_inbound_client() -> S3MailClient:
```

to:

```python
def get_s3_mail_client() -> S3MailClient:
```

and update its one call site (`Depends(get_s3_inbound_client)` → `Depends(get_s3_mail_client)`) in `receive_ses_event`.

- [ ] **Step 3: Fix the pre-existing docs bug (found during design exploration)**

`docs/setup/aws-ses.md` and `docs/operations.md` currently document a `HAIL_INBOUND_BUCKET` env var that was never real — the bucket has only ever been a `Settings`-computed field derived from a prefix var, never independently settable. Since these lines are being touched for the rename anyway, correct them.

Edit `docs/setup/aws-ses.md` — replace line 214:

```
- `inbound_bucket` — set as `HAIL_INBOUND_BUCKET` in the API `.env`.
```

with:

```
- `inbound_bucket` — this is `${HAIL_MAIL_NAME_PREFIX}-mail`; set `HAIL_MAIL_NAME_PREFIX` in the API `.env` to match the Terraform `name_prefix` var (not settable directly — there is no `HAIL_MAIL_BUCKET` var).
```

Replace lines 244-248:

```bash
HAIL_INBOUND_ENABLED=true
HAIL_INBOUND_BUCKET=hail-inbound-prod-raw      # from terraform output
HAIL_INBOUND_HMAC_SECRET=<same as Terraform var>
```

with:

```bash
HAIL_INBOUND_ENABLED=true
HAIL_MAIL_NAME_PREFIX=hail-inbound-prod         # matches Terraform `name_prefix`; bucket = ${prefix}-mail
HAIL_INBOUND_HMAC_SECRET=<same as Terraform var>
```

Edit `docs/operations.md` — replace line 400:

```
# Capture outputs: inbound_mx_record, inbound_bucket, activate_command, lambda_function_arn
```

with:

```
# Capture outputs: inbound_mx_record, inbound_bucket, activate_command, lambda_function_arn
#   (inbound_bucket confirms ${HAIL_MAIL_NAME_PREFIX}-mail — not independently settable)
```

Replace lines 415-417:

```
#      HAIL_INBOUND_BUCKET=<terraform output inbound_bucket>
```

with:

```
#      HAIL_MAIL_NAME_PREFIX=<terraform var name_prefix — same value, bucket derives as ${prefix}-mail>
```

Replace lines 556-557:

```
- `inbound_bucket` — goes into `HAIL_INBOUND_BUCKET` on the API.
```

with:

```
- `inbound_bucket` — confirms `${HAIL_MAIL_NAME_PREFIX}-mail`; set `HAIL_MAIL_NAME_PREFIX` (not `inbound_bucket` itself) on the API.
```

Replace line 596:

```
HAIL_INBOUND_BUCKET=<terraform output>
```

with:

```
HAIL_MAIL_NAME_PREFIX=<terraform var name_prefix>
```

- [ ] **Step 4: Run the affected API test suites**

```bash
cd api && uv run pytest tests/test_internal_ses_events.py tests/test_internal_ses_events_multi_org.py tests/test_emails_inbound_reads.py tests/test_emails_api.py -v
```

Expected: all pass, 0 failures.

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/main.py api/hailhq/api/routes/emails.py api/hailhq/api/routes/internal/ses_events.py api/tests/test_internal_ses_events.py api/tests/test_internal_ses_events_multi_org.py api/tests/test_emails_inbound_reads.py docs/setup/aws-ses.md docs/operations.md
git commit -m "refactor(api): rename S3 inbound client call sites to generic mail naming; fix stale env var docs"
```

---

### Task 3: Rename the Terraform module

**Files:**

- Create: `infra/terraform/s3_mail.tf` (renamed from `s3_inbound.tf`), with an added lifecycle rule for outbound attachments
- Delete: `infra/terraform/s3_inbound.tf`
- Modify: `infra/terraform/main.tf:7`
- Modify: `infra/terraform/outputs.tf:6-9`

- [ ] **Step 1: Rename the file**

```bash
cd /Users/r/playground/hail
git mv infra/terraform/s3_inbound.tf infra/terraform/s3_mail.tf
```

- [ ] **Step 2: Add a third lifecycle rule for the new outbound-attachments prefix**

Edit `infra/terraform/s3_mail.tf` — the `aws_s3_bucket_lifecycle_configuration` resource currently has two rules (`expire-raw` on prefix `raw/`, `expire-attachments` on prefix `attachments/`). Add a third, same shape, for the new outbound prefix:

```hcl
resource "aws_s3_bucket_lifecycle_configuration" "inbound" {
  bucket = aws_s3_bucket.inbound.id

  rule {
    id     = "expire-raw"
    status = "Enabled"
    filter {
      prefix = "raw/"
    }
    expiration {
      days = var.raw_object_expiration_days
    }
  }

  rule {
    id     = "expire-attachments"
    status = "Enabled"
    filter {
      prefix = "attachments/"
    }
    expiration {
      days = var.raw_object_expiration_days
    }
  }

  rule {
    id     = "expire-outbound-attachments"
    status = "Enabled"
    filter {
      prefix = "outbound-attachments/"
    }
    expiration {
      days = var.raw_object_expiration_days
    }
  }
}
```

(Everything else in this file — the bucket resource, public access block, SES write policy — is unchanged; only the lifecycle-configuration resource gets the new rule.)

- [ ] **Step 3: Update the bucket name derivation**

Edit `infra/terraform/main.tf` line 7 — replace:

```hcl
  bucket_name    = "${var.name_prefix}-raw"
```

with:

```hcl
  bucket_name    = "${var.name_prefix}-mail"
```

- [ ] **Step 4: Update the output description**

Edit `infra/terraform/outputs.tf` lines 6-9 — replace:

```hcl
output "inbound_bucket" {
  description = "Set as HAIL_INBOUND_BUCKET in .env."
  value       = aws_s3_bucket.inbound.bucket
}
```

with:

```hcl
output "inbound_bucket" {
  description = "Confirms ${HAIL_MAIL_NAME_PREFIX}-mail; set HAIL_MAIL_NAME_PREFIX (not this value directly) in .env."
  value       = aws_s3_bucket.inbound.bucket
}
```

- [ ] **Step 5: Validate the Terraform syntax**

```bash
cd infra/terraform && terraform fmt -check && terraform validate
```

Expected: `terraform fmt -check` exits 0 (no reformatting needed); `terraform validate` reports `Success! The configuration is valid.` (Requires no live AWS credentials — `validate` is a local syntax/type check only.)

- [ ] **Step 6: Commit**

```bash
git add infra/terraform/s3_mail.tf infra/terraform/main.tf infra/terraform/outputs.tf
git commit -m "refactor(infra): rename inbound bucket module to generic mail naming, add outbound-attachments lifecycle rule"
```

---

### Task 4: Add the `EmailAttachmentUpload` model and migration

**Files:**

- Modify: `core/hailhq/core/models.py` (add new class after `EmailAttachment`, currently ending at line 797)
- Create: `api/migrations/versions/0023_email_attachment_uploads.py`
- Create: `core/tests/test_models_email_attachment_upload.py`

**Interfaces:**

- Produces: `hailhq.core.models.EmailAttachmentUpload` with columns `id: UUID`, `organization_id: UUID`, `filename: str`, `content_type: str`, `size_bytes: int`, `s3_key: str`, `created_at: datetime`, `first_used_at: datetime | None`. Table name `email_attachment_uploads`.

- [ ] **Step 1: Add the model**

Edit `core/hailhq/core/models.py` — add immediately after the `EmailAttachment` class (after line 797):

```python
class EmailAttachmentUpload(Base):
    """A caller-uploaded file, pre-send, awaiting reference from a `send`.

    Distinct from ``EmailAttachment`` (which is always 1:1 with an
    already-received inbound email, created once at ingest, never reused).
    This row is org-owned, reusable across many outbound sends until
    referenced or garbage-collected — see
    ``hailhq.core.email_attachment_gc.EmailAttachmentGcWorker``, which
    deletes rows where ``first_used_at`` is still null 24h after upload.
    Rows that have been used at least once are kept indefinitely.
    """

    __tablename__ = "email_attachment_uploads"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    first_used_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)

    __table_args__ = (
        Index(
            "email_attachment_uploads_gc_idx",
            "created_at",
            postgresql_where=text("first_used_at IS NULL"),
        ),
        Index(
            "email_attachment_uploads_org_idx",
            "organization_id",
        ),
    )
```

- [ ] **Step 2: Write the migration**

Create `api/migrations/versions/0023_email_attachment_uploads.py`:

```python
"""email_attachment_uploads table — pre-send, reusable outbound attachments.

Uploaded via POST /email-attachments, referenced by EmailCreate.attachment_ids.
Rows never used by a send are garbage-collected 24h after upload (see
hailhq.core.email_attachment_gc); used rows are kept indefinitely. Distinct
from email_attachments (0007), which is always 1:1 with an inbound email.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_attachment_uploads",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("first_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "email_attachment_uploads_gc_idx",
        "email_attachment_uploads",
        ["created_at"],
        postgresql_where=sa.text("first_used_at IS NULL"),
    )
    op.create_index(
        "email_attachment_uploads_org_idx",
        "email_attachment_uploads",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "email_attachment_uploads_org_idx", table_name="email_attachment_uploads"
    )
    op.drop_index(
        "email_attachment_uploads_gc_idx", table_name="email_attachment_uploads"
    )
    op.drop_table("email_attachment_uploads")
```

- [ ] **Step 3: Run the migration against the local dev DB**

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d postgres
cd api && uv run alembic upgrade head && uv run alembic current
```

Expected: `uv run alembic current` prints `0023 (head)`.

- [ ] **Step 4: Write and run a model smoke test**

Create `core/tests/test_models_email_attachment_upload.py`:

```python
"""Smoke test: EmailAttachmentUpload round-trips through the ORM."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import EmailAttachmentUpload


@pytest.mark.asyncio
async def test_create_and_fetch_upload_row(async_session: AsyncSession) -> None:
    org_id = uuid4()
    row = EmailAttachmentUpload(
        organization_id=org_id,
        filename="invoice.pdf",
        content_type="application/pdf",
        size_bytes=1234,
        s3_key=f"outbound-attachments/{org_id}/{uuid4()}",
    )
    async_session.add(row)
    await async_session.commit()
    await async_session.refresh(row)

    assert row.id is not None
    assert row.first_used_at is None
    assert row.created_at is not None
```

(Uses the same `async_session` fixture pattern as the existing `core/tests/` suite — check `core/tests/conftest.py` for its exact name/scope if this differs; adjust the fixture parameter to match.)

```bash
cd core && uv run pytest tests/test_models_email_attachment_upload.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/models.py api/migrations/versions/0023_email_attachment_uploads.py core/tests/test_models_email_attachment_upload.py
git commit -m "feat(core): add EmailAttachmentUpload model + migration 0023"
```

---

### Task 5: Add shared size-cap constants and the new schemas

**Files:**

- Create: `core/hailhq/core/email_attachment_limits.py`
- Modify: `core/hailhq/core/schemas.py` (add `EmailAttachmentUploadResponse`; add `attachment_ids` to `EmailCreate`)
- Create: `core/tests/test_email_attachment_limits.py`

**Interfaces:**

- Produces: `MAX_EMAIL_ATTACHMENT_BYTES: int` (10MB), `ATTACHMENT_TOO_LARGE_DETAIL: str` — imported by both the upload route (Task 6) and the send route (Task 7).
- Produces: `EmailAttachmentUploadResponse(id: UUID, filename: str, content_type: str, size_bytes: int)`.
- Produces: `EmailCreate.attachment_ids: list[UUID] | None`.

- [ ] **Step 1: Add the constants module**

Create `core/hailhq/core/email_attachment_limits.py`:

```python
"""Shared size cap for outbound email attachments.

One constant enforced at two layers — the single-file upload endpoint
(POST /email-attachments) and the aggregate per-send check (POST
/emails) — so the error text a caller sees is identical everywhere
(API, MCP, CLI). Mirrors SES SendRawEmail's default hard limit.
"""

MAX_EMAIL_ATTACHMENT_BYTES = 10 * 1024 * 1024

ATTACHMENT_TOO_LARGE_DETAIL = (
    "attachment(s) too large — host the file externally and include a "
    "link in the body instead"
)

__all__ = ["MAX_EMAIL_ATTACHMENT_BYTES", "ATTACHMENT_TOO_LARGE_DETAIL"]
```

- [ ] **Step 2: Write a failing test for the constants**

Create `core/tests/test_email_attachment_limits.py`:

```python
from hailhq.core.email_attachment_limits import (
    ATTACHMENT_TOO_LARGE_DETAIL,
    MAX_EMAIL_ATTACHMENT_BYTES,
)


def test_max_attachment_bytes_is_ten_megabytes():
    assert MAX_EMAIL_ATTACHMENT_BYTES == 10 * 1024 * 1024


def test_oversize_detail_mentions_a_link():
    assert "link" in ATTACHMENT_TOO_LARGE_DETAIL
```

```bash
cd core && uv run pytest tests/test_email_attachment_limits.py -v
```

Expected: 2 passed (this module has no prior implementation to fail against — it's a pure-constants smoke test, written and passing in one step).

- [ ] **Step 3: Add `EmailAttachmentUploadResponse` to schemas**

Edit `core/hailhq/core/schemas.py` — add immediately after `EmailAttachmentResponse` (after line 625):

```python
class EmailAttachmentUploadResponse(BaseModel):
    """Returned by POST /email-attachments.

    ``id`` is reusable across many ``POST /emails`` calls via
    ``EmailCreate.attachment_ids`` until Hail garbage-collects it (24h
    if never referenced by a send).
    """

    id: UUID
    filename: str
    content_type: str
    size_bytes: int

    model_config = ConfigDict(from_attributes=True)
```

- [ ] **Step 4: Add `attachment_ids` to `EmailCreate`**

Edit `core/hailhq/core/schemas.py` — in `EmailCreate` (lines 441-464), add the new field after `metadata`:

```python
    metadata: dict = Field(default_factory=dict)
    attachment_ids: list[UUID] | None = None
```

- [ ] **Step 5: Add `EmailAttachmentUploadResponse` to `schemas.py`'s `__all__`/export surface if one exists**

```bash
grep -n "^__all__" core/hailhq/core/schemas.py
```

If `schemas.py` has an `__all__` list, add `"EmailAttachmentUploadResponse"` to it. If it doesn't (imports elsewhere are by name, not via `__all__`), skip this step.

- [ ] **Step 6: Run the core schema tests**

```bash
cd core && uv run pytest tests/ -k "schema or email_attachment_limits" -v
```

Expected: all pass, 0 failures.

- [ ] **Step 7: Commit**

```bash
git add core/hailhq/core/email_attachment_limits.py core/hailhq/core/schemas.py core/tests/test_email_attachment_limits.py
git commit -m "feat(core): add attachment size-cap constants, EmailAttachmentUploadResponse, EmailCreate.attachment_ids"
```

---

### Task 6: `POST /email-attachments` route

**Files:**

- Create: `api/hailhq/api/routes/email_attachments.py`
- Modify: `api/hailhq/api/main.py` (import + `app.include_router(...)`)
- Create: `api/tests/test_email_attachments_api.py`

**Interfaces:**

- Consumes: `EmailAttachmentUploadResponse`, `MAX_EMAIL_ATTACHMENT_BYTES`, `ATTACHMENT_TOO_LARGE_DETAIL` (Task 5), `EmailAttachmentUpload` model (Task 4), `S3MailClient` (Task 1/2), `Principal`/`get_current_principal` (existing `api/hailhq/api/deps.py`).
- Produces: `router` (FastAPI `APIRouter`, prefix `/email-attachments`) exposing `POST /email-attachments` with `operation_id="upload_email_attachment"` — the explicit operation_id keeps the oapi-codegen-generated Go method name predictable for Task 11.

- [ ] **Step 1: Write the route file**

Create `api/hailhq/api/routes/email_attachments.py`:

```python
"""Routes for outbound email attachment uploads.

POST /email-attachments - upload a file, get back a reusable id.

Uploads are org-scoped and reusable across many POST /emails calls (see
EmailCreate.attachment_ids) until garbage-collected — see
hailhq.core.email_attachment_gc. Bytes live in the shared mail S3 bucket
under outbound-attachments/{organization_id}/{id}; the row purely tracks
metadata + first-use so the GC worker knows what's safe to delete.
"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.deps import Principal, get_current_principal
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.email_attachment_limits import (
    ATTACHMENT_TOO_LARGE_DETAIL,
    MAX_EMAIL_ATTACHMENT_BYTES,
)
from hailhq.core.models import EmailAttachmentUpload
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.schemas import EmailAttachmentUploadResponse

router = APIRouter(prefix="/email-attachments", tags=["email-attachments"])


def _get_s3_mail() -> S3MailClient:
    return S3MailClient(bucket=settings.hail_mail_bucket)


@router.post(
    "",
    response_model=EmailAttachmentUploadResponse,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="upload_email_attachment",
)
async def create_email_attachment(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3MailClient, Depends(_get_s3_mail)],
    file: Annotated[UploadFile, File()],
) -> EmailAttachmentUploadResponse:
    payload = await file.read()
    if len(payload) > MAX_EMAIL_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=ATTACHMENT_TOO_LARGE_DETAIL,
        )

    content_type = file.content_type or "application/octet-stream"
    upload_id = uuid4()
    key = f"outbound-attachments/{principal.organization_id}/{upload_id}"
    await s3.put_attachment(key, payload, content_type)

    row = EmailAttachmentUpload(
        id=upload_id,
        organization_id=principal.organization_id,
        filename=file.filename or "attachment",
        content_type=content_type,
        size_bytes=len(payload),
        s3_key=key,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    return EmailAttachmentUploadResponse.model_validate(row)


__all__ = ["router"]
```

- [ ] **Step 2: Wire the router into the app**

Edit `api/hailhq/api/main.py` — add the import alongside the other route imports (after line 27):

```python
from hailhq.api.routes import email_attachments as email_attachments_routes
```

Add the `include_router` call alongside the others (after line 214, `app.include_router(email_domains_routes.router)`):

```python
app.include_router(email_attachments_routes.router)
```

- [ ] **Step 3: Write the failing test**

`api/tests/test_emails_inbound_reads.py:31-39` establishes the exact S3-stub pattern this repo uses for these routes — a pytest fixture that overrides the route module's `_get_s3_mail` dependency with an `AsyncMock()` (post-Task-2-rename name; was `_get_s3_inbound`). Reuse that pattern here, overriding `email_attachments._get_s3_mail` instead.

Create `api/tests/test_email_attachments_api.py`:

```python
"""Integration tests for POST /email-attachments."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from hailhq.api.main import app
from hailhq.api.routes import email_attachments

from .conftest import insert_org_and_key  # noqa: F401


@pytest.fixture()
def s3_mail_mock():
    s3 = AsyncMock()
    app.dependency_overrides[email_attachments._get_s3_mail] = lambda: s3
    try:
        yield s3
    finally:
        app.dependency_overrides.pop(email_attachments._get_s3_mail, None)


async def test_upload_returns_id_and_metadata(
    client: httpx.AsyncClient, org_and_key: tuple, s3_mail_mock: AsyncMock
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}

    resp = await client.post(
        "/email-attachments",
        files={"file": ("invoice.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["filename"] == "invoice.pdf"
    assert body["content_type"] == "application/pdf"
    assert body["size_bytes"] == len(b"%PDF-1.4 fake pdf bytes")
    assert body["id"]
    s3_mail_mock.put_attachment.assert_awaited_once()


async def test_upload_rejects_oversize_file(
    client: httpx.AsyncClient, org_and_key: tuple, s3_mail_mock: AsyncMock
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    oversize = b"x" * (10 * 1024 * 1024 + 1)

    resp = await client.post(
        "/email-attachments",
        files={"file": ("big.bin", oversize, "application/octet-stream")},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "link" in resp.json()["detail"]
    s3_mail_mock.put_attachment.assert_not_awaited()


async def test_upload_requires_auth(
    client: httpx.AsyncClient, s3_mail_mock: AsyncMock
) -> None:
    resp = await client.post(
        "/email-attachments",
        files={"file": ("a.txt", b"hi", "text/plain")},
    )
    assert resp.status_code == 401
```

- [ ] **Step 4: Run the test, verify it passes**

```bash
cd api && uv run pytest tests/test_email_attachments_api.py -v
```

Expected: 3 passed. (The route was already implemented in Step 1 — this task builds the endpoint before its test, unlike the schema-first tasks elsewhere in this plan, since the route's shape needed to exist before the test's S3-mock fixture could target its dependency function by name.)

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/email_attachments.py api/hailhq/api/main.py api/tests/test_email_attachments_api.py
git commit -m "feat(api): add POST /email-attachments upload endpoint"
```

---

### Task 7: Wire `attachment_ids` into `POST /emails`

**Files:**

- Modify: `api/hailhq/api/routes/emails.py` (imports, `create_email`)
- Modify: `api/tests/test_emails_api.py`

**Interfaces:**

- Consumes: `EmailAttachmentUpload` (Task 4), `MAX_EMAIL_ATTACHMENT_BYTES`/`ATTACHMENT_TOO_LARGE_DETAIL` (Task 5), `ProviderAttachment` (`hailhq.core.providers.email.base`, already accepted by `EmailProvider.send_email(..., attachments=...)` and the SES raw-MIME path — no provider-layer changes needed), `_get_s3_mail` (Task 2, already repositioned above `create_email`).
- Produces: outbound sends with attachments now create `EmailAttachment` rows readable via the _existing, unmodified_ `GET /emails/{id}` handler (verified: its attachment query at `routes/emails.py:686-694` filters only by `email_id`, no `direction` clause).

- [ ] **Step 1: Add imports**

Edit `api/hailhq/api/routes/emails.py` — add to the existing import block:

```python
from hailhq.core.email_attachment_limits import (
    ATTACHMENT_TOO_LARGE_DETAIL,
    MAX_EMAIL_ATTACHMENT_BYTES,
)
from hailhq.core.models import Email, EmailAttachment, EmailAttachmentUpload, EmailDomain, EmailEvent
from hailhq.core.providers.email.base import ProviderAttachment
```

(The `from hailhq.core.models import Email, EmailAttachment, EmailDomain, EmailEvent` line at line 50 already exists — extend it to include `EmailAttachmentUpload` rather than adding a second import line.)

- [ ] **Step 2: Write the failing test**

These tests exercise both `POST /email-attachments` and `POST /emails` in
the same flow, so both routes' `_get_s3_mail` dependencies need
stubbing — reuse the same `AsyncMock`-override pattern as
`api/tests/test_emails_inbound_reads.py:31-39` (see Task 6 Step 3), but
override both `email_attachments._get_s3_mail` and
`emails_routes._get_s3_mail`, and set `fetch_raw`'s return value since
`create_email` passes it straight into `ProviderAttachment(payload=...)`,
which requires real `bytes`.

Add to `api/tests/test_emails_api.py`:

```python
from hailhq.api.main import app
from hailhq.api.routes import email_attachments
from hailhq.api.routes import emails as emails_routes


@pytest.fixture()
def s3_mail_mock():
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"pdf bytes"
    app.dependency_overrides[email_attachments._get_s3_mail] = lambda: s3
    app.dependency_overrides[emails_routes._get_s3_mail] = lambda: s3
    try:
        yield s3
    finally:
        app.dependency_overrides.pop(email_attachments._get_s3_mail, None)
        app.dependency_overrides.pop(emails_routes._get_s3_mail, None)


async def _upload_attachment(
    client: httpx.AsyncClient, headers: dict, content: bytes = b"pdf bytes"
) -> str:
    resp = await client.post(
        "/email-attachments",
        files={"file": ("invoice.pdf", content, "application/pdf")},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_post_emails_with_attachment_ids_attaches_and_lists(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    async_session: AsyncSession,
    s3_mail_mock: AsyncMock,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    att_id = await _upload_attachment(client, headers)

    resp = await client.post(
        "/emails",
        json={
            "to": ["alice@example.com"],
            "subject": "hi",
            "body_text": "body",
            "recipient_consent": True,
            "attachment_ids": [att_id],
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    email_id = resp.json()["id"]

    email_mock.send_email.assert_awaited_once()
    call_kwargs = email_mock.send_email.call_args.kwargs
    assert len(call_kwargs["attachments"]) == 1
    assert call_kwargs["attachments"][0].filename == "invoice.pdf"

    get_resp = await client.get(f"/emails/{email_id}", headers=headers)
    assert get_resp.status_code == 200
    attachments = get_resp.json()["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "invoice.pdf"


async def test_post_emails_rejects_oversize_aggregate_attachments(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    s3_mail_mock: AsyncMock,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    big = b"x" * (10 * 1024 * 1024)
    att_id = await _upload_attachment(client, headers, content=big)

    resp = await client.post(
        "/emails",
        json={
            "to": ["alice@example.com"],
            "subject": "hi",
            "body_text": "this pushes it over the 10MB cap",
            "recipient_consent": True,
            "attachment_ids": [att_id],
        },
        headers=headers,
    )
    assert resp.status_code == 422
    assert "link" in resp.json()["detail"]
    email_mock.send_email.assert_not_awaited()


async def test_post_emails_rejects_attachment_from_another_org(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")

    fake_id = "00000000-0000-0000-0000-000000000000"
    resp = await client.post(
        "/emails",
        json={
            "to": ["alice@example.com"],
            "subject": "hi",
            "body_text": "body",
            "recipient_consent": True,
            "attachment_ids": [fake_id],
        },
        headers=headers,
    )
    assert resp.status_code == 404
    email_mock.send_email.assert_not_awaited()
```

```bash
cd api && uv run pytest tests/test_emails_api.py -k "attachment" -v
```

Expected: FAIL (`attachment_ids` accepted by the schema per Task 5, but `create_email` ignores it — `attachments` key error in the mock assertions, and no 404/422 behavior yet).

- [ ] **Step 3: Resolve and validate attachments before creating the `Email` row**

Edit `api/hailhq/api/routes/emails.py`, `create_email` — insert this block right after the balance gate (after line 340, `raise HTTPException(... insufficient credits ...)`, before `sd = await _resolve_sender(...)`):

```python
    attachment_rows: list[EmailAttachmentUpload] = []
    if body.attachment_ids:
        stmt = select(EmailAttachmentUpload).where(
            EmailAttachmentUpload.id.in_(body.attachment_ids),
            EmailAttachmentUpload.organization_id == principal.organization_id,
        )
        attachment_rows = list((await db.execute(stmt)).scalars().all())
        found_ids = {row.id for row in attachment_rows}
        missing = [str(aid) for aid in body.attachment_ids if aid not in found_ids]
        if missing:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"attachment(s) not found: {', '.join(missing)}",
            )
        total_bytes = sum(row.size_bytes for row in attachment_rows)
        total_bytes += len((body.body_text or "").encode("utf-8"))
        total_bytes += len((body.body_html or "").encode("utf-8"))
        if total_bytes > MAX_EMAIL_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=ATTACHMENT_TOO_LARGE_DETAIL,
            )
```

- [ ] **Step 4: Add the `s3` dependency and pass attachments into the provider send call**

Add a new parameter to `create_email`'s signature (after the existing `email_provider` parameter):

```python
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
    s3: Annotated[S3MailClient, Depends(_get_s3_mail)],
```

(Add `from hailhq.core.s3_mail import S3MailClient` to the imports if the sed pass in Task 2 didn't already leave that import in this file — check first: `grep -n "S3MailClient" api/hailhq/api/routes/emails.py`.)

Immediately before the `try: result = await email_provider.send_email(...)` block (before line 401), build the provider attachment list:

```python
    provider_attachments: list[ProviderAttachment] = [
        ProviderAttachment(
            filename=row.filename,
            content_type=row.content_type,
            payload=await s3.fetch_raw(row.s3_key),
        )
        for row in attachment_rows
    ]
```

Add `attachments=provider_attachments or None,` as a new kwarg to the `email_provider.send_email(...)` call (alongside `headers={...}`).

- [ ] **Step 5: Persist `EmailAttachment` rows and stamp `first_used_at` after a successful send**

Immediately after the successful-send block's `await db.refresh(email)` (after line 465, before the usage-event write), add:

```python
    now_used = datetime.now(timezone.utc)
    for row in attachment_rows:
        db.add(
            EmailAttachment(
                email_id=email.id,
                filename=row.filename,
                content_type=row.content_type,
                size_bytes=row.size_bytes,
                s3_key=row.s3_key,
            )
        )
        if row.first_used_at is None:
            row.first_used_at = now_used
    if attachment_rows:
        await db.commit()
        await db.refresh(email)
```

- [ ] **Step 6: Add attachment ids to the audit log payload**

Edit the `write_audit_log(... action="email.create" ...)` call's `payload` dict (around line 371-382) — add one key:

```python
            "attachment_ids": [str(a) for a in (body.attachment_ids or [])],
```

- [ ] **Step 7: Run the tests, verify they pass**

```bash
cd api && uv run pytest tests/test_emails_api.py -v
```

Expected: all pass, including the four new attachment tests.

- [ ] **Step 8: Commit**

```bash
git add api/hailhq/api/routes/emails.py api/tests/test_emails_api.py
git commit -m "feat(api): wire EmailCreate.attachment_ids into POST /emails send + read paths"
```

---

### Task 8: Garbage-collect unused attachment uploads after 24h

**Files:**

- Create: `core/hailhq/core/email_attachment_gc.py`
- Modify: `api/hailhq/api/main.py` (wire a new worker into `lifespan`, mirroring `OutboundForwardWorker`)
- Create: `core/tests/test_email_attachment_gc.py`

**Interfaces:**

- Produces: `EmailAttachmentGcWorker(session_factory, s3_factory, poll_interval=3600.0)` with `run_forever()`/`tick() -> int`/`stop()`, same shape as `OutboundForwardWorker` (`core/hailhq/core/outbound_worker.py:67-113`).

- [ ] **Step 1: Write the failing test**

Create `core/tests/test_email_attachment_gc.py`:

```python
"""Unit tests for EmailAttachmentGcWorker.tick()."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.email_attachment_gc import EmailAttachmentGcWorker
from hailhq.core.models import EmailAttachmentUpload


async def _make_row(
    session: AsyncSession, *, created_at, first_used_at=None
) -> EmailAttachmentUpload:
    row = EmailAttachmentUpload(
        organization_id=uuid4(),
        filename="f.pdf",
        content_type="application/pdf",
        size_bytes=10,
        s3_key=f"outbound-attachments/x/{uuid4()}",
    )
    session.add(row)
    await session.flush()
    # Backdate created_at directly since the column is server-defaulted.
    row.created_at = created_at
    row.first_used_at = first_used_at
    await session.commit()
    await session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_tick_deletes_only_stale_unused_rows(async_session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    stale_unused = await _make_row(async_session, created_at=now - timedelta(hours=25))
    fresh_unused = await _make_row(async_session, created_at=now - timedelta(hours=1))
    stale_used = await _make_row(
        async_session,
        created_at=now - timedelta(hours=48),
        first_used_at=now - timedelta(hours=47),
    )

    def session_factory():
        return async_session  # type: ignore[return-value]

    s3 = AsyncMock()
    worker = EmailAttachmentGcWorker(session_factory=lambda: async_session, s3_factory=lambda: s3)

    processed = await worker.tick()

    assert processed == 1
    s3.delete.assert_awaited_once_with(stale_unused.s3_key)

    remaining_ids = set(
        (await async_session.execute(select(EmailAttachmentUpload.id))).scalars().all()
    )
    assert stale_unused.id not in remaining_ids
    assert fresh_unused.id in remaining_ids
    assert stale_used.id in remaining_ids
```

(This test assumes `S3MailClient` gains a `delete(key)` method — see Step 2 below — and reuses whatever `async_session` fixture the rest of `core/tests/` already provides; if that fixture is wrapped in a context manager rather than a bare session, adjust `session_factory` to match its actual shape.)

```bash
cd core && uv run pytest tests/test_email_attachment_gc.py -v
```

Expected: FAIL — `hailhq.core.email_attachment_gc` doesn't exist yet.

- [ ] **Step 2: Add a `delete` method to `S3MailClient`**

Edit `core/hailhq/core/s3_mail.py` — add after `put_attachment`:

```python
    async def delete(self, key: str) -> None:
        await asyncio.to_thread(
            self._client.delete_object, Bucket=self._bucket, Key=key
        )
```

- [ ] **Step 3: Write the GC worker**

Create `core/hailhq/core/email_attachment_gc.py`:

```python
"""Background sweep deleting never-used outbound attachment uploads.

Uploads are reusable across sends (see EmailAttachmentUpload) so nothing
deletes them on use — only rows that were uploaded and never referenced
by any POST /emails within 24h are garbage. Same run_forever/tick shape
as OutboundForwardWorker (core/hailhq/core/outbound_worker.py), gated in
api/hailhq/api/main.py's lifespan on the mail bucket being configured.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import EmailAttachmentUpload
from hailhq.core.s3_mail import S3MailClient

logger = logging.getLogger(__name__)

UNUSED_TTL = timedelta(hours=24)
GC_BATCH = 100

SessionFactory = Callable[[], "asynccontextmanager[AsyncSession]"]


class EmailAttachmentGcWorker:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        s3_factory: Callable[[], S3MailClient],
        poll_interval: float = 3600.0,
    ) -> None:
        self._session_factory = session_factory
        self._s3_factory = s3_factory
        self._s3: S3MailClient | None = None
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    def _get_s3(self) -> S3MailClient:
        if self._s3 is None:
            self._s3 = self._s3_factory()
        return self._s3

    async def run_forever(self) -> None:
        """Drive ``tick()`` until ``stop()`` is called."""
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # pragma: no cover — defensive; logged + retried
                logger.exception("email attachment GC tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> int:
        """Delete stale never-used uploads (S3 object + row). Returns count deleted."""
        cutoff = datetime.now(timezone.utc) - UNUSED_TTL
        async with self._session_factory() as session:
            stmt = (
                select(EmailAttachmentUpload)
                .where(EmailAttachmentUpload.first_used_at.is_(None))
                .where(EmailAttachmentUpload.created_at < cutoff)
                .limit(GC_BATCH)
            )
            rows = (await session.execute(stmt)).scalars().all()
            if not rows:
                return 0
            s3 = self._get_s3()
            for row in rows:
                try:
                    await s3.delete(row.s3_key)
                except Exception:
                    logger.warning(
                        "GC: failed to delete S3 object for upload_id=%s; "
                        "skipping row deletion this tick",
                        row.id,
                        exc_info=True,
                    )
                    continue
                await session.execute(
                    delete(EmailAttachmentUpload).where(
                        EmailAttachmentUpload.id == row.id
                    )
                )
            await session.commit()
            return len(rows)


__all__ = ["EmailAttachmentGcWorker", "UNUSED_TTL"]
```

- [ ] **Step 4: Run the test, verify it passes**

```bash
cd core && uv run pytest tests/test_email_attachment_gc.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Wire the worker into `lifespan`**

Edit `api/hailhq/api/main.py` — add the import (alongside the other worker imports, after line 17):

```python
from hailhq.core.email_attachment_gc import EmailAttachmentGcWorker
```

Add, in `lifespan`, right after the `forward_worker`/`forward_task` block (after line 156):

```python
    attachment_gc_worker: EmailAttachmentGcWorker | None = None
    attachment_gc_task: asyncio.Task | None = None
    if settings.hail_mail_bucket:
        attachment_gc_worker = EmailAttachmentGcWorker(
            session_factory=session_scope,
            s3_factory=lambda: S3MailClient(bucket=settings.hail_mail_bucket),
        )
        attachment_gc_task = asyncio.create_task(
            attachment_gc_worker.run_forever(), name="email-attachment-gc-worker"
        )
```

Add the matching shutdown call in the `finally` block, after the `forward_worker`/`forward_task` stop (after line 181):

```python
        if attachment_gc_worker is not None and attachment_gc_task is not None:
            await _stop_worker(attachment_gc_worker, attachment_gc_task)
```

(`S3MailClient` is already imported in this file per Task 2's rename.)

- [ ] **Step 6: Run the API test suite to confirm the app still boots cleanly**

```bash
cd api && uv run pytest tests/test_emails_api.py tests/test_email_attachments_api.py -v
```

Expected: all pass (this exercises app startup/shutdown via the test client fixture, which goes through `lifespan`).

- [ ] **Step 7: Commit**

```bash
git add core/hailhq/core/email_attachment_gc.py core/hailhq/core/s3_mail.py core/tests/test_email_attachment_gc.py api/hailhq/api/main.py
git commit -m "feat(core): add 24h GC worker for unused outbound attachment uploads"
```

---

### Task 9: MCP `HailClient` — upload method + `send_email` attachment_ids

**Files:**

- Modify: `mcp/hailhq/mcp/hail_client.py`

**Interfaces:**

- Produces: `HailClient.upload_email_attachment(*, filename: str, content: bytes, content_type: str) -> dict[str, Any]`.
- Produces: `HailClient.send_email(..., attachment_ids: list[str] | None = None, ...)`.

- [ ] **Step 1: Add `attachment_ids` to `send_email`**

Edit `mcp/hailhq/mcp/hail_client.py` — in `send_email`'s signature (lines 175-191), add after `message_type`:

```python
        message_type: str = "informational",
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
```

And in the body-building block (before `body = EmailCreate.model_validate(...)`), add:

```python
        if attachment_ids:
            fields["attachment_ids"] = list(attachment_ids)
```

- [ ] **Step 2: Add the upload method**

Add a new method after `send_email` (after line 229, before `get_email` at line 235):

```python
    # ------------------------------------------------------------------ #
    # POST /email-attachments
    # ------------------------------------------------------------------ #

    async def upload_email_attachment(
        self, *, filename: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        """POST /email-attachments — upload a file for outbound attachment.

        Returns ``{"id": ..., "filename": ..., "content_type": ...,
        "size_bytes": ...}``; the ``id`` is reusable via
        ``send_email(attachment_ids=[...])``.
        """
        resp = await self._client.post(
            "/email-attachments",
            files={"file": (filename, content, content_type)},
        )
        return _decode(resp)
```

- [ ] **Step 3: Add a unit test**

Check `mcp/tests/` for the existing `HailClient` test file (e.g. `test_hail_client.py`) and its transport-mocking convention (likely `httpx.MockTransport` passed via the `transport=` constructor kwarg, matching `HailClient.__init__`'s `transport: httpx.AsyncBaseTransport | None` param). Add:

```python
async def test_upload_email_attachment_posts_multipart():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(201, json={
            "id": "11111111-1111-1111-1111-111111111111",
            "filename": "a.pdf",
            "content_type": "application/pdf",
            "size_bytes": 3,
        })

    client = HailClient(
        base_url="https://test",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )
    result = await client.upload_email_attachment(
        filename="a.pdf", content=b"abc", content_type="application/pdf"
    )
    assert result["filename"] == "a.pdf"
    assert captured["request"].url.path == "/email-attachments"
    await client.aclose()
```

(Adjust imports/fixture wiring to match whatever `mcp/tests/test_hail_client.py` — or equivalent — already establishes; follow its exact pattern for constructing `HailClient` with a mock transport rather than the sketch above if it differs.)

```bash
cd mcp && uv run pytest tests/ -k "hail_client" -v
```

Expected: all pass, including the new upload test.

- [ ] **Step 4: Commit**

```bash
git add mcp/hailhq/mcp/hail_client.py
git commit -m "feat(mcp): add HailClient.upload_email_attachment + send_email attachment_ids"
```

---

### Task 10: MCP tools — `upload_email_attachment` tool + `send_email` update

**Files:**

- Modify: `mcp/hailhq/mcp/tools.py`

**Interfaces:**

- Consumes: `HailClient.upload_email_attachment`/`send_email(attachment_ids=...)` (Task 9).
- Produces: registered MCP tool `upload_email_attachment(content_base64, filename, content_type)`; updated `send_email` tool with `attachment_ids` param and docstring mention.

- [ ] **Step 1: Add the domain function**

Edit `mcp/hailhq/mcp/tools.py` — add near `send_email` (after its definition, before `get_call`):

```python
async def upload_email_attachment(
    *, client: HailClient, content_base64: str, filename: str, content_type: str
) -> dict[str, Any]:
    try:
        content = base64.b64decode(content_base64)
    except Exception:
        return {"error": "content_base64: invalid base64 encoding"}
    try:
        return await client.upload_email_attachment(
            filename=filename, content=content, content_type=content_type
        )
    except HailAPIError as exc:
        return _format_api_error(exc)
```

Add `import base64` to the top-level imports if not already present (check `grep -n "^import base64" mcp/hailhq/mcp/tools.py` first).

- [ ] **Step 2: Add `attachment_ids` to the `send_email` domain function**

Edit the existing `send_email` function (lines 141-184) — add `attachment_ids: list[str] | None = None` to its signature (after `message_type`) and pass it through to `client.send_email(..., attachment_ids=attachment_ids)`.

- [ ] **Step 3: Register the new tool + update `send_email`'s registration**

Edit `mcp/hailhq/mcp/tools.py`'s `register_tools` (starting line 383) — add `attachment_ids: list[str] | None = None` to `send_email_tool`'s signature (after `message_type: str = "informational",`), pass it through to the inner `send_email(...)` call, and extend the docstring with:

```
        ``attachment_ids`` are ids returned by ``upload_email_attachment``
        — upload a file first, then pass its id(s) here to attach it.
```

Add the new tool registration after `send_email_tool`:

```python
    @mcp_app.tool(name="upload_email_attachment")
    async def upload_email_attachment_tool(
        ctx: Context,
        content_base64: str,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        """Upload a file to attach to a future outbound email.

        ``content_base64`` is the file's raw bytes, base64-encoded.
        Returns ``{"id": ..., "filename": ..., "content_type": ...,
        "size_bytes": ...}`` — pass ``id`` in ``send_email``'s
        ``attachment_ids`` list. The id is reusable across many sends
        and expires in 24h if never used. Files over 10MB (combined
        with the message body and any other attachments, per send) are
        rejected — host large files externally and link to them in the
        body instead.
        """
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await upload_email_attachment(
                    client=client,
                    content_base64=content_base64,
                    filename=filename,
                    content_type=content_type,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}
```

- [ ] **Step 4: Write a test**

Check `mcp/tests/` for the existing tool-registration test convention (e.g. how `send_email`/`get_email_attachment` tools are exercised — likely via a FastMCP test client or by calling the registered closures directly). Add an equivalent test asserting: (a) `upload_email_attachment` decodes base64 and forwards to `client.upload_email_attachment`; (b) invalid base64 returns `{"error": "..."}"` without calling the client. Follow the exact existing test's mocking pattern for `HailClient`.

```bash
cd mcp && uv run pytest tests/ -k "tools or email" -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add mcp/hailhq/mcp/tools.py
git commit -m "feat(mcp): register upload_email_attachment tool; send_email gains attachment_ids"
```

---

### Task 11: Python SDK (`hail-sdk`) — attachment support

**Files:**

- Modify: `sdk/hail/_http.py` (add multipart support)
- Modify: `sdk/hail/models.py` (add `EmailAttachmentUploadResponse`; add `attachment_ids` to `EmailCreate`)
- Modify: `sdk/hail/client.py` (add `_EmailAttachmentsResource`; extend `_EmailsResource.create`)
- Modify: `sdk/tests/test_emails.py`

**Interfaces:**

- Produces: `client.email_attachments.create(*, filename, content, content_type) -> EmailAttachmentUploadResponse`.
- Produces: `client.emails.create(..., attachment_ids: list[str | UUID] | None = None)`.

- [ ] **Step 1: Add multipart support to `_HailHTTP`**

Edit `sdk/hail/_http.py` — add a new method after `request` (after line 1347, before `generate_idempotency_key`):

```python
    async def request_multipart(
        self,
        method: str,
        path: str,
        *,
        files: dict[str, tuple[str, bytes, str]],
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Like :meth:`request`, but sends a multipart/form-data body.

        ``files`` follows httpx's ``files=`` shape:
        ``{field: (filename, content, content_type)}``. Never retried —
        there's no idempotency-key convention for uploads yet, and
        re-sending a large body on a flaky connection is worse than
        failing fast.
        """
        client = self._ensure_client()
        merged_headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": "hail-sdk-python",
        }
        if headers:
            merged_headers.update(headers)
        resp = await client.request(
            method, path, files=files, headers=merged_headers
        )
        _raise_for_status(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()
```

- [ ] **Step 2: Add the model**

Edit `sdk/hail/models.py` — add after `EmailAttachmentResponse` (after line 900):

```python
class EmailAttachmentUploadResponse(BaseModel):
    """Returned by ``client.email_attachments.create(...)``.

    Mirrors ``core/hailhq/core/schemas.py:EmailAttachmentUploadResponse``.
    The returned ``id`` is reusable across many
    ``emails.create(attachment_ids=...)`` calls until Hail garbage-collects
    it (24h if never referenced by a send).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    content_type: str
    size_bytes: int
```

Add `"EmailAttachmentUploadResponse"` to `__all__` (after `"EmailAttachmentResponse"`, line 1114).

- [ ] **Step 3: Add `attachment_ids` to the SDK's `EmailCreate`**

Edit `sdk/hail/models.py`'s `EmailCreate` (lines 798-853) — add after `metadata`:

```python
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachment_ids: list[UUID] | None = None
```

- [ ] **Step 4: Add the resource class and wire it into `Client`**

Edit `sdk/hail/client.py` — add `EmailAttachmentUploadResponse` to the `from hail.models import (...)` block, and add a new resource class after `_EmailsResource` (after line 280, before `_EmailDomainsResource`):

```python
class _EmailAttachmentsResource:
    """``client.email_attachments.*`` — upload files to attach to outbound email."""

    def __init__(self, http: _HailHTTP) -> None:
        self._http = http

    async def create(
        self, *, filename: str, content: bytes, content_type: str
    ) -> EmailAttachmentUploadResponse:
        """Upload a file; returns a reusable id for ``emails.create(attachment_ids=...)``."""
        data = await self._http.request_multipart(
            "POST",
            "/email-attachments",
            files={"file": (filename, content, content_type)},
        )
        return EmailAttachmentUploadResponse.model_validate(data)
```

Add `attachment_ids: list[str | UUID] | None = None` to `_EmailsResource.create`'s signature (after `message_type`), and in its body-building block:

```python
        if attachment_ids:
            body["attachment_ids"] = [str(a) for a in attachment_ids]
```

In `Client.__init__`, add after `self.emails = _EmailsResource(self._http)`:

```python
        self.email_attachments = _EmailAttachmentsResource(self._http)
```

- [ ] **Step 5: Write the failing tests**

Add to `sdk/tests/test_emails.py`:

```python
@respx.mock
async def test_email_attachments_create(base_url: str, api_key: str) -> None:
    payload = {
        "id": "11111111-1111-1111-1111-111111111111",
        "filename": "invoice.pdf",
        "content_type": "application/pdf",
        "size_bytes": 3,
    }
    route = respx.post(f"{base_url}/email-attachments").mock(
        return_value=httpx.Response(201, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        att = await c.email_attachments.create(
            filename="invoice.pdf", content=b"abc", content_type="application/pdf"
        )
    assert att.filename == "invoice.pdf"
    assert att.size_bytes == 3
    assert route.calls.last.request.url.path == "/email-attachments"


@respx.mock
async def test_emails_create_with_attachment_ids(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/emails").mock(
        return_value=httpx.Response(201, json=make_email_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.emails.create(
            to=["a@example.com"],
            subject="hi",
            body_text="body",
            recipient_consent=True,
            attachment_ids=["11111111-1111-1111-1111-111111111111"],
        )
    body = json.loads(route.calls.last.request.content)
    assert body["attachment_ids"] == ["11111111-1111-1111-1111-111111111111"]
```

```bash
cd sdk && uv run pytest tests/test_emails.py -k "attachment" -v
```

Expected first run: FAIL (`client.email_attachments` doesn't exist / `attachment_ids` unsupported).
After Steps 1-4: 2 passed.

- [ ] **Step 6: Run the full SDK suite**

```bash
cd sdk && uv run pytest -v
```

Expected: all pass, 0 failures.

- [ ] **Step 7: Commit**

```bash
git add sdk/hail/_http.py sdk/hail/models.py sdk/hail/client.py sdk/tests/test_emails.py
git commit -m "feat(sdk): add client.email_attachments.create + emails.create(attachment_ids=...)"
```

(No version bump here — per `CHANGELOG.md`'s existing convention, `sdk-vX.Y.Z`/`cli-vX.Y.Z` are cut in a separate `chore(release):` commit alongside a `pyproject.toml` bump, not as part of the feature commit. Task 12 adds the CHANGELOG entry documenting this change ahead of that release.)

---

### Task 12: OpenAPI regen + Go CLI — upload command, `--attach` flag

**Files:**

- Modify: `openapi/openapi.yaml` (regenerated, not hand-edited)
- Modify: `cli/internal/client/client.gen.go` (regenerated, not hand-edited)
- Modify: `cli/internal/cmd/email.go` (`--attach`/`--attach-id` flags on `send`)
- Create: `cli/internal/cmd/email_attachment_upload.go`
- Modify: `cli/internal/cmd/email_test.go` (new test)
- Create: `cli/internal/cmd/email_attachment_upload_test.go`

**Interfaces:**

- Consumes: `POST /email-attachments` (Task 6), `POST /emails` with `attachment_ids` (Task 7) — both already regenerate into `openapi/openapi.yaml` automatically since they're live FastAPI routes.
- Produces: `hail email attachment-upload <file>` command; `--attach <path>` / `--attach-id <id>` flags on `hail email send`.

- [ ] **Step 1: Regenerate the OpenAPI spec**

```bash
cd /Users/r/playground/hail/api
uv run python -c "from hailhq.api.main import app; import sys, yaml; yaml.safe_dump(app.openapi(), sys.stdout, sort_keys=False)" > ../openapi/openapi.yaml
```

Expected: `openapi/openapi.yaml` diff shows a new `/email-attachments` path and `attachment_ids` added to the `EmailCreate` schema, plus the storage rename (Task 1-3) has no effect here since it's not API-surface.

```bash
git diff --stat openapi/openapi.yaml
```

Expected: non-zero changes.

- [ ] **Step 2: Regenerate the Go client**

```bash
cd /Users/r/playground/hail/cli && make codegen
```

Expected: `internal/client/client.gen.go` is rewritten; `git diff --stat internal/client/client.gen.go` shows changes including new types (`EmailAttachmentUploadResponse`, `HTTPValidationError` reuse) and a new client method for the `upload_email_attachment` operation.

- [ ] **Step 3: Confirm the generated upload method's exact signature**

```bash
grep -n "UploadEmailAttachment" cli/internal/client/client.gen.go
```

Expected (per oapi-codegen v2's convention for `multipart/form-data` request bodies — the operation is treated as an opaque-body upload since the explicit `operation_id="upload_email_attachment"` was set on the FastAPI route in Task 6): a generated pair

```go
func (c *Client) UploadEmailAttachmentWithBody(ctx context.Context, contentType string, body io.Reader, reqEditors ...RequestEditorFn) (*http.Response, error)
func (c *ClientWithResponses) UploadEmailAttachmentWithBodyWithResponse(ctx context.Context, contentType string, body io.Reader, reqEditors ...RequestEditorFn) (*UploadEmailAttachmentResponse, error)
```

**If the actual generated name/signature differs** (oapi-codegen version drift, or FastAPI emitting a different operationId shape), adjust every reference to it in Steps 4-5 below to match what `grep` actually found — treat the code below as the expected shape, not a guarantee.

- [ ] **Step 4: Write the shared upload helper + new CLI command**

Create `cli/internal/cmd/email_attachment_upload.go`:

```go
package cmd

import (
	"bytes"
	"context"
	"fmt"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"

	"github.com/hail-hq/hail/cli/internal/client"
)

func newEmailAttachmentUploadCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "attachment-upload <file>",
		Short: "Upload a file for use as an outbound email attachment",
		Long: `hail email attachment-upload — upload a local file, get back a
reusable attachment id.

Pass the returned id to ` + "`hail email send --attach-id <id>`" + ` (or,
simpler, use ` + "`hail email send --attach <file>`" + ` to upload and send
in one step). The id can be reused across many sends until Hail
garbage-collects it (24h if never referenced by a send). Files over 10MB
(combined with the message body and any other attachments, per send) are
rejected — host large files externally and link to them in the body
instead.`,
		Args: argsOrHelp(1, "<file>"),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEmailAttachmentUpload(cmd.Context(), opts, args[0])
		},
	}
	return cmd
}

func runEmailAttachmentUpload(ctx context.Context, opts *Options, path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read %s: %w", path, err)
	}
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	att, err := uploadEmailAttachment(ctx, apiClient, path, data)
	if err != nil {
		return err
	}
	if opts.JSON {
		return printJSON(opts.Stdout, att)
	}
	fmt.Fprintf(opts.Stdout, "✓ Attachment uploaded: %s\n", att.Id.String())
	fmt.Fprintf(opts.Stdout, "  Filename: %s\n", att.Filename)
	fmt.Fprintf(opts.Stdout, "  Size:     %d bytes\n", att.SizeBytes)
	return nil
}

// uploadEmailAttachment builds a multipart body from raw file bytes and
// posts it to /email-attachments. Shared by `attachment-upload` and
// `email send --attach`.
func uploadEmailAttachment(
	ctx context.Context, apiClient *client.ClientWithResponses, path string, data []byte,
) (*client.EmailAttachmentUploadResponse, error) {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	part, err := w.CreateFormFile("file", filepath.Base(path))
	if err != nil {
		return nil, fmt.Errorf("build upload: %w", err)
	}
	if _, err := part.Write(data); err != nil {
		return nil, fmt.Errorf("build upload: %w", err)
	}
	if err := w.Close(); err != nil {
		return nil, fmt.Errorf("build upload: %w", err)
	}

	resp, err := apiClient.UploadEmailAttachmentWithBodyWithResponse(ctx, w.FormDataContentType(), &buf)
	if err != nil {
		return nil, fmt.Errorf("attachment upload API: %w", err)
	}
	if resp.HTTPResponse.StatusCode != http.StatusCreated || resp.JSON201 == nil {
		return nil, apiError(resp.HTTPResponse.StatusCode, resp.Body)
	}
	return resp.JSON201, nil
}
```

Register it in `newEmailCmd` (`cli/internal/cmd/email.go`, after line 61's `cmd.AddCommand(newEmailAttachmentCmd(opts))`):

```go
	cmd.AddCommand(newEmailAttachmentUploadCmd(opts))
```

- [ ] **Step 5: Add `--attach`/`--attach-id` to `email send`**

Edit `cli/internal/cmd/email.go` — add two fields to `emailSendFlags` (after `messageType`):

```go
	attach            []string
	attachIDs         []string
```

Add two flags in `newEmailSendCmd` (after the `--message-type` flag, before `return cmd`):

```go
	cmd.Flags().StringArrayVar(&f.attach, "attach", nil, "Local file path to upload and attach (repeatable)")
	cmd.Flags().StringArrayVar(&f.attachIDs, "attach-id", nil, "Pre-uploaded attachment id from `hail email attachment-upload` (repeatable)")
```

In `runEmailSend`, move the `apiClient, err := opts.newClient(idempotencyEditor(idem))` call (currently at line 178) to **before** the `body := client.EmailCreate{...}` construction (line 142), so it's available for the upload step too. Then, right after the `body.MessageType = ...` block (after line 171) and before the now-relocated `apiClient` construction is used for the send, add:

```go
	var attachmentIDs []string
	attachmentIDs = append(attachmentIDs, f.attachIDs...)
	for _, path := range f.attach {
		data, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("--attach %s: %w", path, err)
		}
		att, err := uploadEmailAttachment(ctx, apiClient, path, data)
		if err != nil {
			return fmt.Errorf("--attach %s: %w", path, err)
		}
		attachmentIDs = append(attachmentIDs, att.Id.String())
	}
	if len(attachmentIDs) > 0 {
		ids := make([]openapi_types.UUID, 0, len(attachmentIDs))
		for _, s := range attachmentIDs {
			id, err := uuid.Parse(s)
			if err != nil {
				return fmt.Errorf("--attach-id %q: not a valid UUID: %w", s, err)
			}
			ids = append(ids, openapi_types.UUID(id))
		}
		body.AttachmentIds = &ids
	}
```

(Add `openapi_types "github.com/oapi-codegen/runtime/types"` to `email.go`'s imports — this package is already a dependency, used elsewhere in the `cmd` package, e.g. `email_attachment.go:10`.)

Update the `Long` help text (lines 72-83) to mention the new flags:

```go
Either --body or --body-html (or both) must be supplied. --body-file
and --body-html-file read content from disk. --attach uploads a local
file and attaches it (repeatable); --attach-id attaches a file already
uploaded via ` + "`hail email attachment-upload`" + ` (repeatable).
```

- [ ] **Step 6: Write the CLI tests**

Add to `cli/internal/cmd/email_test.go`:

```go
func TestEmailSend_AttachFlag(t *testing.T) {
	dir := t.TempDir()
	filePath := filepath.Join(dir, "invoice.pdf")
	if err := os.WriteFile(filePath, []byte("pdf bytes"), 0o644); err != nil {
		t.Fatalf("write attach file: %v", err)
	}

	mux := http.NewServeMux()
	var sendBody []byte
	mux.HandleFunc("/email-attachments", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":           "11111111-1111-1111-1111-111111111111",
			"filename":     "invoice.pdf",
			"content_type": "application/octet-stream",
			"size_bytes":   9,
		})
	})
	mux.HandleFunc("/emails", func(w http.ResponseWriter, r *http.Request) {
		sendBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		email := sampleEmailResponse()
		_ = json.NewEncoder(w).Encode(email)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "x@example.com",
		"--subject", "hi",
		"--body", "hello",
		"--attach", filePath,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body map[string]any
	if err := json.Unmarshal(sendBody, &body); err != nil {
		t.Fatalf("bad send body: %v", err)
	}
	ids, ok := body["attachment_ids"].([]any)
	if !ok || len(ids) != 1 || ids[0] != "11111111-1111-1111-1111-111111111111" {
		t.Fatalf("attachment_ids = %v", body["attachment_ids"])
	}
}
```

(Uses a bespoke `http.NewServeMux`-based fake server rather than the shared single-route `newFakeServer` helper, since this test needs two distinct routes to respond differently.)

Create `cli/internal/cmd/email_attachment_upload_test.go`:

```go
package cmd

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestEmailAttachmentUpload_HappyPath(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, map[string]any{
		"id":           "11111111-1111-1111-1111-111111111111",
		"filename":     "invoice.pdf",
		"content_type": "application/octet-stream",
		"size_bytes":   9,
	})

	dir := t.TempDir()
	filePath := filepath.Join(dir, "invoice.pdf")
	if err := os.WriteFile(filePath, []byte("pdf bytes"), 0o644); err != nil {
		t.Fatalf("write file: %v", err)
	}

	stdout, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "attachment-upload", filePath,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stdout, "11111111-1111-1111-1111-111111111111") {
		t.Errorf("stdout missing id: %q", stdout)
	}
	if srv.lastReq.URL.Path != "/email-attachments" {
		t.Fatalf("unexpected route: %s", srv.lastReq.URL.Path)
	}
}
```

- [ ] **Step 7: Run the CLI test suite**

```bash
cd cli && go test ./... -run "TestEmailSend|TestEmailAttachment" -v
```

Expected: all pass. If `UploadEmailAttachmentWithBodyWithResponse` doesn't match the actual generated name from Step 3, fix the references in `email_attachment_upload.go` first, then re-run.

- [ ] **Step 8: Commit**

```bash
git add openapi/openapi.yaml cli/internal/client/client.gen.go cli/internal/cmd/email.go cli/internal/cmd/email_attachment_upload.go cli/internal/cmd/email_test.go cli/internal/cmd/email_attachment_upload_test.go
git commit -m "feat(cli): add email attachment-upload command + email send --attach/--attach-id flags"
```

---

### Task 13: Docs + CHANGELOG

**Files:**

- Modify: `CHANGELOG.md`
- Modify: wherever email sending is documented for API/MCP/CLI usage (locate via `grep -rl "send_email\|POST /emails" docs/`)

- [ ] **Step 1: Find the doc(s) to extend**

```bash
grep -rl "send_email\|POST /emails" docs/ --include="*.md"
```

Add a short "Attachments" example to whichever file(s) this returns that document the email-send surface (likely `docs/setup/aws-ses.md` or a dedicated API-usage doc) — one `curl` example uploading then sending:

```bash
# 1. Upload
curl -s -X POST https://api.hail.so/email-attachments \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -F "file=@invoice.pdf" | jq -r .id
# → "3fa85f64-5717-4562-b3fc-2c963f66afa6"

# 2. Reference it in the send
curl -s -X POST https://api.hail.so/emails \
  -H "Authorization: Bearer $HAIL_API_KEY" -H "Content-Type: application/json" \
  -d '{"to":["a@example.com"],"subject":"Invoice","body_text":"See attached.","recipient_consent":true,"attachment_ids":["3fa85f64-5717-4562-b3fc-2c963f66afa6"]}'
```

- [ ] **Step 2: Add a CHANGELOG entry**

Edit `CHANGELOG.md` — insert a new `## [Unreleased]` section at the top (this repo has none yet; entries have historically been added retroactively as dated/versioned sections at release-cut time, but `CHANGELOG.md:3` already declares adherence to Keep a Changelog, whose "Unreleased" convention is the standard way to stage entries between releases):

```markdown
# Changelog

All notable changes to Hail are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Hail adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Outbound email attachments. Upload a file once, reference its id from as
many sends as you like.

- `POST /email-attachments` — upload a file (multipart/form-data,
  ≤10MB), get back a reusable id. `POST /emails` gains
  `attachment_ids` to attach one or more uploaded files; oversize
  requests (body + attachments combined, matching SES's 10MB raw-message
  cap) get a clear 422 suggesting a hosted link instead. Unused uploads
  are garbage-collected 24h after upload; used ones are kept
  indefinitely and reusable across sends.
- MCP: new `upload_email_attachment` tool; `send_email` gains
  `attachment_ids`.
- SDK: `client.email_attachments.create()`; `client.emails.create(...,
attachment_ids=...)`.
- CLI: `hail email attachment-upload <file>`; `hail email send` gains
  `--attach <file>` (upload + attach in one step) and `--attach-id <id>`.
- Internal: the S3 bucket/client backing inbound mail storage is renamed
  from "inbound" to a generic "mail" name (`HAIL_MAIL_NAME_PREFIX`
  replaces `HAIL_INBOUND_EMAIL_NAME_PREFIX`) since it now also holds
  outbound attachment uploads. No data migration; self-hosters recreate
  the bucket under the new prefix.

## [0.8.1] — 2026-07-02
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md docs/
git commit -m "docs: document outbound email attachments (API/MCP/SDK/CLI) + rename note"
```

---

## Final verification (run once, after all tasks)

```bash
cd /Users/r/playground/hail
(cd core && uv run pytest)
(cd api && uv run pytest)
(cd mcp && uv run pytest)
(cd sdk && uv run pytest)
(cd cli && go test ./...)
(cd infra/terraform && terraform validate)
grep -rn "S3InboundClient\|hail_inbound_bucket\|hail_inbound_email_name_prefix\|HAIL_INBOUND_BUCKET\|HAIL_INBOUND_EMAIL_NAME_PREFIX" --include="*.py" --include="*.go" --include="*.tf" --include=".env.example" .
```

Expected: every test suite passes; the final `grep` returns nothing (confirms the rename is complete with no stragglers anywhere in the tree).
