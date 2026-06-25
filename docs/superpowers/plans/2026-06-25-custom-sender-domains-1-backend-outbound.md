# Custom Sender Domains — Plan 1: Backend Outbound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a verified custom domain send mail from the customer's brand — configure a custom SES MAIL FROM (no "via amazonses.com"), surface all DNS records, auto-poll verification, and stop one org's delete from breaking another org's shared identity.

**Architecture:** All changes are in `hail/` (`core/` + `api/`). The SES provider gains MAIL FROM configuration and returns a generalized DNS-record list; a new background worker polls pending domains to `verified`/`failed`; the delete route guards a shared SES identity. The DB column `dkim_records` becomes `dns_records` (now holds CNAME/MX/TXT).

**Tech Stack:** Python 3, FastAPI (async), SQLAlchemy 2 (async) + Alembic, Pydantic v2, boto3 SESv2, pytest + `botocore.stub.Stubber`, `uv`.

This is **Plan 1 of 3** for the spec at `docs/superpowers/specs/2026-06-25-custom-sender-domains-design.md`. Plan 2 = backend inbound (catch-all rule, custom ingest, per-custom dedup). Plan 3 = website `/console/emails`.

## Global Constraints

- **Conventional Commits** for every commit message.
- **Lint/format/type:** `ruff` (`--fix`) + `black` for Python; `mypy` + `pytest` pass in CI. Pre-commit (husky + lint-staged) runs these on staged files.
- **Provider adapters live only in `core/hailhq/core/providers/<channel>/<name>.py`.** `api/` must not import boto3 directly — it goes through `core`.
- **Shared models go in `core/`.** No duplicated Email/EmailDomain schemas across services.
- **Regenerate `openapi/openapi.yaml` in the same commit as any API route/response-shape change** (command in Task 5).
- **New env vars: update `.env.example` in the same commit**, under the right provider section.
- **URLs are not strings** — use `hailhq.core.urls` helpers if any URL crosses a comparison (not expected in this plan).
- Tests run from the package dir: `cd core && uv run pytest …` or `cd api && uv run pytest …`. Migrations: `cd api && uv run alembic upgrade head`.

---

### Task 1: Generalize `DkimRecord` → `DnsRecord` and add `mail_from_status`

**Files:**

- Modify: `core/hailhq/core/providers/email/base.py`
- Test: `core/tests/providers/test_email_base_types.py` (create)

**Interfaces:**

- Produces:
  - `DnsRecord(name: str, value: str, type: Literal["CNAME","MX","TXT"] = "CNAME", priority: int | None = None)`
  - `DkimRecord = DnsRecord` (back-compat alias; existing imports keep working)
  - `ProviderIdentity` gains `mail_from_status: IdentityVerificationStatus | None = None`

- [ ] **Step 1: Write the failing test**

Create `core/tests/providers/test_email_base_types.py`:

```python
from __future__ import annotations

from hailhq.core.providers.email.base import DkimRecord, DnsRecord, ProviderIdentity


def test_dnsrecord_defaults_to_cname() -> None:
    r = DnsRecord(name="x._domainkey.acme.com", value="x.dkim.amazonses.com")
    assert r.type == "CNAME"
    assert r.priority is None


def test_dnsrecord_supports_mx_with_priority() -> None:
    r = DnsRecord(name="send.acme.com", value="feedback-smtp.us-east-1.amazonses.com",
                  type="MX", priority=10)
    assert r.type == "MX"
    assert r.priority == 10


def test_dkimrecord_is_dnsrecord_alias() -> None:
    assert DkimRecord is DnsRecord


def test_provider_identity_mail_from_status_optional() -> None:
    ident = ProviderIdentity(domain="acme.com", verification_status="pending",
                             dkim_records=[])
    assert ident.mail_from_status is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/providers/test_email_base_types.py -v`
Expected: FAIL — `ImportError: cannot import name 'DnsRecord'`.

- [ ] **Step 3: Write minimal implementation**

In `core/hailhq/core/providers/email/base.py`, replace the `DkimRecord` class with `DnsRecord` and add the alias; widen the `type` literal; add `priority`; add `mail_from_status` to `ProviderIdentity`:

```python
class DnsRecord(BaseModel):
    """One DNS record the operator must publish for a sending domain.

    Covers DKIM CNAMEs (SES Easy DKIM, 3 per domain) plus the custom
    MAIL FROM records: an MX to the SES feedback endpoint and a TXT SPF.
    Surfaced verbatim so the caller can paste them into their DNS console.
    """

    name: str
    value: str
    type: Literal["CNAME", "MX", "TXT"] = "CNAME"
    # Only meaningful for MX records; None otherwise.
    priority: int | None = None


# Back-compat alias — existing call sites import DkimRecord.
DkimRecord = DnsRecord
```

