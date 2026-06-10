# Inbound-Email & Webhooks — Review-Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the merge-blocking and high-value findings from the inbound-email/webhooks code review so the feature is correct, secure, and mergeable.

**Architecture:** The feature already exists in the working tree (uncommitted). This plan _corrects_ it in place — no greenfield. The corrections cluster into: persistence correctness (multi-org collision + idempotency), webhook delivery (spec-conformant envelope, encrypted-at-rest secrets, write-time URL validation), MIME robustness, forwarding correctness, URL-helper hygiene, doc-vs-behavior alignment, and completing the client surface that actually ships.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / Alembic / Pydantic v2 / `cryptography` (Fernet) · Go (Cobra CLI) · Python SDK (`hail-sdk`) · OpenAPI 3.

---

## Scope: included vs. excluded

These boundaries were confirmed with the requester. **Read this before starting** — it is the contract for what this plan does and does not touch.

### ✅ Included

| ID      | Finding                                                                 | Phase |
| ------- | ----------------------------------------------------------------------- | ----- |
| **C1**  | Multi-org inbound delivery 500s on global `provider_message_id` UNIQUE  | 1     |
| **C2**  | Idempotency `IntegrityError` backstop claimed but absent                | 1     |
| **C3**  | Delivered webhook envelope missing top-level `id`/`created_at` (§5.2)   | 2     |
| **C4**  | `hail webhooks` command defined but never registered                    | 9     |
| **I1**  | Webhook secret in-process cache → **encrypted-at-rest redesign**        | 3     |
| **I2**  | No HTTPS / private-network validation at webhook-write time             | 4     |
| **I3**  | `message/rfc822` parts corrupt the MIME parse                           | 5     |
| **I4**  | `get_content()` crashes on unknown/garbage charset                      | 5     |
| **I5**  | Forward rate limiter is per-org, contradicts per-domain contract        | 6     |
| **I6**  | Per-target loop check `return`s, dropping valid sibling targets         | 6     |
| **I7**  | SDK drops `webhook_secret` + omits inbound fields                       | 9     |
| **I10** | URL-invariant violations (`.rstrip("/")` + f-string joins)              | 7     |
| **I11** | `GET /emails/{id}` never populates `raw_url`/`attachments`              | 8     |
| **I12** | `hail_inbound_org_rate_per_hour` configured but never enforced          | 8     |
| —       | Docs reconciliation (keep `hail webhooks`, drop unshipped CLI examples) | 10    |
| —       | OpenAPI regen + full-suite verification                                 | 11    |

### ❌ Excluded (explicitly out of scope for this plan)

