# Custom Sender Domains — Plan 2: Backend Inbound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Receive inbound mail on verified custom domains and route each message to the right org/domain — one webhook per receiving domain — without per-domain SES provisioning.

**Architecture:** All changes are in `hail/` (`core/` + `infra/`). The inbound ingest gains a `kind='custom'` recipient-matching branch and dedups custom rows by receiving identity (not org); the `email.received` webhook payload carries the matched domain name; the SES receipt rule becomes catch-all so any verified identity is received and routed in-app.

**Tech Stack:** Python 3, SQLAlchemy 2 async, pytest (`async_session` harness), Terraform (SES receipt rule).

This is **Plan 2 of 3** for `docs/superpowers/specs/2026-06-25-custom-sender-domains-design.md`. Plan 1 (backend outbound) is done. Plan 3 = website `/console/emails`.

## Global Constraints

- **Conventional Commits** for every commit message.
- **Lint/format/type:** `ruff` (`--fix`) + `black`; `mypy` + `pytest` pass in CI.
- **Shared logic lives in `core/`.** Ingest, routing, and webhook fan-out are all `core` modules.
- **Tests need a DB:** run with `DATABASE_URL="postgresql+psycopg://hail:hail@localhost:5432/hail"` set, from the package dir (`cd core && DATABASE_URL=... uv run pytest …`). Local Postgres must be up (`docker compose -f docker-compose.yml -f docker-compose.local.yml up postgres`).
- **Inbound matching gates differ by kind (intentional):** `hail_mail` rows are ingested regardless of `inbound_enabled` (platform addresses); `custom` rows are matched **only** when `inbound_enabled = true AND verification_status = 'verified'` (receiving is opt-in per customer).
- **Dedup scope:** per-identity dedup applies to `kind='custom'` only; `hail_mail` keeps its existing per-org dedup.

---

### Task 1: Match inbound recipients on verified, inbound-enabled custom domains

**Files:**

- Modify: `core/hailhq/core/email_ingest.py` (`_find_domain_for_recipient`, ~line 65)
- Test: `core/tests/test_email_ingest.py`

**Interfaces:**

- Consumes: `EmailDomain` (`kind`, `domain`, `inbound_enabled`, `verification_status`, `organization_id`).
- Produces: `_find_domain_for_recipient(db, recipient, base_domain)` now also returns a `kind='custom'` row whose `domain` equals the recipient's domain part, when that row is `inbound_enabled` and `verified`. Hail-mail behavior unchanged.

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_email_ingest.py` (after the existing `_make_inbound_domain` helper):

```python
def _make_custom_inbound_domain(org_id, domain="mail.acme.com", *, enabled=True,
                                status="verified"):
    """A kind='custom' row set up (or not) to receive inbound mail."""
    return EmailDomain(
        organization_id=org_id,
        kind="custom",
        domain=domain,
        verification_status=status,
        provider="ses",
        inbound_enabled=enabled,
        forward_to=["ops@example.com"] if enabled else None,
    )


@pytest.mark.asyncio
async def test_custom_domain_receives_when_verified_and_inbound_enabled(async_session):
    org_id = uuid.uuid4()
    async_session.add(_make_custom_inbound_domain(org_id, "mail.acme.com"))
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="cust1", envelope_from="x@example.com",
        envelope_recipients=["support@mail.acme.com"],  # any local-part
        raw_s3_bucket="b", raw_s3_key="raw/cust1",
        spam_verdict="PASS", virus_verdict="PASS", spf_verdict="PASS",
        dkim_verdict="PASS", dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "multipart_attachment.eml").read_bytes()

    result = await ingest_inbound(
        async_session, message=msg, s3=s3,
        hail_mail_base_domain="mail.hail.so", org_rate_per_hour=10_000,
    )
    assert len(result.email_ids) == 1
    assert result.skipped_recipients == []