In `ProviderIdentity`, add below `verification_status`:

```python
    # SES MAIL FROM verification (the Return-Path subdomain). None until a
    # custom MAIL FROM is configured. Independent of `verification_status`,
    # which is DKIM-driven and gates sending.
    mail_from_status: IdentityVerificationStatus | None = None
```

Add `"DnsRecord"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/providers/test_email_base_types.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the existing SES provider tests (alias regression)**

Run: `cd core && uv run pytest tests/providers/test_ses_email.py -v`
Expected: PASS — `DkimRecord` imports still resolve via the alias.

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/providers/email/base.py core/tests/providers/test_email_base_types.py
git commit -m "feat(email): generalize DkimRecord to DnsRecord + add mail_from_status"
```

---

### Task 2: `create_identity` configures custom MAIL FROM and returns all records

**Files:**

- Modify: `core/hailhq/core/providers/email/ses.py`
- Test: `core/tests/providers/test_ses_email.py`

**Interfaces:**

- Consumes: `DnsRecord` (Task 1), `settings.aws_region`.
- Produces: `SesEmailProvider.create_identity(domain)` returns a `ProviderIdentity` whose `dkim_records` holds **5** records (3 DKIM CNAME + 1 MAIL FROM MX + 1 SPF TXT), `mail_from_domain == f"send.{domain}"`, `mail_from_status == "pending"`.

- [ ] **Step 1: Write the failing test**

Add to `core/tests/providers/test_ses_email.py` (under the `create_identity` section). Note the **two** stubbed calls, in call order:

```python
async def test_create_identity_sets_mail_from_and_returns_all_records(
    ses_client, stub: Stubber
) -> None:
    from hailhq.core.providers.email.base import DnsRecord

    stub.add_response(
        "create_email_identity",
        {"IdentityType": "DOMAIN",
         "DkimAttributes": {"Tokens": ["aaaaa", "bbbbb", "ccccc"], "Status": "PENDING"}},
        {"EmailIdentity": "acme.com"},
    )
    stub.add_response(
        "put_email_identity_mail_from_attributes",
        {},
        {"EmailIdentity": "acme.com",
         "MailFromDomain": "send.acme.com",
         "BehaviorOnMxFailure": "USE_DEFAULT_VALUE"},
    )

    provider = SesEmailProvider(client=ses_client)
    identity = await provider.create_identity("acme.com")

    assert identity.mail_from_domain == "send.acme.com"
    assert identity.mail_from_status == "pending"
    assert identity.verification_status == "pending"
    # 3 DKIM CNAMEs ...
    assert DnsRecord(name="aaaaa._domainkey.acme.com",
                     value="aaaaa.dkim.amazonses.com") in identity.dkim_records
    # ... plus the MAIL FROM MX (region from the us-east-1 test client) ...
    assert DnsRecord(name="send.acme.com",
                     value="feedback-smtp.us-east-1.amazonses.com",
                     type="MX", priority=10) in identity.dkim_records
    # ... plus the SPF TXT.
    assert DnsRecord(name="send.acme.com",
                     value="v=spf1 include:amazonses.com ~all",
                     type="TXT") in identity.dkim_records
    assert len(identity.dkim_records) == 5
    stub.assert_no_pending_responses()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/providers/test_ses_email.py::test_create_identity_sets_mail_from_and_returns_all_records -v`
Expected: FAIL — `put_email_identity_mail_from_attributes` was never called (Stubber reports an unexpected/leftover response), and `mail_from_domain` is `None`.

- [ ] **Step 3: Write minimal implementation**

In `core/hailhq/core/providers/email/ses.py`, add a helper near `_dkim_records_for`:

```python
def _mail_from_records(domain: str, region: str) -> list[DkimRecord]:
    """The two records for a custom MAIL FROM on ``send.<domain>``.

    The MX points at the region's SES feedback endpoint; the TXT is the SPF
    record authorising SES. Region-specific — unlike the DKIM CNAMEs.
    """
    mail_from = f"send.{domain}"
    return [
        DkimRecord(
            name=mail_from,
            value=f"feedback-smtp.{region}.amazonses.com",
            type="MX",
            priority=10,
        ),
        DkimRecord(
            name=mail_from,
            value="v=spf1 include:amazonses.com ~all",
            type="TXT",
        ),
    ]
```