| ID                      | Finding                                                                                                                                                                                                                                                                                                                                                      | Why excluded / where tracked                                                                                                                                                                            |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **I8**                  | No `hail email list`, no inbound-config CLI subcommands (PATCH/rotate)                                                                                                                                                                                                                                                                                       | Deferred to a follow-up CLI plan. Docs are trimmed (Phase 10) so nothing references these. SDK already covers inbound config via PATCH.                                                                 |
| **M1–M13** (all Minors) | DNS-rebind/IPv6 guard, inline `cid:` images, S3-orphan-on-rollback, double MIME parse, hardcoded `provider="ses"`, redeliver state guard, dead `secret_hash` (subsumed by I1), untyped SDK webhooks resource, missing SDK webhook tests, CLI `--json` ignore, over-strict `EmailSummary.email_domain_id`, `_sha256` import placement, bcrypt-vs-sha doc note | Tracked as a follow-up "polish" issue. None are correctness/security blockers. (Note: the dead `secret_hash` column concern is **resolved** by I1's column rename, and `_sha256` is **deleted** by I1.) |
| —                       | `docs/operations/litellm-upstream.md`, `docs/operations/refresh-costs.md`                                                                                                                                                                                                                                                                                    | Unrelated cost-dataset docs bundled by accident. The requester will split them into a separate commit; this plan does not touch them.                                                                   |

---

## File structure map

**New files**

- `api/migrations/versions/0009_provider_message_id_partial_unique.py` — C1 constraint fix (released schema, must be a _new_ revision).
- `core/hailhq/core/secret_cipher.py` — Fernet encrypt/decrypt for webhook secrets (I1).
- `core/tests/test_secret_cipher.py`
- `core/tests/fixtures/inbound/nested_rfc822.eml` — I3 fixture.
- `core/tests/fixtures/inbound/bad_charset.eml` — I4 fixture.
- `cli/internal/cmd/webhooks_test.go` — C4 regression (command registered).
- `api/tests/test_internal_ses_events_multi_org.py` — C1 regression.

**Modified files**

- `core/hailhq/core/models.py` — drop global unique on `provider_message_id`; rename secret columns (I1).
- `api/migrations/versions/0007_email_inbound_schema.py` — rename `webhook_secret_hash` → `webhook_secret_encrypted` (I1, edit-in-place, unreleased).
- `api/migrations/versions/0008_webhook_subscriptions.py` — rename `secret_hash` → `secret_encrypted` (I1, edit-in-place, unreleased).
- `core/hailhq/core/email_ingest.py` — idempotency savepoint (C2); per-domain rate-limit threading (I5); loop continue/return (I6); `join_url` (I10).
- `core/hailhq/core/forward_limiter.py` — per-domain filter (I5).
- `core/hailhq/core/email_mime.py` — `message/rfc822` leaf + charset-safe decode (I3, I4).
- `core/hailhq/core/webhook_worker.py` — §5.2 envelope at send (C3); decrypt-from-row secret (I1).
- `core/hailhq/core/webhook_fanout.py` — store `organization_id`+`data` only (C3).
- `core/hailhq/core/config.py` — add `hail_webhook_secret_key` (I1).
- `api/hailhq/api/routes/webhooks.py` — encrypt+store secret, write-time URL validation (I1, I2).
- `api/hailhq/api/routes/email_domains.py` — encrypt+store secret, write-time URL validation (I1, I2).
- `api/hailhq/api/routes/internal/ses_events.py` — `join_url` for `api_base_url` (I10).
- `api/hailhq/api/routes/emails.py` — populate `raw_url`/`attachments` on GET (I11).
- `api/hailhq/api/main.py` — wire `decrypt` into worker instead of `resolve_secret` (I1).
- `core/hailhq/core/schemas.py` — `WebhookSubscriptionPatch`/`Create` validators are unchanged; validation moves to routes (I2). (No edit unless noted.)
- `cli/internal/cmd/root.go` — register `newWebhooksCmd` (C4).
- `sdk/hail/models.py` — inbound fields on `EmailDomainResponse`/`EmailResponse` + `EmailAttachmentResponse` (I7).
- `.env.example` — `HAIL_WEBHOOK_SECRET_KEY` (I1).
- `docs/setup/aws-ses.md`, `docs/architecture.md` — reconcile to shipped surface (Phase 10).
- `openapi/openapi.yaml` — regenerated (Phase 11).

**Deleted files**

- `api/hailhq/api/webhook_secrets.py` — in-process cache replaced by encrypted-at-rest (I1).

---

## Phase 0 — Prep

### Task 0: Branch + green baseline

**Files:** none (environment only).

- [ ] **Step 1: Create a working branch**

```bash
cd /Users/r/playground/hail
git checkout -b fix/inbound-email-review
```

- [ ] **Step 2: Bring up data services + migrate**

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d postgres minio
cd api && uv run alembic upgrade head
```

- [ ] **Step 3: Capture the baseline test state**

Run each suite and note pass/fail counts (some will fail — those failures are what this plan fixes):

```bash
cd /Users/r/playground/hail/core && uv run pytest -q
cd /Users/r/playground/hail/api  && uv run pytest -q
cd /Users/r/playground/hail/sdk  && uv run pytest -q
cd /Users/r/playground/hail/cli  && go test ./...
```

Expected: core/api/sdk mostly pass; the bugs this plan fixes are largely _latent_ (not covered by existing tests), so a green baseline here is normal — the new tests we add are what expose them.

---

## Phase 1 — Critical persistence fixes (C1, C2)

### Task 1: Migration 0009 — make `provider_message_id` uniqueness outbound-only (C1)

**Why:** `provider_message_id` is globally `UNIQUE` (from released migration `0005`). Inbound, the same SES receipt `messageId` is written on one `Email` row _per recipient org_ (`email_ingest.py:140`), so a 2-org delivery collides and 500s, rolling back both rows. Nothing looks up bounces by `provider_message_id` (the SES-events route correlates by `message_id`), so the global constraint only needs to protect **outbound** dedup. Convert it to a partial unique index `WHERE direction='outbound'`.

**Files:**

- Create: `api/migrations/versions/0009_provider_message_id_partial_unique.py`
- Modify: `core/hailhq/core/models.py:515-517`
- Test: `api/tests/test_migrations.py`, `api/tests/test_internal_ses_events_multi_org.py` (created in Task 3 of this phase below — Step references it)

- [ ] **Step 1: Write the failing migration round-trip assertion**

Add to `api/tests/test_migrations.py` (it already round-trips upgrade→downgrade; add a constraint-shape check). Append this test:

```python
def test_provider_message_id_unique_is_outbound_only(alembic_runner, alembic_engine):
    """After 0009 the global unique is gone; only a partial (outbound) one remains."""
    alembic_runner.migrate_up_to("0009")
    with alembic_engine.connect() as conn:
        # The old global unique constraint must be gone.
        global_uq = conn.exec_driver_sql(
            "SELECT 1 FROM pg_constraint WHERE conname = 'emails_provider_message_id_key'"
        ).fetchone()
        assert global_uq is None
        # A partial unique index scoped to outbound must exist.
        partial = conn.exec_driver_sql(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'emails_provider_message_id_outbound_uq'"
        ).fetchone()
        assert partial is not None
        assert "direction" in partial[0] and "outbound" in partial[0]
```

> If `test_migrations.py` does not already expose `alembic_runner`/`alembic_engine` fixtures, match the existing test's harness in that file (read the top of `test_migrations.py` first and reuse its exact pattern for stepping revisions).

- [ ] **Step 2: Run it — expect FAIL**

```bash
cd /Users/r/playground/hail/api && uv run pytest tests/test_migrations.py::test_provider_message_id_unique_is_outbound_only -v
```

Expected: FAIL (revision `0009` does not exist).

- [ ] **Step 3: Write migration 0009**

Create `api/migrations/versions/0009_provider_message_id_partial_unique.py`:

```python
"""Make emails.provider_message_id uniqueness outbound-only.

The global UNIQUE on provider_message_id (from 0005) is correct for
outbound sends — SES returns a unique MessageId per send. But inbound
fan-out writes the *same* SES receipt messageId on one row per recipient
org, so a multi-org delivery collides on the global constraint and 500s,
rolling back every org's row. Inbound idempotency is already enforced by
`emails_inbound_message_id_uq` on (organization_id, message_id), so the
provider_message_id constraint only needs to guard outbound dedup.

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "emails_provider_message_id_key", "emails", type_="unique"
    )
    op.create_index(
        "emails_provider_message_id_outbound_uq",
        "emails",
        ["provider_message_id"],
        unique=True,
        postgresql_where="direction = 'outbound' AND provider_message_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index(
        "emails_provider_message_id_outbound_uq", table_name="emails"
    )
    op.create_unique_constraint(
        "emails_provider_message_id_key", "emails", ["provider_message_id"]
    )
```

- [ ] **Step 4: Update the ORM model to match**

In `core/hailhq/core/models.py`, change the `provider_message_id` column (line ~515) from `unique=True` to a non-unique column, and add the partial index to the table args. Replace:

```python
    provider_message_id: Mapped[str | None] = mapped_column(
        Text, unique=True, nullable=True
    )
```

with:

```python
    provider_message_id: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
```

Then add to the `Email` model's `__table_args__` tuple (find the existing `__table_args__` on the `Email` class and append this `Index`; ensure `Index` and `text` are imported from `sqlalchemy`):

```python
        Index(
            "emails_provider_message_id_outbound_uq",
            "provider_message_id",
            unique=True,
            postgresql_where=text(
                "direction = 'outbound' AND provider_message_id IS NOT NULL"
            ),
        ),
```

- [ ] **Step 5: Run migration test — expect PASS**

```bash
cd /Users/r/playground/hail/api && uv run alembic downgrade base && uv run alembic upgrade head
uv run pytest tests/test_migrations.py -v
```

Expected: PASS, and the full upgrade/downgrade round-trip succeeds.

- [ ] **Step 6: Commit**

```bash
git add api/migrations/versions/0009_provider_message_id_partial_unique.py core/hailhq/core/models.py api/tests/test_migrations.py
git commit -m "fix(email): scope provider_message_id uniqueness to outbound rows"
```

### Task 2: Idempotency `IntegrityError` backstop (C2)

**Why:** `email_ingest._persist_one` does SELECT-then-INSERT with no `try/except IntegrityError`, despite the module docstring claiming the partial unique index "backstops a race." Two concurrent SES re-deliveries both pass the SELECT, the second trips `emails_inbound_message_id_uq`, and the unhandled error poisons the session → the single `commit()` at the end of `ingest_inbound` fails the whole batch → SES retries forever. Wrap the flush in a SAVEPOINT and recover.

**Files:**

- Modify: `core/hailhq/core/email_ingest.py:107-165`
- Test: `core/tests/test_email_ingest.py`

- [ ] **Step 1: Write the failing concurrent-duplicate test**

Add to `core/tests/test_email_ingest.py` (reuse the file's existing session/domain fixtures — read its top first to match fixture names):

```python
async def test_persist_one_survives_unique_violation(db_session, inbound_domain):
    """Simulate a race: a row with the same (org, message_id) already exists,
    inserted via a *separate* path so the in-memory SELECT misses it, then a
    flush trips the partial unique index. _persist_one must recover and return
    the existing id, not raise."""
    from hailhq.core.email_ingest import _persist_one
    from hailhq.core.email_mime import ParsedMime
    from hailhq.core.providers.email.inbound.base import InboundMessage
    from hailhq.core.models import Email

    parsed = ParsedMime(
        from_address="a@b.com", to_addresses=[], cc_addresses=[],
        subject="x", message_id="<dup@hail>", in_reply_to=None,
        references_ids=None, body_text="hi", body_html=None, attachments=[],
    )
    msg = InboundMessage(
        provider_message_id="ses-1", raw_s3_key="k", envelope_recipients=[],
        spam_verdict=None, virus_verdict=None, spf_verdict=None,
        dkim_verdict=None, dmarc_verdict=None, received_at=None,
    )
    # Pre-insert a colliding inbound row directly and commit it.
    pre = Email(
        organization_id=inbound_domain.organization_id,
        email_domain_id=inbound_domain.id, direction="inbound",
        from_address="a@b.com", to_addresses=[], subject="x",
        status="received", provider="ses", message_id="<dup@hail>",
    )
    db_session.add(pre)
    await db_session.commit()

    # Now persist again. The in-process SELECT in _persist_one will find it and
    # short-circuit — but to exercise the index path, delete it from the identity
    # map so the SELECT is forced to the DB? Simpler: assert the call returns the
    # existing id and does not raise.
    class _StubS3:
        async def put_attachment(self, *a, **k): ...
    result_id = await _persist_one(
        db_session, parsed=parsed, message=msg, domain=inbound_domain,
        suppress=None, s3=_StubS3(),
    )
    assert result_id == pre.id
```

> This test primarily locks in the short-circuit-returns-existing-id contract. The index-race path is covered structurally by Step 3's savepoint; a true two-connection race is hard to force in a single-session test, so we also add the route-level multi-org regression in Task 3.

- [ ] **Step 2: Run it — expect PASS or FAIL depending on current SELECT**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_email_ingest.py::test_persist_one_survives_unique_violation -v
```

Expected: this specific assertion likely PASSES today via the SELECT short-circuit. Its purpose is regression protection; the real fix is Step 3 + the route test in Task 3.

- [ ] **Step 3: Add the savepoint backstop**

In `core/hailhq/core/email_ingest.py`, ensure `from sqlalchemy.exc import IntegrityError` is imported at the top. Replace the persist block (currently `db.add(email)` / `await db.flush()` / attachments / `await db.flush()` / `return email.id`, lines ~152-165) with a savepoint-wrapped flush:

```python
    db.add(email)
    try:
        async with db.begin_nested():  # SAVEPOINT — local to this row
            await db.flush()
    except IntegrityError:
        # A concurrent delivery won the race on emails_inbound_message_id_uq.
        # The savepoint rolled back this row only; the outer transaction (and
        # other orgs' rows) is intact. Re-read and short-circuit.
        existing_id = await _existing_inbound_id(
            db, domain.organization_id, parsed.message_id
        )
        return existing_id

    if suppress is None:
        await _persist_attachments(
            db,
            email_id=email.id,
            attachments=parsed.attachments,
            s3=s3,
        )
        await db.flush()
    return email.id
```

> Note: `begin_nested()` issues a real SAVEPOINT so one duplicate doesn't abort the sibling orgs' rows in the same `ingest_inbound` batch — that is exactly what makes the multi-org path (Task 3) safe.

- [ ] **Step 4: Update the module docstring to match reality**

In `email_ingest.py` lines ~5-8, the docstring already claims the index "backstops" the race — it is now true. Adjust the inline comment at line ~116-117 to read:

```python
    # Idempotency: short-circuit if (org, message_id) is already on file.
    # A concurrent insert that slips past this SELECT is caught by the
    # SAVEPOINT-wrapped flush below (emails_inbound_message_id_uq).
```

- [ ] **Step 5: Run the ingest suite — expect PASS**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_email_ingest.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/email_ingest.py core/tests/test_email_ingest.py
git commit -m "fix(email): backstop inbound idempotency race with a savepoint"
```

### Task 3: Multi-org delivery regression test (C1 end-to-end)

**Why:** Lock in that a single SES delivery addressed to two recipient orgs persists **two** rows and returns 200 — the exact scenario that 500'd before Task 1. This is the test whose absence hid C1.

**Files:**

- Create: `api/tests/test_internal_ses_events_multi_org.py`

- [ ] **Step 1: Write the failing multi-org test**

Read `api/tests/test_internal_ses_events.py` first and reuse its signed-request helper (HMAC signing, the `client` fixture, and how it seeds an `EmailDomain`). Then create `api/tests/test_internal_ses_events_multi_org.py`:

```python
"""C1 regression: one SES delivery → two recipient orgs → two rows, no 500."""
import pytest

from tests.test_internal_ses_events import (  # reuse the existing harness
    sign_body,            # helper that returns the X-Hail-Signature value
    make_ses_payload,     # builds the SES notification JSON body
    seed_inbound_domain,  # seeds an EmailDomain for an org with a local prefix
)


@pytest.mark.anyio
async def test_two_orgs_same_message_id_persists_both(client, db_session, settings):
    org_a = await seed_inbound_domain(db_session, prefix="alpha")
    org_b = await seed_inbound_domain(db_session, prefix="beta")

    body = make_ses_payload(
        message_id="<shared@ses>",            # same RFC Message-ID header
        provider_message_id="ses-receipt-1",  # same SES receipt id
        recipients=[
            f"alpha@{settings.hail_mail_base_domain}",
            f"beta@{settings.hail_mail_base_domain}",
        ],
    )
    resp = await client.post(
        "/internal/ses-events",
        content=body,
        headers={"X-Hail-Signature": sign_body(body, settings)},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["email_ids"]) == 2
```

> Adapt `sign_body`/`make_ses_payload`/`seed_inbound_domain` names to whatever the existing `test_internal_ses_events.py` actually defines. If it inlines these, extract small module-level helpers there first (a refactor commit), then import them here.

- [ ] **Step 2: Run it — expect PASS (proves Task 1 fixed C1)**

```bash
cd /Users/r/playground/hail/api && uv run pytest tests/test_internal_ses_events_multi_org.py -v
```

Expected: PASS. If you run this against the pre-Task-1 schema it FAILs with a 500 `duplicate key … emails_provider_message_id_key` — confirming the regression is real.

- [ ] **Step 3: Commit**

```bash
git add api/tests/test_internal_ses_events_multi_org.py api/tests/test_internal_ses_events.py
git commit -m "test(email): multi-org SES delivery persists both orgs' rows"
```

---

## Phase 2 — Webhook delivery envelope (C3)

### Task 4: Emit the §5.2-conformant envelope at send time

**Why:** The bytes on the wire (`webhook_worker.py:165` → `_json.dumps(row.payload)`) carry only `{type, api_version, organization_id, data}`. Spec §5.2 requires top-level `id` (delivery id) and `created_at`. The conformant builder `webhooks.build_event_payload` exists, is tested, and is **called from nowhere** — dead code. Build the wire body in the worker via `build_event_payload`, pulling `id` from `row.id` and `created_at` from `row.created_at` (both confirmed present on `WebhookDelivery`). Store only `organization_id`+`data` in `row.payload`.

**Files:**

- Modify: `core/hailhq/core/webhook_fanout.py:128-136`
- Modify: `core/hailhq/core/webhook_worker.py:163-166`
- Test: `core/tests/test_webhook_worker.py`

- [ ] **Step 1: Write the failing wire-bytes test**

Add to `core/tests/test_webhook_worker.py`. This is the assertion whose absence hid C3 — it inspects the actual POST body:

```python
async def test_delivered_body_has_full_envelope(db_session, make_delivery):
    """The bytes POSTed must carry all six top-level §5.2 keys."""
    import json
    captured = {}

    async def fake_post(url, body, headers):
        captured["body"] = body
        return 200, "ok"

    row = await make_delivery(  # seeds a pending WebhookDelivery + subscription
        event_type="email.received",
        payload={"organization_id": "11111111-1111-1111-1111-111111111111",
                 "data": {"email_id": "e1"}},
    )
    worker = _worker_with(db_session, http_post=fake_post, decrypt=lambda c: "sek")
    await worker.tick()

    env = json.loads(captured["body"])
    assert set(env) == {
        "id", "type", "api_version", "created_at", "organization_id", "data"
    }
    assert env["id"] == str(row.id)
    assert env["type"] == "email.received"
    assert env["data"] == {"email_id": "e1"}
```

> `make_delivery` / `_worker_with` are helpers — reuse whatever `test_webhook_worker.py` already uses to build a worker and seed deliveries (read the file; it already constructs `WebhookWorker` in other tests). The `decrypt` kwarg is introduced in Phase 3; for now, if the worker still takes `plain_secret_resolver`, use that signature and switch this test in Task 7. Sequence note: if you implement Phase 3 before re-running, use `decrypt=`.

- [ ] **Step 2: Run it — expect FAIL**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_webhook_worker.py::test_delivered_body_has_full_envelope -v
```

Expected: FAIL — `set(env)` is missing `id` and `created_at`.

- [ ] **Step 3: Build the envelope at send time**

In `core/hailhq/core/webhook_worker.py`, add to the top-level imports:

```python
from hailhq.core.webhooks import build_event_payload, next_attempt_delay, sign_payload
```

(remove the now-unused `import json as _json` and the `sign_payload`/`next_attempt_delay`-only import line). Replace the body-construction block in `_deliver` (lines ~163-166):

```python
                import json as _json

                body = _json.dumps(row.payload, separators=(",", ":")).encode()
                sig = sign_payload(body, secret)
```

with:

```python
                body = build_event_payload(
                    delivery_id=row.id,
                    event_type=row.event_type,
                    organization_id=row.payload["organization_id"],
                    data=row.payload["data"],
                    created_at=row.created_at,
                )
                sig = sign_payload(body, secret)
```

- [ ] **Step 4: Slim the stored payload in fan-out**

In `core/hailhq/core/webhook_fanout.py`, change `_payload` (lines ~128-136) to store only what the envelope is assembled from — drop the now-redundant `type`/`api_version` (the worker supplies them from `row.event_type` / the builder default):

```python
def _payload(
    event_type: str, organization_id: UUID, data: dict[str, Any]
) -> dict[str, Any]:
    # The worker assembles the §5.2 envelope (id, type, api_version,
    # created_at) at send time via build_event_payload. We persist only
    # the org + data it needs. event_type lives on the delivery row.
    return {
        "organization_id": str(organization_id),
        "data": data,
    }
```

> `_payload` no longer uses `event_type`; keep the parameter for the call-site signature or drop it and update the caller at line ~118 to `_payload(organization_id, data)`. Prefer dropping it (YAGNI) and update the one caller.

- [ ] **Step 5: Run worker + fanout suites — expect PASS**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_webhook_worker.py tests/test_webhook_fanout.py -v
```

Expected: PASS. If `test_webhook_fanout.py` asserted the old 4-key payload shape, update those assertions to the new 2-key shape.

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/webhook_worker.py core/hailhq/core/webhook_fanout.py core/tests/test_webhook_worker.py core/tests/test_webhook_fanout.py
git commit -m "fix(webhooks): emit spec-conformant event envelope with id and created_at"
```

---

## Phase 3 — Encrypted-at-rest webhook secrets (I1)

> **Replaces** the in-process `webhook_secrets.py` cache. After this phase, secrets are stored encrypted in the DB and decrypted by the worker on each delivery — surviving restarts and multi-process deployments, and never auto-disabling a blameless subscription after a restart.

### Task 5: Secret cipher + config + dependency

**Files:**

- Create: `core/hailhq/core/secret_cipher.py`
- Create: `core/tests/test_secret_cipher.py`
- Modify: `core/hailhq/core/config.py:86-88`
- Modify: `core/pyproject.toml` (declare `cryptography`)
- Modify: `.env.example`

- [ ] **Step 1: Declare the `cryptography` dependency**

`cryptography` (46.0.7) is already resolved transitively in the venv, but I1 makes `core` depend on it directly. Add it to `core/pyproject.toml` under `[project] dependencies` (alphabetical), e.g.:

```toml
    "cryptography>=42",
```

License: `cryptography` is Apache-2.0 / BSD — compatible with AGPLv3 (CLAUDE.md license-check satisfied). Then refresh the lock:

```bash
cd /Users/r/playground/hail/core && uv lock
```

- [ ] **Step 2: Write the failing cipher test**

Create `core/tests/test_secret_cipher.py`:

```python
import pytest

from hailhq.core.secret_cipher import (
    SecretCipher, SecretKeyMissing, generate_key,
)


def test_round_trip():
    cipher = SecretCipher(generate_key())
    token = cipher.encrypt("whs_supersecret")
    assert token != "whs_supersecret"          # not plaintext at rest
    assert cipher.decrypt(token) == "whs_supersecret"


def test_distinct_keys_cannot_decrypt():
    a, b = SecretCipher(generate_key()), SecretCipher(generate_key())
    token = a.encrypt("x")
    with pytest.raises(Exception):
        b.decrypt(token)


def test_missing_key_raises():
    with pytest.raises(SecretKeyMissing):
        SecretCipher("")
```

- [ ] **Step 3: Run it — expect FAIL (module missing)**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_secret_cipher.py -v
```

Expected: FAIL — `ModuleNotFoundError: hailhq.core.secret_cipher`.

- [ ] **Step 4: Implement the cipher**

Create `core/hailhq/core/secret_cipher.py`:

```python
"""Symmetric encryption for webhook secrets at rest.

Webhook signing needs the plaintext secret at delivery time, so we can't
store only a hash. Instead we Fernet-encrypt the secret with a deployment
key (``HAIL_WEBHOOK_SECRET_KEY``) and persist the ciphertext. The worker
decrypts on each delivery — so secrets survive restarts and work across
processes, unlike the previous in-process cache.

Generate a key with::

    python -c "from hailhq.core.secret_cipher import generate_key; print(generate_key())"
"""
from __future__ import annotations

from cryptography.fernet import Fernet

__all__ = ["SecretCipher", "SecretKeyMissing", "generate_key"]


class SecretKeyMissing(RuntimeError):
    """HAIL_WEBHOOK_SECRET_KEY is unset but a secret op was attempted."""


def generate_key() -> str:
    return Fernet.generate_key().decode()


class SecretCipher:
    def __init__(self, key: str) -> None:
        if not key:
            raise SecretKeyMissing(
                "HAIL_WEBHOOK_SECRET_KEY must be set to use webhooks"
            )
        self._fernet = Fernet(key.encode())

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
```

- [ ] **Step 5: Add the config field**

In `core/hailhq/core/config.py`, near the other webhook settings (line ~86-88), add:

```python
    # Fernet key for encrypting webhook secrets at rest. Generate with
    # `python -c "from hailhq.core.secret_cipher import generate_key; print(generate_key())"`.
    hail_webhook_secret_key: str = ""
```

- [ ] **Step 6: Document the env var**

In `.env.example`, under the existing SES-inbound / webhook section, add (no value committed — secrets invariant):

```bash
# Fernet key for encrypting webhook secrets at rest (required for webhooks).
# Generate: python -c "from hailhq.core.secret_cipher import generate_key; print(generate_key())"
HAIL_WEBHOOK_SECRET_KEY=
```

- [ ] **Step 7: Run cipher test — expect PASS**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_secret_cipher.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add core/hailhq/core/secret_cipher.py core/tests/test_secret_cipher.py core/hailhq/core/config.py core/pyproject.toml core/uv.lock .env.example
git commit -m "feat(webhooks): add at-rest secret cipher and HAIL_WEBHOOK_SECRET_KEY"
```

### Task 6: Rename secret columns to ciphertext (migrations + model)

**Why:** The columns are named `*_hash` but will now hold Fernet ciphertext. Migrations `0007` (`email_domains.webhook_secret_hash`) and `0008` (`webhook_subscriptions.secret_hash`) are **unreleased**, so edit them in place for a clean history. Because Alembic keys on revision id, a local DB already at head won't replay an in-place edit — so this task includes a mandatory reset.

**Files:**

- Modify: `api/migrations/versions/0007_email_inbound_schema.py`
- Modify: `api/migrations/versions/0008_webhook_subscriptions.py`
- Modify: `core/hailhq/core/models.py` (lines ~429, ~462, ~638)

- [ ] **Step 1: Rename in migration 0008**

In `api/migrations/versions/0008_webhook_subscriptions.py`, change the column definition (line ~47) from:

```python
        sa.Column("secret_hash", sa.Text(), nullable=False),
```

to:

```python
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
```

(No other reference to `secret_hash` exists in 0008; if the downgrade names it, update there too.)

- [ ] **Step 2: Rename in migration 0007**

In `api/migrations/versions/0007_email_inbound_schema.py`, find every `webhook_secret_hash` occurrence (column add, any CHECK constraint text, and the downgrade) and rename to `webhook_secret_encrypted`. The CHECK constraint `(webhook_url IS NULL) = (webhook_secret_hash IS NULL)` becomes `(webhook_url IS NULL) = (webhook_secret_encrypted IS NULL)`.

```bash
cd /Users/r/playground/hail && grep -n "webhook_secret_hash" api/migrations/versions/0007_email_inbound_schema.py
```

Replace each occurrence with `webhook_secret_encrypted`.

- [ ] **Step 3: Rename in the ORM model**

In `core/hailhq/core/models.py`:

- Line ~429: `webhook_secret_hash: Mapped[str | None]` → `webhook_secret_encrypted: Mapped[str | None]`.
- Line ~462 CHECK: `(webhook_url IS NULL) = (webhook_secret_hash IS NULL)` → `(webhook_url IS NULL) = (webhook_secret_encrypted IS NULL)`.
- Line ~638: `secret_hash: Mapped[str]` → `secret_encrypted: Mapped[str]`.

- [ ] **Step 4: Reset the local DB so the edited revisions replay**

```bash
cd /Users/r/playground/hail/api && uv run alembic downgrade base && uv run alembic upgrade head
```

Verify the new column names exist:

```bash
uv run alembic upgrade head
docker compose -f ../docker-compose.yml -f ../docker-compose.local.yml exec postgres \
  psql -U postgres -d hail -c "\d webhook_subscriptions" -c "\d email_domains" | grep -i secret
```

Expected: `secret_encrypted` and `webhook_secret_encrypted` present; no `*_hash`.

- [ ] **Step 5: Run migration test — expect PASS**

```bash
cd /Users/r/playground/hail/api && uv run pytest tests/test_migrations.py -v
```

Expected: PASS (round-trip clean).

- [ ] **Step 6: Commit**

```bash
git add api/migrations/versions/0007_email_inbound_schema.py api/migrations/versions/0008_webhook_subscriptions.py core/hailhq/core/models.py
git commit -m "refactor(webhooks): store webhook secrets as ciphertext columns"
```

### Task 7: Switch routes + worker to encrypt/decrypt; delete the cache

**Files:**

- Modify: `api/hailhq/api/routes/webhooks.py`
- Modify: `api/hailhq/api/routes/email_domains.py`
- Modify: `core/hailhq/core/webhook_worker.py`
- Modify: `api/hailhq/api/main.py`
- Delete: `api/hailhq/api/webhook_secrets.py`
- Test: `api/tests/test_webhooks_api.py`, `core/tests/test_webhook_worker.py`, `api/tests/test_email_domains_inbound_patch.py`

- [ ] **Step 1: Change the worker to decrypt from the row**

In `core/hailhq/core/webhook_worker.py`:

Replace the `plain_secret_resolver` constructor param with a `decrypt` callable:

```python
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        http_post: HttpPostFn,
        decrypt: Callable[[str], str],
        concurrency: int = DEFAULT_CONCURRENCY,
        poll_interval: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._http_post = http_post
        self._decrypt = decrypt
        self._sem = asyncio.Semaphore(concurrency)
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
```

Change `_resolve_target_url` to return both the URL **and** the encrypted secret (it already loads the row — don't load twice):

```python
    async def _resolve_target_url(
        self, db: AsyncSession, row: WebhookDelivery
    ) -> tuple[str, str] | None:
        """Return (target_url, secret_encrypted) or None if undeliverable."""
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
            return sub.target_url, sub.secret_encrypted
        if row.email_domain_id:
            dom = (
                await db.execute(
                    select(EmailDomain).where(EmailDomain.id == row.email_domain_id)
                )
            ).scalar_one_or_none()
            if dom is None or not dom.webhook_url or not dom.webhook_secret_encrypted:
                return None
            return dom.webhook_url, dom.webhook_secret_encrypted
        return None
```

Update `_deliver` to use the tuple and decrypt (replacing the old `target_url` + `secret = self._secret_for(...)` block, lines ~152-166):

```python
                resolved = await self._resolve_target_url(session, row)
                if resolved is None:
                    await self._record_failure(session, row, None, "no target url")
                    return
                target_url, secret_encrypted = resolved

                try:
                    secret = self._decrypt(secret_encrypted)
                except Exception:
                    await self._record_failure(
                        session, row, None, "secret decrypt failed"
                    )
                    return

                body = build_event_payload(
                    delivery_id=row.id,
                    event_type=row.event_type,
                    organization_id=row.payload["organization_id"],
                    data=row.payload["data"],
                    created_at=row.created_at,
                )
                sig = sign_payload(body, secret)
```

> This supersedes the Task 4 body block — they edit the same region; apply Task 4 first, then this. The `owner_id`/`self._secret_for` lines are deleted.

- [ ] **Step 2: Wire the worker in main.py**

In `api/hailhq/api/main.py`, replace the `resolve_secret` import and the `plain_secret_resolver=resolve_secret` wiring (lines ~18, ~82-89) with a cipher-backed `decrypt`:

```python
from hailhq.core.secret_cipher import SecretCipher
```

and in `lifespan`:

```python
    cipher = SecretCipher(settings.hail_webhook_secret_key)
    webhook_worker = WebhookWorker(
        session_factory=...,            # unchanged
        http_post=partial(
            httpx_post,
            allow_private_networks=settings.hail_webhook_allow_private_networks,
        ),
        decrypt=cipher.decrypt,
    )
```

Remove `from hailhq.api.webhook_secrets import resolve_secret`.

- [ ] **Step 3: Encrypt-and-store in the webhooks route**

In `api/hailhq/api/routes/webhooks.py`:

- Remove `from hailhq.api.webhook_secrets import forget_secret, remember_secret`.
- Replace the `_hash(secret)` helper usage: store ciphertext instead. At create (line ~99) and rotate (line ~212), build a cipher from settings and store `secret_encrypted=cipher.encrypt(secret)`. Drop the `remember_secret(...)` calls (lines ~105, ~216). Example for create:

```python
    cipher = SecretCipher(settings.hail_webhook_secret_key)
    sub = WebhookSubscription(
        organization_id=principal.organization_id,
        target_url=body.target_url,
        event_types=body.event_types,
        secret_encrypted=cipher.encrypt(secret),
        status="active",
    )
    db.add(sub)
    await db.flush()
    return _to_response(sub, secret=secret)
```

Get `settings` from the existing dependency the module already uses (match how other handlers in the file access `settings`; if none, add `settings: Annotated[Settings, Depends(get_settings)]`). Delete the now-unused `_hash`/`secret_hash` plumbing.

- [ ] **Step 4: Encrypt-and-store in the email_domains route**

In `api/hailhq/api/routes/email_domains.py`:

- Delete the `_sha256` helper (line ~34) and the `from hailhq.api.webhook_secrets import remember_secret` imports (lines ~398, ~506).
- Where it set `webhook_secret_hash=_sha256(new_secret)` (lines ~443, ~527) store `webhook_secret_encrypted=cipher.encrypt(new_secret)`; where it cleared it (line ~439) set `webhook_secret_encrypted=None`.
- Drop the `remember_secret(...)` calls (lines ~480, ~530).
- Build `cipher = SecretCipher(settings.hail_webhook_secret_key)` once per handler.

- [ ] **Step 5: Delete the cache module**

```bash
git rm api/hailhq/api/webhook_secrets.py
```

Then grep for any lingering importers:

```bash
cd /Users/r/playground/hail && grep -rn "webhook_secrets" api/ core/ | grep -v "secret_encrypted"
```

Expected: no results (all importers removed in Steps 1-4). Fix any that remain.

- [ ] **Step 6: Update tests for the new wiring**

- In `core/tests/test_webhook_worker.py`, every `WebhookWorker(...)` construction: replace `plain_secret_resolver=...` with `decrypt=lambda c: c` (identity) and seed `secret_encrypted` on the subscription/domain rows accordingly (use the identity so the stored value _is_ the plaintext secret). Update the Task 4 test's `decrypt=` kwarg.
- In `api/tests/test_webhooks_api.py` and `test_email_domains_inbound_patch.py`, set `settings.hail_webhook_secret_key` to a generated key in the fixture (use `core/tests` conftest pattern or set via the API `settings` override). Assert the create/rotate response still returns the plaintext secret once, and that the stored column round-trips through `SecretCipher(...).decrypt`.

- [ ] **Step 7: Run the affected suites — expect PASS**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_webhook_worker.py -v
cd /Users/r/playground/hail/api  && uv run pytest tests/test_webhooks_api.py tests/test_email_domains_inbound_patch.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(webhooks): encrypt secrets at rest, delete in-process cache"
```

---

## Phase 4 — Write-time webhook target validation (I2)

### Task 8: Reject non-HTTPS and private-network targets at create/PATCH

**Why:** `target_url`/`webhook_url` are accepted as any non-empty string. Spec §7 requires HTTPS. A bad URL (`http://…`, or a private/SSRF target) is accepted and only fails later as a dead delivery (burning retries). Validate at write time → synchronous 422. The check must consult `hail_webhook_allow_private_networks` (a deployment setting), so it lives in the **route**, not a Pydantic field validator (which can't see settings).

**Files:**

- Modify: `core/hailhq/core/http_post.py` (add a combined validator helper)
- Modify: `api/hailhq/api/routes/webhooks.py` (create + patch)
- Modify: `api/hailhq/api/routes/email_domains.py` (patch)
- Test: `api/tests/test_webhooks_api.py`, `api/tests/test_email_domains_inbound_patch.py`

- [ ] **Step 1: Write the failing validation tests**

Add to `api/tests/test_webhooks_api.py`:

```python
@pytest.mark.anyio
async def test_create_rejects_non_https(client, auth_headers):
    resp = await client.post(
        "/webhooks",
        json={"target_url": "http://hooks.example.com/x", "event_types": ["email.received"]},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "https" in resp.text.lower()


@pytest.mark.anyio
async def test_create_rejects_private_target(client, auth_headers):
    resp = await client.post(
        "/webhooks",
        json={"target_url": "https://169.254.169.254/meta", "event_types": ["email.received"]},
        headers=auth_headers,
    )
    assert resp.status_code == 422
```

- [ ] **Step 2: Run — expect FAIL (currently 200/201)**

```bash
cd /Users/r/playground/hail/api && uv run pytest tests/test_webhooks_api.py -k "rejects" -v
```

Expected: FAIL.

- [ ] **Step 3: Add a reusable validator to http_post.py**

In `core/hailhq/core/http_post.py`, add (reusing the existing `is_private_url`):

```python
def validate_webhook_target(url: str, *, allow_private_networks: bool) -> None:
    """Raise ValueError if ``url`` is not a deliverable webhook target.

    Requires https; rejects private/local targets unless the deployment
    has opted in (self-host escape hatch). Mirrors the delivery-time guard
    in ``httpx_post`` so misconfig fails synchronously at write time.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        if not (allow_private_networks and parsed.scheme == "http"):
            raise ValueError("target_url must use https")
    if not parsed.hostname:
        raise ValueError("target_url must include a host")
    if not allow_private_networks and is_private_url(url):
        raise ValueError("target_url must not point at a private address")
```

Add `validate_webhook_target` to `__all__`.

- [ ] **Step 4: Call it in the webhooks route**

In `api/hailhq/api/routes/webhooks.py`, in `create_subscription` and the PATCH handler, before persisting, validate (raising `HTTPException(422)` on `ValueError`):

```python
    from hailhq.core.http_post import validate_webhook_target
    if body.target_url is not None:  # always set on create; optional on patch
        try:
            validate_webhook_target(
                body.target_url,
                allow_private_networks=settings.hail_webhook_allow_private_networks,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
```

- [ ] **Step 5: Call it in the email_domains PATCH route**

In `api/hailhq/api/routes/email_domains.py`, in the PATCH handler where `body.webhook_url` is set to a non-empty value (line ~441), run the same `validate_webhook_target(...)` guard before assigning `updates["webhook_url"]`.

- [ ] **Step 6: Run — expect PASS**

```bash
cd /Users/r/playground/hail/api && uv run pytest tests/test_webhooks_api.py tests/test_email_domains_inbound_patch.py -v
```

Expected: PASS. (If `hail_webhook_allow_private_networks` defaults True in the test fixture, set it False for these two tests.)

- [ ] **Step 7: Commit**

```bash
git add core/hailhq/core/http_post.py api/hailhq/api/routes/webhooks.py api/hailhq/api/routes/email_domains.py api/tests/test_webhooks_api.py api/tests/test_email_domains_inbound_patch.py
git commit -m "feat(webhooks): validate https + non-private target at write time"
```

---

## Phase 5 — MIME robustness (I3, I4)

### Task 9: Treat `message/rfc822` as a leaf attachment (I3)

**Why:** A `message/rfc822` part reports `is_multipart() == True`, so `msg.walk()` descends into the embedded message and attributes its body/parts to the **parent** — the parent's body can be silently replaced and the attached `.eml` is lost. `walk()` cannot be patched to stop descending; we need a custom recursive traversal that treats `message/rfc822` as a leaf attachment.

**Files:**

- Modify: `core/hailhq/core/email_mime.py:53-80`
- Create: `core/tests/fixtures/inbound/nested_rfc822.eml`
- Test: `core/tests/test_email_mime.py`

- [ ] **Step 1: Create the fixture**

Create `core/tests/fixtures/inbound/nested_rfc822.eml`:

```
From: outer@example.com
To: alpha@mail.hail.so
Subject: Fwd: please see attached
Message-ID: <outer@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUND"

--BOUND
Content-Type: text/plain

Parent body — this must be body_text.
--BOUND
Content-Type: message/rfc822
Content-Disposition: attachment; filename="inner.eml"

From: inner@example.com
To: outer@example.com
Subject: the inner message
Message-ID: <inner@example.com>
Content-Type: text/plain

Inner body — must NOT leak into the parent's body_text.
--BOUND--
```

- [ ] **Step 2: Write the failing test**

Add to `core/tests/test_email_mime.py`:

```python
def test_nested_rfc822_is_attachment_not_parent_body(fixture_bytes):
    parsed = parse_mime(fixture_bytes("nested_rfc822.eml"))
    assert parsed.body_text.strip() == "Parent body — this must be body_text."
    assert "Inner body" not in (parsed.body_text or "")
    rfc822 = [a for a in parsed.attachments if a.content_type == "message/rfc822"]
    assert len(rfc822) == 1
    assert rfc822[0].filename == "inner.eml"
    assert b"Inner body" in rfc822[0].payload
```

> `fixture_bytes` is whatever helper the existing `test_email_mime.py` uses to load `core/tests/fixtures/inbound/*.eml`. Reuse it.

- [ ] **Step 3: Run — expect FAIL**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_email_mime.py::test_nested_rfc822_is_attachment_not_parent_body -v
```

Expected: FAIL — inner body leaks into parent, no `message/rfc822` attachment captured.

- [ ] **Step 4: Replace `_walk_bodies` with a non-descending recursion**

In `core/hailhq/core/email_mime.py`, replace `_walk_bodies` (lines 53-80) with an explicit recursion that stops at `message/rfc822`:

```python
def _collect(
    part: Message,
    text: list[str],
    html: list[str],
    atts: list[ParsedAttachment],
) -> None:
    ctype = part.get_content_type()
    if ctype == "message/rfc822":
        # Leaf: capture the embedded message as an attachment, do NOT
        # descend (walk() would otherwise attribute its body to the parent).
        payload = part.get_payload(decode=True)
        if payload is None:
            inner = part.get_payload()
            raw = inner[0].as_bytes() if isinstance(inner, list) and inner else b""
        else:
            raw = payload
        atts.append(
            ParsedAttachment(
                filename=part.get_filename() or "message.eml",
                content_type="message/rfc822",
                payload=raw,
                content_id=(part.get("Content-ID") or "").strip("<>") or None,
            )
        )
        return
    if part.is_multipart():
        for child in part.get_payload():
            _collect(child, text, html, atts)
        return
    disp = (part.get_content_disposition() or "").lower()
    filename = part.get_filename()
    if disp == "attachment" or filename:
        atts.append(
            ParsedAttachment(
                filename=filename or "attachment",
                content_type=ctype,
                payload=part.get_payload(decode=True) or b"",
                content_id=(part.get("Content-ID") or "").strip("<>") or None,
            )
        )
        return
    if ctype == "text/plain" and not text:
        text.append(_safe_text(part))
    elif ctype == "text/html" and not html:
        html.append(_safe_text(part))


def _walk_bodies(
    msg: Message,
) -> tuple[str | None, str | None, list[ParsedAttachment]]:
    text: list[str] = []
    html: list[str] = []
    atts: list[ParsedAttachment] = []
    _collect(msg, text, html, atts)
    return (text[0] if text else None, html[0] if html else None, atts)
```

> `_safe_text` is added in Task 10 (I4). For this task, temporarily define `_safe_text = lambda p: p.get_content()` at module scope and replace it in Task 10. (Sequence Task 10 immediately after.)

- [ ] **Step 5: Run — expect PASS**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_email_mime.py -v
```

Expected: PASS (including the pre-existing `simple`/`multipart_attachment`/`threaded` tests — verify no regression).

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/email_mime.py core/tests/fixtures/inbound/nested_rfc822.eml core/tests/test_email_mime.py
git commit -m "fix(email): parse message/rfc822 as a leaf attachment"
```

### Task 10: Charset-safe body decoding (I4)

**Why:** Under `policy.default`, `part.get_content()` raises `LookupError` (or similar) on an unrecognized/garbage charset — ordinary open-internet mail will hit this, 500-ing ingest and triggering an SES retry storm. Fall back to `get_payload(decode=True)` decoded with `errors="replace"`.

**Files:**

- Modify: `core/hailhq/core/email_mime.py`
- Create: `core/tests/fixtures/inbound/bad_charset.eml`
- Test: `core/tests/test_email_mime.py`

- [ ] **Step 1: Create the fixture**

Create `core/tests/fixtures/inbound/bad_charset.eml`:

```
From: weird@example.com
To: alpha@mail.hail.so
Subject: bad charset
Message-ID: <badcharset@example.com>
MIME-Version: 1.0
Content-Type: text/plain; charset="x-not-a-real-charset"
Content-Transfer-Encoding: 8bit

hello with a bad charset label
```

- [ ] **Step 2: Write the failing test**

Add to `core/tests/test_email_mime.py`:

```python
def test_unknown_charset_does_not_raise(fixture_bytes):
    parsed = parse_mime(fixture_bytes("bad_charset.eml"))
    assert "hello with a bad charset label" in (parsed.body_text or "")
```

- [ ] **Step 3: Run — expect FAIL (raises LookupError)**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_email_mime.py::test_unknown_charset_does_not_raise -v
```

Expected: FAIL with `LookupError: unknown encoding`.

- [ ] **Step 4: Implement `_safe_text`**

In `core/hailhq/core/email_mime.py`, replace the temporary `_safe_text` lambda from Task 9 with:

```python
def _safe_text(part: Message) -> str:
    """Decode a text part, tolerating bogus/unknown charsets."""
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, ValueError):
        raw = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")
```

- [ ] **Step 5: Run — expect PASS**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_email_mime.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/email_mime.py core/tests/fixtures/inbound/bad_charset.eml core/tests/test_email_mime.py
git commit -m "fix(email): tolerate unknown charsets when decoding bodies"
```

---

## Phase 6 — Forwarding correctness (I5, I6)

### Task 11: Per-domain forward rate cap (I5)

**Why:** `ForwardLimiter.can_forward` filters only on `organization_id`, but its docstring and design §6.2 call it a per-domain cap and it applies the per-domain `forward_rate_per_hour` override. An org with two inbound domains shares one budget; a low per-domain override is enforced org-wide. `outbound_queue.enqueue_outbound_forward` already writes `email_domain_id` on forwarded rows, so the filter is feasible.

**Files:**

- Modify: `core/hailhq/core/forward_limiter.py`
- Modify: `core/hailhq/core/email_ingest.py` (the `can_forward` call site, line ~196)
- Test: `core/tests/test_forward_limiter.py`

- [ ] **Step 1: Write the failing per-domain isolation test**

Add to `core/tests/test_forward_limiter.py` (reuse its existing fixtures for seeding `Email` rows):

```python
async def test_cap_is_scoped_per_domain(db_session, two_domains_same_org):
    """A forward against domain A doesn't consume domain B's budget."""
    dom_a, dom_b = two_domains_same_org  # same org, different EmailDomain ids
    limiter = ForwardLimiter(default_per_hour=1)

    # Seed one forwarded outbound row attributed to domain A.
    await seed_forwarded_row(db_session, org=dom_a.organization_id, domain=dom_a.id)

    # Domain A is now at its cap of 1...
    assert not await limiter.can_forward(
        db_session, organization_id=dom_a.organization_id,
        email_domain_id=dom_a.id, override=1,
    )
    # ...but domain B still has its full budget.
    assert await limiter.can_forward(
        db_session, organization_id=dom_b.organization_id,
        email_domain_id=dom_b.id, override=1,
    )
```

> Add `two_domains_same_org` and `seed_forwarded_row` helpers in the test module if absent (a forwarded row is `Email(direction="outbound", email_domain_id=..., metadata_={"forwarded_from": "..."})`).

- [ ] **Step 2: Run — expect FAIL (signature lacks email_domain_id)**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_forward_limiter.py::test_cap_is_scoped_per_domain -v
```

Expected: FAIL (`can_forward()` got an unexpected keyword argument `email_domain_id`).

- [ ] **Step 3: Add the per-domain filter**

In `core/hailhq/core/forward_limiter.py`, add the `email_domain_id` parameter and `.where`:

```python
    async def can_forward(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        email_domain_id: UUID,
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
            .where(Email.email_domain_id == email_domain_id)
            .where(Email.direction == "outbound")
            .where(Email.created_at >= since)
            .where(Email.metadata_["forwarded_from"].astext.isnot(None))
        )
        used = (await db.execute(stmt)).scalar_one()
        return used < cap
```

- [ ] **Step 4: Thread `email_domain_id` at the call site**

In `core/hailhq/core/email_ingest.py` (line ~197), pass the domain id:

```python
    if not await limiter.can_forward(
        db,
        organization_id=domain.organization_id,
        email_domain_id=domain.id,
        override=domain.forward_rate_per_hour,
    ):
```

- [ ] **Step 5: Run — expect PASS**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_forward_limiter.py tests/test_email_ingest.py -v
```

Expected: PASS (the docstring at the top of `forward_limiter.py` is now accurate — no edit needed, but verify it reads "per-domain").

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/forward_limiter.py core/hailhq/core/email_ingest.py core/tests/test_forward_limiter.py
git commit -m "fix(email): scope forward rate cap to the inbound domain"
```

### Task 12: Loop-detect — drop only the offending target, not its siblings (I6)

**Why:** In the forward loop (`email_ingest.py:208-238`), a `LoopDetected` does `return`, aborting all remaining targets. The hop-cap cause is global (return is right), but the base-domain cause is **per-target** — one self-referential target silently suppresses every other valid target. Distinguish the two and `continue` for the per-target case.

**Files:**

- Modify: `core/hailhq/core/email_routing.py` (where `LoopDetected`/`detect_loop` live — add a cause discriminator)
- Modify: `core/hailhq/core/email_ingest.py:208-238`
- Test: `core/tests/test_email_routing.py`, `core/tests/test_email_ingest.py`

- [ ] **Step 1: Inspect the current `detect_loop`/`LoopDetected`**

```bash
cd /Users/r/playground/hail && grep -n "class LoopDetected\|def detect_loop\|raise LoopDetected\|max_hops\|base_domain" core/hailhq/core/email_routing.py
```

Note the two raise sites (hop-cap vs. base-domain match).

- [ ] **Step 2: Write the failing test**

Add to `core/tests/test_email_routing.py`:

```python
def test_loop_detected_carries_cause():
    from hailhq.core.email_routing import detect_loop, LoopDetected
    # base-domain self-reference → per-target cause
    try:
        detect_loop(target="x@mail.hail.so", hops=0,
                    base_domain="mail.hail.so", max_hops=3)
        assert False, "expected LoopDetected"
    except LoopDetected as exc:
        assert exc.cause == "base_domain"
    # hop cap exceeded → global cause
    try:
        detect_loop(target="ok@example.com", hops=5,
                    base_domain="mail.hail.so", max_hops=3)
        assert False, "expected LoopDetected"
    except LoopDetected as exc:
        assert exc.cause == "hop_cap"
```

- [ ] **Step 3: Run — expect FAIL (no `.cause`)**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_email_routing.py::test_loop_detected_carries_cause -v
```

Expected: FAIL — `LoopDetected` has no attribute `cause`.

- [ ] **Step 4: Add a `cause` discriminator**

In `core/hailhq/core/email_routing.py`, give `LoopDetected` a `cause`:

```python
class LoopDetected(Exception):
    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause  # "hop_cap" | "base_domain"
```

and at the two raise sites in `detect_loop`, raise `LoopDetected("hop_cap")` (when `hops >= max_hops`) and `LoopDetected("base_domain")` (when the target is on the base domain). Match the existing condition order in the function.

- [ ] **Step 5: Use the cause in ingest**

In `core/hailhq/core/email_ingest.py`, replace the `except LoopDetected: ... return` block (lines ~213-217) with:

```python
        try:
            detect_loop(
                target=target,
                hops=hops,
                base_domain=hail_mail_base_domain,
                max_hops=forward_max_hops,
            )
        except LoopDetected as exc:
            if "forward_loop" not in result.suppressed_reasons:
                result.suppressed_reasons.append("forward_loop")
            if exc.cause == "hop_cap":
                return        # global: no target can be forwarded
            continue          # per-target: skip this one, keep the rest
```

- [ ] **Step 6: Write the ingest sibling-survival test**

Add to `core/tests/test_email_ingest.py` (or the forwarding test module) a case where `forward_to` has `["self@mail.hail.so", "valid@external.com"]` and assert the external target still gets enqueued (one `forward_enqueue` call for `valid@external.com`). Use the existing `ForwardEnqueue` stub pattern in that file to count calls.

- [ ] **Step 7: Run — expect PASS**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_email_routing.py tests/test_email_ingest.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add core/hailhq/core/email_routing.py core/hailhq/core/email_ingest.py core/tests/test_email_routing.py core/tests/test_email_ingest.py
git commit -m "fix(email): per-target loop suppression keeps valid sibling targets"
```

---

## Phase 7 — URL-invariant hygiene (I10)

### Task 13: Replace `.rstrip("/")` + f-string joins with `join_url`

**Why:** CLAUDE.md's "URLs are not strings" invariant forbids `.rstrip("/")` + f-string URL minting. Violations: `email_ingest.py:321` and `:354` (raw/attachment URLs), and `ses_events.py:80` (`str(request.base_url).rstrip("/")`). Use `hailhq.core.urls.join_url`.

**Files:**

- Modify: `core/hailhq/core/email_ingest.py:320-324, 354-363`
- Modify: `api/hailhq/api/routes/internal/ses_events.py:80`
- Test: `core/tests/test_email_ingest.py` (assert URL shape)

- [ ] **Step 1: Write/extend a test asserting joined URL shape**

In `core/tests/test_email_ingest.py`, in the fan-out test that already exercises `api_base_url`, pass a base **with** a trailing slash (`"https://api.hail.so/"`) and assert the produced `raw_url` is exactly `"https://api.hail.so/emails/<id>/raw"` (no double slash, no missing slash). If no such test exists, add one around the `_attachment_payload`/data assembly.

- [ ] **Step 2: Run — expect FAIL (double slash or current shape)**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_email_ingest.py -k "url" -v
```

- [ ] **Step 3: Use `join_url` in email_ingest.py**

Add `from hailhq.core.urls import join_url` at the top. Replace the raw-url f-string (lines ~320-324):

```python
                raw_url=(
                    join_url(api_base_url, f"emails/{email_id}/raw")
                    if api_base_url
                    else None
                ),
```

and in `_attachment_payload` (lines ~354-363) replace the `base = api_base_url.rstrip("/")` + f-string with:

```python
            "url": (
                join_url(api_base_url, f"emails/{email_id}/attachments/{att.id}")
                if api_base_url
                else None
            ),
```

(drop the now-unused `base = ...` line).

- [ ] **Step 4: Use `join_url` at the ses_events call site**

In `api/hailhq/api/routes/internal/ses_events.py:80`, the call passes `api_base_url=str(request.base_url).rstrip("/")`. Change to pass the canonical base and let `join_url` handle separators:

```python
        api_base_url=canonical_url(str(request.base_url)),
```

Add `from hailhq.core.urls import canonical_url`. (`join_url` downstream tolerates the trailing slash `canonical_url` may add — that's the point of the helper.)

- [ ] **Step 5: Run — expect PASS**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_email_ingest.py -v
cd /Users/r/playground/hail/api  && uv run pytest tests/test_internal_ses_events.py tests/test_internal_ses_events_multi_org.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/email_ingest.py api/hailhq/api/routes/internal/ses_events.py core/tests/test_email_ingest.py
git commit -m "fix(email): build inbound URLs via join_url per URL invariant"
```

---

## Phase 8 — Doc-vs-behavior alignment (I11, I12)

### Task 14: Populate `raw_url`/`attachments` on `GET /emails/{id}` (I11)

**Why:** `EmailResponse` documents `raw_url` and `attachments` as surfaced "on the full-row endpoint (GET /emails/{id})," but `get_email` returns `EmailResponse.model_validate(email)` leaving both at defaults. Populate them for inbound rows.

**Files:**

- Modify: `api/hailhq/api/routes/emails.py:417-435`
- Test: `api/tests/test_emails_inbound_reads.py`

- [ ] **Step 1: Write the failing test**

Add to `api/tests/test_emails_inbound_reads.py` (reuse its inbound-email seeding helper that already creates an `Email` + `EmailAttachment`):

```python
@pytest.mark.anyio
async def test_get_email_populates_raw_url_and_attachments(client, auth_headers, seeded_inbound_email):
    eid, att_id = seeded_inbound_email  # has raw_s3_key + one attachment
    resp = await client.get(f"/emails/{eid}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["raw_url"].endswith(f"/emails/{eid}/raw")
    assert len(body["attachments"]) == 1
    assert body["attachments"][0]["url"].endswith(f"/emails/{eid}/attachments/{att_id}")
```

- [ ] **Step 2: Run — expect FAIL (raw_url None, attachments [])**

```bash
cd /Users/r/playground/hail/api && uv run pytest tests/test_emails_inbound_reads.py -k "populates" -v
```

- [ ] **Step 3: Populate in `get_email`**

In `api/hailhq/api/routes/emails.py`, replace the final `return EmailResponse.model_validate(email)` in `get_email` with logic that, for rows with a `raw_s3_key` or attachments, builds the URLs via `join_url` and loads attachment rows. Add `from hailhq.core.urls import join_url` and `from hailhq.core.models import EmailAttachment` (if not already imported):

```python
    resp = EmailResponse.model_validate(email)
    base = str(request.base_url)
    if email.raw_s3_key:
        resp.raw_url = join_url(base, f"emails/{email.id}/raw")
    att_rows = (
        (await db.execute(
            select(EmailAttachment).where(EmailAttachment.email_id == email.id)
        )).scalars().all()
    )
    resp.attachments = [
        EmailAttachmentResponse(
            id=a.id, filename=a.filename, content_type=a.content_type,
            size_bytes=a.size_bytes, content_id=a.content_id,
            url=join_url(base, f"emails/{email.id}/attachments/{a.id}"),
        )
        for a in att_rows
    ]
    return resp
```

Add `request: Request` to the handler signature (import `from fastapi import Request`) and `EmailAttachmentResponse` to the schema imports.

- [ ] **Step 4: Run — expect PASS**

```bash
cd /Users/r/playground/hail/api && uv run pytest tests/test_emails_inbound_reads.py tests/test_emails_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/emails.py api/tests/test_emails_inbound_reads.py
git commit -m "fix(email): populate raw_url and attachments on GET /emails/{id}"
```

### Task 15: Enforce `hail_inbound_org_rate_per_hour` soft cap (I12)

**Why:** The setting is configured and its comment promises "Beyond it we persist but skip fan-out," but nothing reads it. Implement that exact behavior: when an org exceeds the inbound cap in the last hour, still persist the row but skip webhook fan-out (and record a suppressed reason).

**Files:**

- Modify: `core/hailhq/core/email_ingest.py` (`ingest_inbound`, around the fan-out block ~305-335; accept the cap as a parameter)
- Modify: `api/hailhq/api/routes/internal/ses_events.py` (pass the cap)
- Test: `core/tests/test_email_ingest.py`

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_email_ingest.py`: seed `org_rate_per_hour=1`, ingest two distinct inbound messages for the same org, and assert the **second** persists a row but triggers **no** fan-out call (count the `fanout` stub invocations) and adds `"org_rate_limit"` to `suppressed_reasons`.

```python
async def test_org_inbound_cap_skips_fanout_but_persists(db_session, inbound_domain):
    calls = []
    async def fanout(*a, **k): calls.append(k)
    # first message — under cap
    await ingest_inbound(db_session, message=_msg("<m1@x>"), s3=_StubS3(),
                         hail_mail_base_domain="mail.hail.so",
                         forward_enqueue=_noop, forward_max_hops=3,
                         forward_default_per_hour=200, fanout=fanout,
                         api_base_url=None, org_rate_per_hour=1)
    # second message — over cap
    res = await ingest_inbound(db_session, message=_msg("<m2@x>"), s3=_StubS3(),
                         hail_mail_base_domain="mail.hail.so",
                         forward_enqueue=_noop, forward_max_hops=3,
                         forward_default_per_hour=200, fanout=fanout,
                         api_base_url=None, org_rate_per_hour=1)
    assert len(calls) == 1                       # fan-out only for the first
    assert "org_rate_limit" in res.suppressed_reasons
    assert len(res.email_ids) == 1               # second still persisted
```

> Use the helper constructors already in the file; `_msg`/`_StubS3`/`_noop` mirror existing fixtures. If `ingest_inbound` has no `org_rate_per_hour` param yet, this fails at the call — that's expected.

- [ ] **Step 2: Run — expect FAIL**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_email_ingest.py -k "org_inbound_cap" -v
```

- [ ] **Step 3: Add the cap parameter + check**

In `core/hailhq/core/email_ingest.py`, add `org_rate_per_hour: int` to `ingest_inbound`'s keyword params. Before the fan-out block, compute the org's inbound count in the last hour (one query, reuse the pattern from `forward_limiter`) and gate fan-out:

```python
        over_cap = await _org_over_inbound_cap(
            db, organization_id=domain.organization_id, cap=org_rate_per_hour
        )
        if over_cap and "org_rate_limit" not in result.suppressed_reasons:
            result.suppressed_reasons.append("org_rate_limit")
        if suppress is None and fanout is not None and not over_cap:
            ...  # existing fan-out call unchanged
```

Add the helper:

```python
async def _org_over_inbound_cap(
    db: AsyncSession, *, organization_id: UUID, cap: int
) -> bool:
    if cap <= 0:
        return True
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    stmt = (
        select(func.count())
        .select_from(Email)
        .where(Email.organization_id == organization_id)
        .where(Email.direction == "inbound")
        .where(Email.created_at >= since)
    )
    used = (await db.execute(stmt)).scalar_one()
    return used >= cap
```

(import `func`, `timedelta`, `timezone`, `datetime` if not already imported.)

- [ ] **Step 4: Pass the cap from the route**

In `api/hailhq/api/routes/internal/ses_events.py`, add to the `ingest_inbound(...)` call:

```python
        org_rate_per_hour=settings.hail_inbound_org_rate_per_hour,
```

- [ ] **Step 5: Run — expect PASS**

```bash
cd /Users/r/playground/hail/core && uv run pytest tests/test_email_ingest.py -v
cd /Users/r/playground/hail/api  && uv run pytest tests/test_internal_ses_events.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/email_ingest.py api/hailhq/api/routes/internal/ses_events.py core/tests/test_email_ingest.py
git commit -m "feat(email): enforce per-org inbound soft cap (persist, skip fan-out)"
```

---

## Phase 9 — Client surface (C4, I7)

### Task 16: Register the `hail webhooks` command (C4)

**Why:** `newWebhooksCmd` is defined but `root.go` never calls `AddCommand`, so `hail webhooks` is unreachable — half the PR's named deliverable. Register it and add a smoke test that every top-level command is wired.

**Files:**

- Modify: `cli/internal/cmd/root.go:132-136`
- Create: `cli/internal/cmd/webhooks_test.go`

- [ ] **Step 1: Write the failing registration test**

Create `cli/internal/cmd/webhooks_test.go`:

```go
package cmd

import "testing"

func TestWebhooksCommandRegistered(t *testing.T) {
	root := newRootCmd(nil, nil, nil, func(string) string { return "" })
	for _, c := range root.Commands() {
		if c.Name() == "webhooks" {
			return
		}
	}
	t.Fatal("`hail webhooks` is not registered on the root command")
}
```

> Match `newRootCmd`'s actual signature (read `root.go:81`+ — it takes stdout/stderr/getenv/opts). Adjust the constructor call to compile.

- [ ] **Step 2: Run — expect FAIL**

```bash
cd /Users/r/playground/hail/cli && go test ./internal/cmd/ -run TestWebhooksCommandRegistered -v
```

Expected: FAIL — "webhooks is not registered."

- [ ] **Step 3: Register the command**

In `cli/internal/cmd/root.go`, after line 136 (`root.AddCommand(newAuthCmd(opts))`), add:

```go
	root.AddCommand(newWebhooksCmd(opts))
```

- [ ] **Step 4: Run — expect PASS, and verify in the help**

```bash
cd /Users/r/playground/hail/cli && go test ./internal/cmd/ -run TestWebhooksCommandRegistered -v
go run . --help | grep webhooks
```

Expected: PASS and `webhooks` appears in `--help`.

- [ ] **Step 5: Commit**

```bash
git add cli/internal/cmd/root.go cli/internal/cmd/webhooks_test.go
git commit -m "fix(cli): register the webhooks command on root"
```

### Task 17: SDK inbound fields incl. `webhook_secret` (I7)

**Why:** `sdk/hail/models.py` `EmailDomainResponse` omits all inbound fields, so `extra="ignore"` silently drops the once-only `webhook_secret` from a PATCH response — a user can never retrieve it via the SDK. `EmailResponse` also lacks inbound fields and there's no `EmailAttachmentResponse`. Mirror the `core` schema additions into the SDK.

**Files:**

- Modify: `sdk/hail/models.py`
- Test: `sdk/tests/test_email_domains.py` (and a new inbound read assertion)

- [ ] **Step 1: Write the failing tests**

Add to `sdk/tests/test_email_domains.py` (reuse its mock-transport pattern):

```python
def test_patch_returns_webhook_secret(email_domains_client, mock_patch_response):
    mock_patch_response({"id": "...", "kind": "inbound", "domain": "mail.x",
                         "webhook_url": "https://h/x", "webhook_secret": "whd_abc",
                         "inbound_enabled": True, "forward_to": None,
                         "forward_rate_per_hour": None})
    dom = email_domains_client.patch("…", webhook_url="https://h/x")
    assert dom.webhook_secret == "whd_abc"
    assert dom.inbound_enabled is True
```

- [ ] **Step 2: Run — expect FAIL (attribute missing / dropped)**

```bash
cd /Users/r/playground/hail/sdk && uv run pytest tests/test_email_domains.py -k "webhook_secret" -v
```

- [ ] **Step 3: Add inbound fields to the SDK models**

In `sdk/hail/models.py`, extend `EmailDomainResponse` with the inbound fields (defaults so outbound/existing serializations keep working):

```python
    inbound_enabled: bool = False
    forward_to: list[str] | None = None
    webhook_url: str | None = None
    forward_rate_per_hour: int | None = None
    # populated only by create + PATCH-that-sets-webhook + rotate responses
    webhook_secret: str | None = None
```

Add an `EmailAttachmentResponse` model and extend `EmailResponse` to mirror `core/hailhq/core/schemas.py:EmailResponse` (direction, message_id, in_reply_to, references_ids, the four verdicts, provider_received_at, raw_url, attachments):

```python
class EmailAttachmentResponse(BaseModel):
    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    content_id: str | None = None
    url: str


class EmailResponse(EmailSummary):
    body_text: str | None = None
    body_html: str | None = None
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
    raw_url: str | None = None
    attachments: list[EmailAttachmentResponse] = []
```

Export `EmailAttachmentResponse` from `sdk/hail/__init__.py` if the SDK re-exports models there.

- [ ] **Step 4: Run — expect PASS**

```bash
cd /Users/r/playground/hail/sdk && uv run pytest tests/ -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sdk/hail/models.py sdk/hail/__init__.py sdk/tests/test_email_domains.py
git commit -m "fix(sdk): expose inbound email-domain and email fields incl. webhook_secret"
```

---

## Phase 10 — Docs reconciliation

### Task 18: Trim docs to the shipped surface

**Why:** Because I8 is excluded (no `hail email list`, no inbound-config CLI), docs that show those commands would violate the docs invariant ("every page should let a reader take the next action"). Keep the now-working `hail webhooks` examples; remove or replace the unshipped ones. Also update the §10.6 secret-cache footgun note — it no longer applies after I1.

**Files:**

- Modify: `docs/setup/aws-ses.md`
- Modify: `docs/architecture.md`

- [ ] **Step 1: Audit the doc references**

```bash
cd /Users/r/playground/hail && grep -rn "hail email list\|hail webhooks\|secret cache\|§10.6\|rotates the affected\|first delivery fails" docs/setup/aws-ses.md docs/architecture.md
```

- [ ] **Step 2: Replace `hail email list --direction inbound` examples**

In `docs/setup/aws-ses.md` §10.4 (and any in `architecture.md`), replace the `hail email list --direction inbound` invocation with the API/SDK equivalent that _does_ ship — e.g. a `curl "$HAIL_API_URL/emails?direction=inbound"` example, or the SDK `client.emails.list(direction="inbound")` if the SDK exposes it. Verify the chosen example actually runs against the current code before committing.

- [ ] **Step 3: Keep + verify the `hail webhooks` example**

Confirm §10.5's `hail webhooks create …` matches the registered command's actual flags (`go run . webhooks create --help`). Fix any flag drift.

- [ ] **Step 4: Rewrite the secret-cache footgun note (§10.6)**

Replace the "in-process cache; first delivery fails until the tenant rotates after a restart" paragraph with the new behavior: secrets are encrypted at rest with `HAIL_WEBHOOK_SECRET_KEY`; deliveries survive restarts and multi-process; the key must be set or webhook creation fails. Link `core/hailhq/core/secret_cipher.py`.

- [ ] **Step 5: Sanity-check the docs build/links**

```bash
cd /Users/r/playground/hail && grep -rn "webhook_secret_hash\|secret_hash\|in-process cache" docs/
```

Expected: no stale references.

- [ ] **Step 6: Commit**

```bash
git add docs/setup/aws-ses.md docs/architecture.md
git commit -m "docs(email): reconcile inbound/webhooks docs with shipped surface"
```

---

## Phase 11 — OpenAPI regen + full verification

### Task 19: Regenerate OpenAPI and run every suite

**Why:** CLAUDE.md invariant — OpenAPI is the source of truth for the CLI and must be regenerated after any route/schema change. This phase added a `direction` query usage path, validation responses, and changed no public route shapes materially, but the spec must reflect the current app and stay drift-free. Then run all suites + linters.

**Files:**

- Modify: `openapi/openapi.yaml`

- [ ] **Step 1: Regenerate the spec**

Use the repo's existing generation path (check `docs/operations.md` or a Makefile target for the canonical command; it dumps `app.openapi()`). Typically:

```bash
cd /Users/r/playground/hail/api && uv run python -c "import json, yaml; from hailhq.api.main import app; print(yaml.safe_dump(app.openapi(), sort_keys=False))" > ../openapi/openapi.yaml
```

> Use the project's actual generator if one exists — match it rather than hand-rolling, so formatting stays stable.

- [ ] **Step 2: Diff the spec — confirm only intended changes**

```bash
cd /Users/r/playground/hail && git diff openapi/openapi.yaml | head -120
```

Expected: changes limited to validation/response additions and any field updates from this plan; no unexpected route loss.

- [ ] **Step 3: Run the full test matrix + linters**

```bash
cd /Users/r/playground/hail/core && uv run pytest -q && uv run ruff check . && uv run black --check . && uv run mypy .
cd /Users/r/playground/hail/api  && uv run pytest -q && uv run ruff check . && uv run black --check . && uv run mypy .
cd /Users/r/playground/hail/sdk  && uv run pytest -q
cd /Users/r/playground/hail/cli  && go test ./... && gofmt -l .
cd /Users/r/playground/hail/mcp  && uv run pytest -q
```

Expected: all green; `gofmt -l .` prints nothing; `ruff`/`black`/`mypy` clean.

- [ ] **Step 4: Commit**

```bash
git add openapi/openapi.yaml
git commit -m "chore(openapi): regenerate spec for inbound-email review fixes"
```

---

## Self-review checklist (run before handing off / opening a PR)

- [ ] **C1** — migration 0009 applied; multi-org SES delivery test green; global `emails_provider_message_id_key` gone.
- [ ] **C2** — `_persist_one` flush is SAVEPOINT-wrapped with `IntegrityError` recovery; docstring matches.
- [ ] **C3** — wire-bytes test asserts all six §5.2 keys; `build_event_payload` is now _called_ (no longer dead).
- [ ] **C4** — `hail webhooks` in `--help`; registration test green.
- [ ] **I1** — `webhook_secrets.py` deleted; no importers remain; secrets round-trip via `SecretCipher`; `HAIL_WEBHOOK_SECRET_KEY` in `.env.example` + config; `cryptography` declared in `core/pyproject.toml` + lock.
- [ ] **I2** — `http://` and private targets 422 at create/PATCH on both webhooks and email-domains routes.
- [ ] **I3** — `message/rfc822` captured as attachment; parent body intact; fixture present.
- [ ] **I4** — bad-charset fixture parses without raising.
- [ ] **I5** — limiter filters on `email_domain_id`; sibling-domain isolation test green.
- [ ] **I6** — base-domain loop `continue`s; hop-cap `return`s; sibling-survival test green.
- [ ] **I7** — SDK `EmailDomainResponse.webhook_secret` + inbound `EmailResponse`/`EmailAttachmentResponse` present and tested.
- [ ] **I10** — no `.rstrip("/")`/f-string URL joins in `email_ingest.py` or `ses_events.py`.
- [ ] **I11** — `GET /emails/{id}` returns `raw_url` + `attachments` for inbound rows.
- [ ] **I12** — over-cap org persists row but skips fan-out + records `org_rate_limit`.
- [ ] **Docs** — no references to `hail email list --direction inbound` or the old secret-cache footgun.
- [ ] **OpenAPI** — regenerated; diff reviewed; CLI client consistent.
- [ ] **Excluded items unchanged** — I8 CLI subcommands, all Minors, and the two costs docs are _not_ touched by these commits.
- [ ] All suites + `ruff`/`black`/`mypy`/`gofmt` green.

---

## Notes for the implementer

- **Test fixture names** are referenced generically (`db_session`, `auth_headers`, `inbound_domain`, `fixture_bytes`, `make_delivery`). Read the top of each target test file first and use the **actual** fixture names — do not invent new ones if equivalents exist.
- **Sequencing within Phase 2→3:** Task 4 (C3) and Task 7 (I1) both edit `webhook_worker._deliver`. Apply Task 4 first, then Task 7 layers the decrypt change onto the same region. The Task 4 test uses the final `decrypt=` worker signature.
- **Migrations:** 0009 is a _new_ revision (released schema). The secret-column renames edit _unreleased_ 0007/0008 in place — always `alembic downgrade base && alembic upgrade head` after editing so your local DB replays them.
- **Conventional Commits** throughout (already used above). Each task ends in a single focused commit.