@pytest.mark.asyncio
async def test_custom_domain_skipped_when_pending_or_inbound_disabled(async_session):
    org_id = uuid.uuid4()
    async_session.add(_make_custom_inbound_domain(org_id, "pending.acme.com",
                                                  enabled=True, status="pending"))
    async_session.add(_make_custom_inbound_domain(org_id, "off.acme.com",
                                                  enabled=False, status="verified"))
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="cust2", envelope_from="x@example.com",
        envelope_recipients=["a@pending.acme.com", "b@off.acme.com"],
        raw_s3_bucket="b", raw_s3_key="raw/cust2",
        spam_verdict="PASS", virus_verdict="PASS", spf_verdict="PASS",
        dkim_verdict="PASS", dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "multipart_attachment.eml").read_bytes()

    result = await ingest_inbound(
        async_session, message=msg, s3=s3,
        hail_mail_base_domain="mail.hail.so", org_rate_per_hour=10_000,
    )
    assert result.email_ids == []
    assert set(result.skipped_recipients) == {"a@pending.acme.com", "b@off.acme.com"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && DATABASE_URL="postgresql+psycopg://hail:hail@localhost:5432/hail" uv run pytest tests/test_email_ingest.py::test_custom_domain_receives_when_verified_and_inbound_enabled -v`
Expected: FAIL — recipient is skipped (custom domains aren't matched yet).

- [ ] **Step 3: Add the custom branch**

In `core/hailhq/core/email_ingest.py`, extend `_find_domain_for_recipient`. After the hail-mail lookup, add a custom fallback:

```python
async def _find_domain_for_recipient(
    db: AsyncSession, recipient: str, base_domain: str
) -> EmailDomain | None:
    classified = classify_hail_mail_recipient(recipient, base_domain)
    if classified is not None:
        stmt = (
            select(EmailDomain)
            .where(EmailDomain.kind == "hail_mail")
            .where(EmailDomain.local_prefix_user == classified.user_prefix)
            .where(EmailDomain.local_prefix_org == classified.org_prefix)
            .limit(1)
        )
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is not None:
            return row

    # Custom domains: match any local-part at a verified, inbound-enabled
    # custom domain. Receiving is opt-in per customer, so (unlike hail-mail)
    # both the verified and inbound_enabled gates are required.
    _, _, dom = recipient.partition("@")
    if not dom:
        return None
    stmt = (
        select(EmailDomain)
        .where(EmailDomain.kind == "custom")
        .where(EmailDomain.domain == dom.lower())
        .where(EmailDomain.inbound_enabled.is_(True))
        .where(EmailDomain.verification_status == "verified")
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && DATABASE_URL="postgresql+psycopg://hail:hail@localhost:5432/hail" uv run pytest tests/test_email_ingest.py -v`
Expected: PASS — both new tests, and all existing hail-mail ingest tests (regression).

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/email_ingest.py core/tests/test_email_ingest.py
git commit -m "feat(email): route inbound to verified inbound-enabled custom domains"
```

---

### Task 2: Dedup inbound rows per custom identity (one webhook per receiving domain)

**Files:**

- Modify: `core/hailhq/core/email_ingest.py` (`ingest_inbound` loop, ~line 365)
- Test: `core/tests/test_email_ingest.py`

**Interfaces:**

- Produces: a single inbound message addressed to two verified custom domains in the **same org** yields **two** `Email` rows (one per domain). Hail-mail dedup (one row per org) is unchanged.

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_email_ingest.py`:

```python
@pytest.mark.asyncio
async def test_two_custom_domains_same_org_yield_two_rows(async_session):
    org_id = uuid.uuid4()
    async_session.add(_make_custom_inbound_domain(org_id, "mail.acme.com"))
    async_session.add(_make_custom_inbound_domain(org_id, "mail.beta.com"))
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="multi1", envelope_from="x@example.com",
        envelope_recipients=["a@mail.acme.com", "b@mail.beta.com"],
        raw_s3_bucket="b", raw_s3_key="raw/multi1",
        spam_verdict="PASS", virus_verdict="PASS", spf_verdict="PASS",
        dkim_verdict="PASS", dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "multipart_attachment.eml").read_bytes()

    result = await ingest_inbound(
        async_session, message=msg, s3=s3,
        hail_mail_base_domain="mail.hail.so", org_rate_per_hour=10_000,
    )
    # One row PER receiving domain, even though both are the same org.
    assert len(result.email_ids) == 2
    domain_ids = {
        d for (_eid, _oid) in result.created_email_ids
        for d in [(await async_session.execute(
            select(Email.email_domain_id).where(Email.id == _eid))).scalar_one()]
    }
    assert len(domain_ids) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && DATABASE_URL="postgresql+psycopg://hail:hail@localhost:5432/hail" uv run pytest tests/test_email_ingest.py::test_two_custom_domains_same_org_yield_two_rows -v`
Expected: FAIL — only 1 row (the `seen_orgs` dedup collapses the second domain).

- [ ] **Step 3: Split the dedup by kind**

In `ingest_inbound` (`core/hailhq/core/email_ingest.py`), replace the `seen_orgs` dedup. Initialise both trackers before the loop:

```python
    seen_orgs: set[UUID] = set()          # hail_mail: one row per org (unchanged)
    seen_domain_ids: set[UUID] = set()    # custom: one row per receiving domain
```

and replace the existing skip block:

```python
        if domain.organization_id in seen_orgs:
            continue
        seen_orgs.add(domain.organization_id)
```

with a kind-aware version:

```python
        if domain.kind == "custom":
            if domain.id in seen_domain_ids:
                continue
            seen_domain_ids.add(domain.id)
        else:
            if domain.organization_id in seen_orgs:
                continue
            seen_orgs.add(domain.organization_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && DATABASE_URL="postgresql+psycopg://hail:hail@localhost:5432/hail" uv run pytest tests/test_email_ingest.py -v`
Expected: PASS — the new multi-domain test, plus all existing tests (hail-mail per-org dedup unchanged).

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/email_ingest.py core/tests/test_email_ingest.py
git commit -m "feat(email): dedup inbound custom rows per receiving domain"
```

---

### Task 3: Put the matched domain name in the `email.received` webhook payload

**Files:**

- Modify: `core/hailhq/core/webhook_fanout.py` (`build_event_data`)
- Modify: `core/hailhq/core/email_ingest.py` (the `build_event_data` call, ~line 453)
- Test: `core/tests/test_email_ingest.py` + `core/tests/test_webhook_fanout.py` (if present; else inline)

**Interfaces:**

- Produces: `build_event_data(..., email_domain: str | None = None)` includes `"email_domain"` in the returned dict; inbound fan-out passes the matched `domain.domain`. Integrators route on this string instead of the `X-Hail-Email-Domain` UUID header.

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_email_ingest.py` (asserts the fan-out callback receives the domain name):

```python
@pytest.mark.asyncio
async def test_inbound_fanout_payload_carries_email_domain_name(async_session):
    org_id = uuid.uuid4()
    async_session.add(_make_custom_inbound_domain(org_id, "mail.acme.com"))
    await async_session.commit()

    msg = InboundMessage(
        provider_message_id="dn1", envelope_from="x@example.com",
        envelope_recipients=["support@mail.acme.com"],
        raw_s3_bucket="b", raw_s3_key="raw/dn1",
        spam_verdict="PASS", virus_verdict="PASS", spf_verdict="PASS",
        dkim_verdict="PASS", dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "multipart_attachment.eml").read_bytes()

    captured = {}
    async def _fanout(db, *, organization_id, email_domain_id, event_type, event_id, data):
        if event_type == "email.received":
            captured.update(data)
        return 1

    await ingest_inbound(
        async_session, message=msg, s3=s3,
        hail_mail_base_domain="mail.hail.so", org_rate_per_hour=10_000,
        fanout=_fanout,
    )
    assert captured.get("email_domain") == "mail.acme.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && DATABASE_URL="postgresql+psycopg://hail:hail@localhost:5432/hail" uv run pytest tests/test_email_ingest.py::test_inbound_fanout_payload_carries_email_domain_name -v`
Expected: FAIL — `captured.get("email_domain")` is `None` (field not built).

- [ ] **Step 3: Add the field + pass it**

In `core/hailhq/core/webhook_fanout.py`, add the parameter and dict entry to `build_event_data`:

```python
def build_event_data(
    *,
    email_id: str,
    direction: str,
    from_address: str,
    to_addresses: list[str],
    subject: str,
    message_id: str | None,
    in_reply_to: str | None,
    spam_verdict: str | None,
    virus_verdict: str | None,
    spf_verdict: str | None,
    dkim_verdict: str | None,
    dmarc_verdict: str | None,
    raw_url: str | None,
    attachments: list[dict[str, Any]],
    email_domain: str | None = None,
) -> dict[str, Any]:
    return {
        "id": email_id,
        "direction": direction,
        "email_domain": email_domain,
        "from_address": from_address,
        # ... rest unchanged ...
        "to_addresses": to_addresses,
        "subject": subject,
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "spam_verdict": spam_verdict,
        "virus_verdict": virus_verdict,
        "spf_verdict": spf_verdict,
        "dkim_verdict": dkim_verdict,
        "dmarc_verdict": dmarc_verdict,
        "raw_url": raw_url,
        "attachments": attachments,
    }
```

In `core/hailhq/core/email_ingest.py`, pass `email_domain=domain.domain` in the `build_event_data(...)` call (~line 453), and add `"email_domain": domain.domain` to the inline `data={...}` dict used for the `email.received.suppressed` fan-out (~line 478) so both event shapes carry it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && DATABASE_URL="postgresql+psycopg://hail:hail@localhost:5432/hail" uv run pytest tests/test_email_ingest.py tests/test_webhook_fanout.py -v`
Expected: PASS. If a `test_webhook_fanout.py` asserts the exact payload dict, update it to include `email_domain`.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/webhook_fanout.py core/hailhq/core/email_ingest.py core/tests/test_email_ingest.py
git commit -m "feat(email): include matched domain name in email.received webhook payload"
```

---

### Task 4: Make the SES receipt rule catch-all

**Files:**

- Modify: `infra/terraform/ses_inbound.tf` (`aws_ses_receipt_rule.main`)

**Interfaces:**

- Produces: the active receipt rule no longer scopes `recipients` to the hail-mail base domain, so SES accepts mail for any verified identity (incl. custom domains) and the ingest layer does the routing. No per-domain SES write is ever needed.

- [ ] **Step 1: Drop the recipients scope**

In `infra/terraform/ses_inbound.tf`, remove the `recipients` line from `aws_ses_receipt_rule.main` so the rule matches all recipients, and add a comment explaining why:

```hcl
resource "aws_ses_receipt_rule" "main" {
  name          = local.rule_name
  rule_set_name = aws_ses_receipt_rule_set.main.rule_set_name
  enabled       = true
  scan_enabled  = true
  # Catch-all (no `recipients`): SES accepts mail for ANY verified identity in
  # this account — the hail-mail base domain and every verified custom sender
  # domain. Routing to the right org/domain happens in the ingest layer
  # (email_ingest._find_domain_for_recipient), so no per-domain SES rule is
  # needed and the 200-rule / 100-recipient receipt-rule limits never bind.
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

If `var.hail_mail_base_domain` is now unused anywhere in the module, leave the variable declared (other resources/outputs may reference it) — only remove the `recipients` usage.

- [ ] **Step 2: Validate the Terraform**

Run (if Terraform is installed):

```bash
cd infra/terraform && terraform fmt -check && terraform validate
```

Expected: formatted + valid. If `terraform` is not available in this environment, confirm by inspection that the `recipients` line is removed and the block is otherwise unchanged, and note it in the report for an operator to `terraform plan` before applying.

> ⚠️ **This rewrites a LIVE prod rule** (it currently scopes to the hail-mail base domain). The behavioral safety net is the existing hail-mail ingest tests (Task 1/2 regressions) — routing is unchanged; SES merely accepts a wider recipient set. Operators must `terraform plan`/`apply` this as a normal infra change and confirm the existing `mail.hail.so` inbound flow still delivers after apply.

- [ ] **Step 3: Commit**

```bash
git add infra/terraform/ses_inbound.tf
git commit -m "feat(infra): make SES receipt rule catch-all for custom inbound domains"
```

---

## Final verification

- [ ] **Run the core suite**

```bash
cd core && DATABASE_URL="postgresql+psycopg://hail:hail@localhost:5432/hail" uv run pytest -q --ignore=tests/providers/test_twilio_voice.py
```

Expected: all green (existing 253 + the new inbound tests).

- [ ] **Lint + type check**

```bash
cd core && uv run ruff check . && uv run mypy hailhq
```

Expected: no errors.

## Docs follow-up (not code)

The `email.received` webhook payload now carries `email_domain` (the matched receiving domain). Update the inbound-email webhook docs to document the field, and note that integrators should route on it rather than the `X-Hail-Email-Domain` UUID header.

## Spec coverage (Plan 2 scope)

- Gap **D** (custom-domain inbound matching) → Task 1.
- Gap **E** (per-custom-identity dedup) → Task 2; (domain name in payload) → Task 3.
- Catch-all receipt rule (decision 6) → Task 4.

Out of scope (Plan 3): the `/console/emails` UI, the inbound toggle UI, the custom-domain panel.