Replace the body of `create_identity` with:

```python
    async def create_identity(self, domain: str) -> ProviderIdentity:
        response = await asyncio.to_thread(
            self._client.create_email_identity,
            EmailIdentity=domain,
        )
        tokens: list[str] = (response.get("DkimAttributes") or {}).get("Tokens") or []

        mail_from = f"send.{domain}"
        # Configure a custom MAIL FROM so the Return-Path aligns to the
        # customer's domain (no "via amazonses.com"). USE_DEFAULT_VALUE keeps
        # sending working while the MX/SPF DNS is still propagating.
        await asyncio.to_thread(
            self._client.put_email_identity_mail_from_attributes,
            EmailIdentity=domain,
            MailFromDomain=mail_from,
            BehaviorOnMxFailure="USE_DEFAULT_VALUE",
        )

        region = settings.aws_region or "us-east-1"
        records = _dkim_records_for(domain, tokens) + _mail_from_records(domain, region)
        return ProviderIdentity(
            domain=domain,
            verification_status="pending",
            dkim_records=records,
            mail_from_domain=mail_from,
            mail_from_status="pending",
            provider_resource_id=domain,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/providers/test_ses_email.py::test_create_identity_sets_mail_from_and_returns_all_records -v`
Expected: PASS.

- [ ] **Step 5: Update the old DKIM-only create test**

The existing `test_create_identity_returns_dkim_cnames` only stubs `create_email_identity` and asserts exactly 3 records — it now fails (a second SES call happens, and the list has 5 records). Add the `put_email_identity_mail_from_attributes` stub (same as Step 1) and change its assertion from `== [3 records]` to assert the 3 DKIM CNAMEs are a **subset**:

```python
    for token in ("aaaaa", "bbbbb", "ccccc"):
        assert DkimRecord(name=f"{token}._domainkey.acme.com",
                          value=f"{token}.dkim.amazonses.com") in identity.dkim_records
```

Run: `cd core && uv run pytest tests/providers/test_ses_email.py -v`
Expected: PASS (all create/get/delete tests green).

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/providers/email/ses.py core/tests/providers/test_ses_email.py
git commit -m "feat(email): configure custom MAIL FROM on SES identity create"
```

---

### Task 3: `get_identity` surfaces `mail_from_status`

**Files:**

- Modify: `core/hailhq/core/providers/email/ses.py`
- Test: `core/tests/providers/test_ses_email.py`

**Interfaces:**

- Produces: `get_identity` maps `MailFromAttributes.MailFromDomainStatus` (`SUCCESS`/`PENDING`/`FAILED`/`TEMPORARY_FAILURE`) onto `mail_from_status` via the existing `_status_from_ses` tri-state.

- [ ] **Step 1: Write the failing test**

Add to `core/tests/providers/test_ses_email.py` (under `get_identity`):

```python
async def test_get_identity_maps_mail_from_status(ses_client, stub: Stubber) -> None:
    stub.add_response(
        "get_email_identity",
        {"IdentityType": "DOMAIN", "VerificationStatus": "SUCCESS",
         "DkimAttributes": {"Status": "SUCCESS", "Tokens": ["aaaaa", "bbbbb", "ccccc"]},
         "MailFromAttributes": {"MailFromDomain": "send.acme.com",
                                "MailFromDomainStatus": "PENDING",
                                "BehaviorOnMxFailure": "USE_DEFAULT_VALUE"}},
        {"EmailIdentity": "acme.com"},
    )
    provider = SesEmailProvider(client=ses_client)
    identity = await provider.get_identity("acme.com")
    assert identity.verification_status == "verified"   # DKIM SUCCESS
    assert identity.mail_from_status == "pending"        # MAIL FROM still PENDING
    stub.assert_no_pending_responses()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/providers/test_ses_email.py::test_get_identity_maps_mail_from_status -v`
Expected: FAIL — `mail_from_status` is `None`.

- [ ] **Step 3: Write minimal implementation**

In `get_identity`, after the `mail_from = ...` line, compute the status and pass it:

```python
        mail_from_attrs = response.get("MailFromAttributes") or {}
        mail_from = mail_from_attrs.get("MailFromDomain")
        mail_from_status = (
            _status_from_ses(mail_from_attrs.get("MailFromDomainStatus"))
            if mail_from
            else None
        )

        return ProviderIdentity(
            domain=domain,
            verification_status=status,
            dkim_records=_dkim_records_for(domain, tokens),
            mail_from_domain=mail_from,
            mail_from_status=mail_from_status,
            provider_resource_id=domain,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/providers/test_ses_email.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/providers/email/ses.py core/tests/providers/test_ses_email.py
git commit -m "feat(email): surface MAIL FROM status from SES get_identity"
```

---

### Task 4: DB migration — rename `dkim_records` → `dns_records`, add `mail_from_status`

**Files:**

- Modify: `core/hailhq/core/models.py` (the `EmailDomain` model, ~line 364)
- Create: `api/migrations/versions/0015_email_domain_dns_records.py`
- Test: `core/tests/test_email_domain_model.py` (create)

**Interfaces:**

- Produces: `EmailDomain.dns_records` (JSONB, was `dkim_records`); `EmailDomain.mail_from_status: str | None` (TEXT NULL).

- [ ] **Step 1: Write the failing test**

Create `core/tests/test_email_domain_model.py`:

```python
from __future__ import annotations

import uuid

from hailhq.core.models import EmailDomain


def test_email_domain_has_dns_records_and_mail_from_status() -> None:
    sd = EmailDomain(
        organization_id=uuid.uuid4(),
        kind="custom",
        domain="acme.com",
        dns_records=[{"name": "x", "value": "y", "type": "CNAME"}],
        mail_from_status="pending",
    )
    assert sd.dns_records[0]["type"] == "CNAME"
    assert sd.mail_from_status == "pending"
    assert not hasattr(sd, "dkim_records")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_email_domain_model.py -v`
Expected: FAIL — `TypeError: 'dns_records' is an invalid keyword argument` (column still named `dkim_records`).

- [ ] **Step 3: Update the model**

In `core/hailhq/core/models.py`, rename the column and add the new one. Replace the `dkim_records` mapped column with:

```python
    # JSON array of {name, value, type, priority} entries (DKIM CNAMEs + the
    # custom MAIL FROM MX/SPF) — surfaced in the response so the tenant can
    # paste them straight into their DNS console.
    dns_records: Mapped[list[dict]] = mapped_column(
        "dns_records",
        JSONB,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
```

Directly below `mail_from_domain`, add:

```python
    # SES MAIL FROM verification status (pending/verified/failed); NULL until a
    # custom MAIL FROM is configured. Secondary to verification_status.
    mail_from_status: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Write the Alembic migration**

Create `api/migrations/versions/0015_email_domain_dns_records.py`:

```python
"""Rename email_domains.dkim_records -> dns_records; add mail_from_status.

The records column now carries DKIM CNAMEs plus the custom MAIL FROM MX/SPF,
so the DKIM-specific name is misleading. mail_from_status tracks the SES
MAIL FROM verification independently of the DKIM-driven verification_status.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("email_domains", "dkim_records", new_column_name="dns_records")
    op.add_column(
        "email_domains",
        sa.Column("mail_from_status", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("email_domains", "mail_from_status")
    op.alter_column("email_domains", "dns_records", new_column_name="dkim_records")
```

- [ ] **Step 5: Run the model test + apply the migration**

```bash
cd core && uv run pytest tests/test_email_domain_model.py -v
```

Expected: PASS.

```bash
cd api && uv run alembic upgrade head
```

Expected: applies `0015` with no error (requires local Postgres up — see CLAUDE.md "Dev commands").

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/models.py api/migrations/versions/0015_email_domain_dns_records.py core/tests/test_email_domain_model.py
git commit -m "feat(email): rename dkim_records to dns_records, add mail_from_status"
```

---

### Task 5: API persists + exposes `dns_records` and `mail_from_status`

**Files:**

- Modify: `core/hailhq/core/schemas.py` (`DkimRecordSchema`, `EmailDomainResponse`)
- Modify: `api/hailhq/api/routes/email_domains.py` (custom-create writer ~line 247; verify writer ~line 541)
- Modify: `openapi/openapi.yaml` (regenerate)
- Test: `api/tests/test_email_domains_api.py`

**Interfaces:**

- Consumes: `ProviderIdentity.dkim_records` + `.mail_from_status` (Tasks 2–3), `EmailDomain.dns_records` + `.mail_from_status` (Task 4).
- Produces: `EmailDomainResponse` exposes `dns_records: list[DnsRecordSchema]` and `mail_from_status: str | None`.

- [ ] **Step 1: Write the failing test**

Add to `api/tests/test_email_domains_api.py`:

```python
async def test_post_custom_returns_dns_records_with_mail_from(
    client: httpx.AsyncClient, org_and_key: tuple, email_mock: AsyncMock,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    resp = await client.post(
        "/email-domains", json={"kind": "custom", "domain": "acme.com"}, headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "dns_records" in body
    types = {r["type"] for r in body["dns_records"]}
    assert {"CNAME", "MX", "TXT"} <= types
    assert body["mail_from_domain"] == "send.acme.com"
    assert body["mail_from_status"] == "pending"
```

> Note: the shared `email_mock` (`api/tests/conftest.py:202`) is an `AsyncMock` for the provider. Its `create_identity` returns a `MagicMock` by default — set a real return so the route persists records. Add this fixture override at the top of the test, OR update the conftest `email_mock` to return a `ProviderIdentity`. Do the latter (Step 3) so every test benefits.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_email_domains_api.py::test_post_custom_returns_dns_records_with_mail_from -v`
Expected: FAIL — response has `dkim_records`, no `dns_records` / `mail_from_status`.

- [ ] **Step 3: Update schema, conftest mock, and the route writers**

In `core/hailhq/core/schemas.py`:

- Rename `class DkimRecordSchema` → `class DnsRecordSchema`, widen `type` and add `priority`:

```python
class DnsRecordSchema(BaseModel):
    name: str
    value: str
    type: Literal["CNAME", "MX", "TXT"] = "CNAME"
    priority: int | None = None
```

- In `EmailDomainResponse`, replace `dkim_records: list[DkimRecordSchema]` with `dns_records: list[DnsRecordSchema]` and add `mail_from_status: str | None = None` below `mail_from_domain`.

In `api/hailhq/api/routes/email_domains.py`, the custom-create writer (`sd = EmailDomain(...)` after `identity = await email_provider.create_identity(domain)`): change `dkim_records=[r.model_dump() for r in identity.dkim_records]` to `dns_records=[...]` and add `mail_from_status=identity.mail_from_status`. In the **verify** writer (`update(EmailDomain)...values(...)`), change `dkim_records=` → `dns_records=` and add `mail_from_status=identity.mail_from_status`. The hail-mail create writer sets `dkim_records=[]` → change to `dns_records=[]`.

In `api/tests/conftest.py`, make the `email_mock.create_identity` return a real identity (find the `email_mock` fixture; add):

```python
    from hailhq.core.providers.email.base import DnsRecord, ProviderIdentity

    def _fake_create_identity(domain: str) -> ProviderIdentity:
        return ProviderIdentity(
            domain=domain, verification_status="pending",
            dkim_records=[
                DnsRecord(name=f"t._domainkey.{domain}", value="t.dkim.amazonses.com"),
                DnsRecord(name=f"send.{domain}",
                          value="feedback-smtp.us-east-1.amazonses.com",
                          type="MX", priority=10),
                DnsRecord(name=f"send.{domain}",
                          value="v=spf1 include:amazonses.com ~all", type="TXT"),
            ],
            mail_from_domain=f"send.{domain}", mail_from_status="pending",
            provider_resource_id=domain,
        )

    email_mock.create_identity.side_effect = _fake_create_identity
```

Also update the `email_mock.get_identity` return used by the verify test so it carries `mail_from_status` (set `verification_status="verified"`, `mail_from_status="verified"`).

- [ ] **Step 4: Run the email-domains test suite**

Run: `cd api && uv run pytest tests/test_email_domains_api.py -v`
Expected: PASS. Fix any test that still references `dkim_records` (rename to `dns_records`).

- [ ] **Step 5: Regenerate the OpenAPI spec**

With the API running locally (`cd api && uv run uvicorn hailhq.api.main:app --port 8080`), in another shell:

```bash
curl -s http://localhost:8080/openapi.json \
  | python -c "import json, sys, yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" \
  > openapi/openapi.yaml
```

Confirm the `EmailDomainResponse` schema now shows `dns_records` + `mail_from_status`.

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/schemas.py api/hailhq/api/routes/email_domains.py api/tests/test_email_domains_api.py api/tests/conftest.py openapi/openapi.yaml
git commit -m "feat(email): expose dns_records + mail_from_status on email-domain API"
```

---

### Task 6: `DomainVerificationWorker` — auto-poll pending custom domains

**Files:**

- Create: `core/hailhq/core/domain_verification_worker.py`
- Test: `core/tests/test_domain_verification_worker.py` (create)

**Interfaces:**

- Consumes: `EmailProvider.get_identity` (Task 3), `EmailDomain` (Task 4).
- Produces:
  - `class DomainVerificationWorker(*, session_factory, provider_factory: Callable[[], EmailProvider], verify_ttl_seconds: int = 259200, poll_interval: float = 120.0)`
  - `async def tick(self) -> int` — polls every pending custom row once; returns count processed.
  - `async def run_forever(self) -> None`, `async def stop(self) -> None` (same shape as `OutboundForwardWorker`).
  - On `get_identity` → `verified`: set `verification_status='verified'`, `verified_at=now`, refresh `dns_records`/`mail_from_status`. On rows older than `verify_ttl_seconds` still `pending`: set `verification_status='failed'`.

- [ ] **Step 1: Write the failing test**

Create `core/tests/test_domain_verification_worker.py`:

```python
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from hailhq.core.domain_verification_worker import DomainVerificationWorker
from hailhq.core.models import EmailDomain
from hailhq.core.providers.email.base import ProviderIdentity


async def _insert_pending(session_factory, *, age_hours: float = 0.0) -> uuid.UUID:
    async with session_factory() as s:
        sd = EmailDomain(
            organization_id=uuid.uuid4(), kind="custom", domain="acme.com",
            verification_status="pending", dns_records=[], provider="ses",
            created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        )
        s.add(sd)
        await s.commit()
        await s.refresh(sd)
        return sd.id


@pytest.mark.usefixtures("db")
async def test_tick_flips_pending_to_verified(session_factory) -> None:
    sd_id = await _insert_pending(session_factory)
    provider = AsyncMock()
    provider.get_identity.return_value = ProviderIdentity(
        domain="acme.com", verification_status="verified", dkim_records=[],
        mail_from_domain="send.acme.com", mail_from_status="verified",
    )
    worker = DomainVerificationWorker(
        session_factory=session_factory, provider_factory=lambda: provider,
    )
    processed = await worker.tick()
    assert processed == 1
    async with session_factory() as s:
        sd = (await s.execute(select(EmailDomain).where(EmailDomain.id == sd_id))).scalar_one()
    assert sd.verification_status == "verified"
    assert sd.verified_at is not None


@pytest.mark.usefixtures("db")
async def test_tick_fails_stale_pending_past_ttl(session_factory) -> None:
    sd_id = await _insert_pending(session_factory, age_hours=80)  # > 72h
    provider = AsyncMock()
    provider.get_identity.return_value = ProviderIdentity(
        domain="acme.com", verification_status="pending", dkim_records=[],
    )
    worker = DomainVerificationWorker(
        session_factory=session_factory, provider_factory=lambda: provider,
        verify_ttl_seconds=72 * 3600,
    )
    await worker.tick()
    async with session_factory() as s:
        sd = (await s.execute(select(EmailDomain).where(EmailDomain.id == sd_id))).scalar_one()
    assert sd.verification_status == "failed"
```

> The `session_factory` / `db` fixtures: mirror whatever `core/tests/test_outbound_worker.py` uses (same async-session test harness). If they live in `core/tests/conftest.py`, reuse them; otherwise copy that file's fixture setup.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_domain_verification_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: hailhq.core.domain_verification_worker`.

- [ ] **Step 3: Write the worker**

Create `core/hailhq/core/domain_verification_worker.py` (model on `outbound_worker.py`):

```python
"""Background poller that drives pending custom email domains to a terminal
verification state.

On-demand verification (POST /email-domains/{id}/verify) still exists; this
worker just removes the need for the tenant to click it. Each tick re-polls
the provider for every pending custom row, flips it to verified when DKIM
lands, and fails it once it has been pending past the TTL (72h, matching
Resend) so a never-published domain doesn't poll forever.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, update

from hailhq.core.models import EmailDomain
from hailhq.core.providers.email.base import EmailProvider

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 72 * 3600  # 72h


class DomainVerificationWorker:
    def __init__(
        self,
        *,
        session_factory,
        provider_factory: Callable[[], EmailProvider],
        verify_ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        poll_interval: float = 120.0,
    ) -> None:
        self._session_factory = session_factory
        self._provider_factory = provider_factory
        self._verify_ttl = timedelta(seconds=verify_ttl_seconds)
        self._poll_interval = poll_interval
        self._provider: EmailProvider | None = None
        self._stop = asyncio.Event()

    def _get_provider(self) -> EmailProvider:
        if self._provider is None:
            self._provider = self._provider_factory()
        return self._provider

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.tick()
            except Exception:  # pragma: no cover — defensive; logged + retried
                logger.exception("domain verification worker tick failed")
                processed = 0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> int:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(EmailDomain)
                    .where(EmailDomain.kind == "custom")
                    .where(EmailDomain.verification_status == "pending")
                )
            ).scalars().all()

        processed = 0
        now = datetime.now(timezone.utc)
        for row in rows:
            processed += 1
            try:
                identity = await self._get_provider().get_identity(row.domain)
            except Exception:
                logger.warning("get_identity failed for domain=%s", row.domain, exc_info=True)
                continue

            if identity.verification_status == "verified":
                values = {
                    "verification_status": "verified",
                    "verified_at": now,
                    "dns_records": [r.model_dump() for r in identity.dkim_records],
                    "mail_from_status": identity.mail_from_status,
                }
            elif identity.verification_status == "failed":
                values = {"verification_status": "failed"}
            elif self._is_past_ttl(row.created_at, now):
                values = {"verification_status": "failed"}
            else:
                values = {"mail_from_status": identity.mail_from_status}

            async with self._session_factory() as session:
                await session.execute(
                    update(EmailDomain).where(EmailDomain.id == row.id).values(**values)
                )
                await session.commit()
        return processed

    def _is_past_ttl(self, created_at: datetime, now: datetime) -> bool:
        created = created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return now - created > self._verify_ttl


__all__ = ["DomainVerificationWorker"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_domain_verification_worker.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/domain_verification_worker.py core/tests/test_domain_verification_worker.py
git commit -m "feat(email): add DomainVerificationWorker for auto-poll verification"
```

---

### Task 7: Wire the worker into the API lifespan + config + `.env.example`

**Files:**

- Modify: `core/hailhq/core/config.py` (add `hail_domain_verify_poll_seconds`)
- Modify: `api/hailhq/api/main.py` (lifespan)
- Modify: `.env.example`
- Test: `api/tests/test_domain_verification_lifespan.py` (create)

**Interfaces:**

- Consumes: `DomainVerificationWorker` (Task 6), `SesEmailProvider`, `session_scope`.
- Produces: setting `settings.hail_domain_verify_poll_seconds: int = 120` (`0` disables the worker).

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_domain_verification_lifespan.py`:

```python
from __future__ import annotations

from hailhq.core.config import settings


def test_domain_verify_poll_setting_defaults_to_120() -> None:
    assert settings.hail_domain_verify_poll_seconds == 120


def test_main_lifespan_references_domain_verification_worker() -> None:
    import inspect
    from hailhq.api import main
    src = inspect.getsource(main.lifespan)
    assert "DomainVerificationWorker" in src
    assert "hail_domain_verify_poll_seconds" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_domain_verification_lifespan.py -v`
Expected: FAIL — `AttributeError: ... hail_domain_verify_poll_seconds`.

- [ ] **Step 3: Add the setting, env example, and lifespan wiring**

In `core/hailhq/core/config.py`, under the Email section:

```python
    # Background re-poll cadence (seconds) for pending custom sender domains.
    # The worker flips them to verified once DKIM lands and fails them past a
    # 72h TTL. Set 0 to disable (rely on POST /email-domains/{id}/verify only).
    hail_domain_verify_poll_seconds: int = 120
```

In `.env.example`, under the email section:

```bash
# Auto-poll cadence (seconds) for pending custom sender domains. 0 disables.
HAIL_DOMAIN_VERIFY_POLL_SECONDS=120
```

In `api/hailhq/api/main.py`: add the import near the other worker imports:

```python
from hailhq.core.domain_verification_worker import DomainVerificationWorker
```

In `lifespan`, after the `forward_worker` block and before `try: yield`, add:

```python
    verify_worker: DomainVerificationWorker | None = None
    verify_task: asyncio.Task | None = None
    if settings.hail_domain_verify_poll_seconds > 0:
        verify_worker = DomainVerificationWorker(
            session_factory=session_scope,
            provider_factory=SesEmailProvider,
            poll_interval=settings.hail_domain_verify_poll_seconds,
        )
        verify_task = asyncio.create_task(
            verify_worker.run_forever(), name="domain-verification-worker"
        )
```

In the `finally:` block, alongside the other `_stop_worker` calls:

```python
        if verify_worker is not None and verify_task is not None:
            await _stop_worker(verify_worker, verify_task)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_domain_verification_lifespan.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/config.py api/hailhq/api/main.py .env.example api/tests/test_domain_verification_lifespan.py
git commit -m "feat(email): run domain verification worker in API lifespan"
```

---

### Task 8: Shared-identity delete guard

**Files:**

- Modify: `api/hailhq/api/routes/email_domains.py` (`delete_email_domain`)
- Test: `api/tests/test_email_domains_api.py`

**Interfaces:**

- Consumes: `EmailProvider.delete_identity`, `EmailDomain`.
- Produces: `delete_email_domain` skips `provider.delete_identity(domain)` when **another org** still has a row for the same `domain`; it always deletes the caller's row.

- [ ] **Step 1: Write the failing test**

Add to `api/tests/test_email_domains_api.py` (uses the `insert_org_and_key` conftest helper to make a second org sharing the domain):

```python
async def test_delete_skips_ses_when_another_org_shares_domain(
    client: httpx.AsyncClient, org_and_key: tuple, email_mock: AsyncMock,
    async_session: AsyncSession,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains", json={"kind": "custom", "domain": "acme.com"}, headers=headers,
    )
    domain_id = created.json()["id"]

    # A second org also registers acme.com (shared SES identity, one AWS acct).
    other_org_id = uuid.uuid4()
    async_session.add(EmailDomain(
        organization_id=other_org_id, kind="custom", domain="acme.com",
        verification_status="verified", dns_records=[], provider="ses",
    ))
    await async_session.commit()

    resp = await client.delete(f"/email-domains/{domain_id}", headers=headers)
    assert resp.status_code == 204
    # SES identity must NOT be deleted — the other org still sends through it.
    email_mock.delete_identity.assert_not_called()
    # The caller's row is gone; the other org's row remains.
    remaining = (await async_session.execute(
        select(EmailDomain).where(EmailDomain.domain == "acme.com")
    )).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].organization_id == other_org_id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_email_domains_api.py::test_delete_skips_ses_when_another_org_shares_domain -v`
Expected: FAIL — `delete_identity` was called (`assert_not_called` raises).

- [ ] **Step 3: Add the guard**

In `delete_email_domain` (`api/hailhq/api/routes/email_domains.py`), the block that calls SES after commit is:

```python
    if deleted_kind == "custom":
        try:
            await email_provider.delete_identity(deleted_domain)
        except Exception:
            ...
```

Wrap the SES call in a cross-org check. **Before** deleting the row (while the session is still usable for a read), or right before the SES call, query for another org's row:

```python
    if deleted_kind == "custom":
        # Single AWS account → one shared SES identity per domain name. Only
        # tear it down at SES when no other org still sends through it.
        other = (
            await db.execute(
                select(EmailDomain.id)
                .where(EmailDomain.domain == deleted_domain)
                .where(EmailDomain.organization_id != principal.organization_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if other is None:
            try:
                await email_provider.delete_identity(deleted_domain)
            except Exception:
                logger.warning(
                    "ses delete_identity failed after DB delete for org=%s domain=%s "
                    "(SES identity may be orphaned)",
                    principal.organization_id, deleted_domain, exc_info=True,
                )
        else:
            logger.info(
                "kept SES identity for domain=%s — still used by another org",
                deleted_domain,
            )
```

> The caller's own row is already deleted+committed by this point, so the `!= organization_id` query correctly returns only _other_ orgs' rows. Confirm the `select` import is present (it is, used elsewhere in the file).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_email_domains_api.py -v`
Expected: PASS — including the existing `test_delete_custom_calls_provider_and_deletes_row` (single-org case still calls `delete_identity`).

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/email_domains.py api/tests/test_email_domains_api.py
git commit -m "fix(email): don't delete a SES identity another org still shares"
```

---

## Final verification

- [ ] **Run the full backend test suites**

```bash
cd core && uv run pytest -q
cd ../api && uv run pytest -q
```

Expected: all green.

- [ ] **Lint + type check**

```bash
cd core && uv run ruff check . && uv run mypy hailhq
cd ../api && uv run ruff check . && uv run mypy hailhq
```

Expected: no errors.

- [ ] **Confirm migration round-trips**

```bash
cd api && uv run alembic downgrade -1 && uv run alembic upgrade head
```

Expected: `0015` down then up cleanly.

## Spec coverage (Plan 1 scope)

- Gap **A** (custom MAIL FROM + records) → Tasks 1, 2, 3, 4, 5.
- Gap **B** (auto-poll verification) → Tasks 6, 7.
- Gap **C** (shared-identity delete guard) → Task 8.
- `dns_records` rename + `mail_from_status` → Tasks 4, 5.

Out of scope here (later plans): catch-all receipt rule, custom-domain inbound ingest, per-custom dedup + domain-name payload (Plan 2); `/console/emails` UI (Plan 3).
