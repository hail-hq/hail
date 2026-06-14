# Inbound Email Billing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Git policy:** Never run git write commands. "Commit checkpoint" steps are user-facing — report the message; the user commits.

**Goal:** Charge $0.01 per inbound email received (once, regardless of fan-out); when an org is out of credit, still persist + charge but suppress forwarding with an `insufficient_funds` reason.

**Architecture:** `core` ingest gains an injected `funds_check` and exposes which rows were newly created; the API layer (`/internal/ses-events`) meters each created inbound row via a shared `write_usage_event` helper (extracted from the outbound path); the website rater prices the new `email_inbound` channel at 1 cent/message.

**Tech Stack:** FastAPI + SQLAlchemy async (api/core), pytest; Next.js + vitest (website TS).

Spec: `docs/superpowers/specs/2026-06-13-inbound-email-billing-design.md`

---

### Task 1: `IngestResult.created_email_ids`

Metering must charge only newly-created rows, not SES redeliveries. `email_ids` includes replays; add a field that holds only the created ones.

**Files:**

- Modify: `core/hailhq/core/email_ingest.py`
- Test: `core/tests/test_email_ingest.py`

- [ ] **Step 1: Write the failing test** — append to `core/tests/test_email_ingest.py`:

```python
@pytest.mark.asyncio
async def test_created_email_ids_excludes_replays(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    async_session.add(domain)
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    msg = InboundMessage(
        provider_message_id="created-1",
        envelope_from="alice@example.com",
        envelope_recipients=[domain.domain],
        raw_s3_bucket="b",
        raw_s3_key="raw/created-1",
        spam_verdict="PASS", virus_verdict="PASS", spf_verdict="PASS",
        dkim_verdict="PASS", dmarc_verdict="PASS",
        received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    first = await ingest_inbound(
        async_session, message=msg, s3=s3,
        hail_mail_base_domain="mail.hail.so", org_rate_per_hour=10_000,
    )
    second = await ingest_inbound(
        async_session, message=msg, s3=s3,
        hail_mail_base_domain="mail.hail.so", org_rate_per_hour=10_000,
    )
    assert len(first.created_email_ids) == 1
    assert second.created_email_ids == []      # replay creates nothing
    assert len(second.email_ids) == 1          # but still resolves the row
```

- [ ] **Step 2: Run it** — `cd core && uv run pytest tests/test_email_ingest.py::test_created_email_ids_excludes_replays -v` → FAIL (`AttributeError: created_email_ids`).

- [ ] **Step 3: Implement** — in `core/hailhq/core/email_ingest.py`, extend the dataclass:

```python
@dataclass
class IngestResult:
    email_ids: list[UUID] = field(default_factory=list)
    created_email_ids: list[UUID] = field(default_factory=list)
    suppressed_reasons: list[str] = field(default_factory=list)
    skipped_recipients: list[str] = field(default_factory=list)
```

and in `ingest_inbound`, right after `result.email_ids.append(email_id)`:

```python
        result.email_ids.append(email_id)
        if created:
            result.created_email_ids.append(email_id)
```

- [ ] **Step 4: Run** → PASS. Then full file: `uv run pytest tests/test_email_ingest.py -q`.

- [ ] **Step 5: Commit checkpoint (user).** `feat(core): expose created_email_ids from ingest for inbound metering`

---

### Task 2: Funds-gated forwarding + `insufficient_funds` reason

**Files:**

- Modify: `core/hailhq/core/email_ingest.py`
- Test: `core/tests/test_email_ingest.py`

- [ ] **Step 1: Write the failing test:**

```python
@pytest.mark.asyncio
async def test_unfunded_org_suppresses_forwarding_with_reason(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    domain.inbound_enabled = True
    domain.forward_to = ["ops@example.com"]
    async_session.add(domain)
    await async_session.commit()

    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    forward_enqueue = AsyncMock()
    fanout = AsyncMock(return_value=1)

    async def broke(_db, _org_id):
        return False

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="nofunds-1",
            envelope_from="alice@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b", raw_s3_key="raw/nofunds-1",
            spam_verdict="PASS", virus_verdict="PASS", spf_verdict="PASS",
            dkim_verdict="PASS", dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3, hail_mail_base_domain="mail.hail.so",
        forward_enqueue=forward_enqueue, fanout=fanout,
        funds_check=broke, org_rate_per_hour=10_000,
    )
    assert len(result.created_email_ids) == 1          # mail still stored
    forward_enqueue.assert_not_awaited()               # but not forwarded
    assert "insufficient_funds" in result.suppressed_reasons
    types = [c.kwargs["event_type"] for c in fanout.await_args_list]
    assert "email.received" in types                   # webhook still fires
    sup = next(c.kwargs for c in fanout.await_args_list
               if c.kwargs["event_type"] == "email.received.suppressed")
    assert sup["data"]["reason"] == "insufficient_funds"


@pytest.mark.asyncio
async def test_funded_org_forwards_normally(async_session):
    org_id = uuid.uuid4()
    domain = _make_inbound_domain(org_id)
    domain.inbound_enabled = True
    domain.forward_to = ["ops@example.com"]
    async_session.add(domain)
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = (FIX / "simple.eml").read_bytes()
    forward_enqueue = AsyncMock()

    async def funded(_db, _org_id):
        return True

    result = await ingest_inbound(
        async_session,
        message=InboundMessage(
            provider_message_id="funds-1",
            envelope_from="alice@example.com",
            envelope_recipients=[domain.domain],
            raw_s3_bucket="b", raw_s3_key="raw/funds-1",
            spam_verdict="PASS", virus_verdict="PASS", spf_verdict="PASS",
            dkim_verdict="PASS", dmarc_verdict="PASS",
            received_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ),
        s3=s3, hail_mail_base_domain="mail.hail.so",
        forward_enqueue=forward_enqueue, funds_check=funded,
        org_rate_per_hour=10_000,
    )
    forward_enqueue.assert_awaited_once()
    assert "insufficient_funds" not in result.suppressed_reasons
```

- [ ] **Step 2: Run** → FAIL (`funds_check` not a kwarg).

- [ ] **Step 3: Implement.** Add the type alias near the other callback aliases (`ForwardEnqueue`, `FanoutFn`) in `core/hailhq/core/email_ingest.py`:

```python
FundsCheck = Callable[[AsyncSession, UUID], Awaitable[bool]]
```

Add `"FundsCheck"` to `__all__`. Add the parameter to `ingest_inbound` (place it next to `fanout`, keep `org_rate_per_hour` keyword-only at the end as today):

```python
    fanout: FanoutFn | None = None,
    funds_check: FundsCheck | None = None,
    api_base_url: str | None = None,
    org_rate_per_hour: int,
```

Replace the forward block (currently `if created and suppress is None and not over_cap and forward_enqueue is not None and domain.inbound_enabled:` → `row_reasons += await _enqueue_forwards(...)`) with:

```python
        if (
            created
            and suppress is None
            and not over_cap
            and forward_enqueue is not None
            and domain.inbound_enabled
        ):
            funded = funds_check is None or await funds_check(
                db, domain.organization_id
            )
            if funded:
                row_reasons += await _enqueue_forwards(
                    db,
                    domain=domain,
                    parsed=parsed,
                    inbound_id=email_id,
                    hops=inbound_hops,
                    hail_mail_base_domain=hail_mail_base_domain,
                    forward_max_hops=forward_max_hops,
                    forward_default_per_hour=forward_default_per_hour,
                    forward_enqueue=forward_enqueue,
                )
            elif domain.forward_to:
                # Out of credit: keep + store + (Task 4) charge, but don't spend
                # on SES forwards. Only flag when targets were actually configured.
                row_reasons.append("insufficient_funds")
```

The existing `row_reasons` fold + the suppressed-event loop already turn `insufficient_funds` into both `result.suppressed_reasons` and an `email.received.suppressed` fanout — no other change needed.

- [ ] **Step 4: Run** both new tests → PASS; then full `tests/test_email_ingest.py` + `cd ../api && uv run pytest tests/test_internal_ses_events.py tests/test_internal_ses_events_multi_org.py -q`.

- [ ] **Step 5: Commit checkpoint (user).** `feat(core): suppress forwarding for out-of-credit orgs (insufficient_funds)`

---

### Task 3: Extract a shared `write_usage_event` helper

The outbound metering helper is private to `emails.py`. Extract it so the inbound handler reuses the identical write-and-ping behavior, parameterized by channel.

**Files:**

- Create: `api/hailhq/api/usage.py`
- Modify: `api/hailhq/api/routes/emails.py`
- Test: `api/tests/test_usage_helper.py`

- [ ] **Step 1: Write the failing test** — new `api/tests/test_usage_helper.py`:

```python
"""write_usage_event appends a usage_events row and pings the rater."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from hailhq.api.usage import write_usage_event
from hailhq.core.models import UsageEvent


@pytest.mark.asyncio
async def test_write_usage_event_inserts_row_and_notifies(async_session, monkeypatch):
    org_id = uuid.uuid4()
    # async_session fixture pins the sessionmaker the helper opens internally.
    with patch("hailhq.api.usage.notify_usage_event_recorded") as notify:
        await write_usage_event(
            organization_id=org_id, channel="email_inbound", units=1,
            ref="email_inbound:test-1",
        )
    rows = (
        await async_session.execute(
            select(UsageEvent).where(UsageEvent.ref == "email_inbound:test-1")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].channel == "email_inbound"
    assert rows[0].units == 1
    notify.assert_called_once()
```

(Read `api/tests/conftest.py` for how `async_session` pins `session_scope`'s sessionmaker — the helper opens its own `session_scope()`, so the fixture must point that at the test DB; mirror how `test_emails_api.py` already exercises the outbound usage write.)

- [ ] **Step 2: Run** → FAIL (`No module named hailhq.api.usage`).

- [ ] **Step 3: Implement** `api/hailhq/api/usage.py`:

```python
"""Shared usage-metering helper.

Appends one ``usage_events`` row in a fresh session and pings the website
rater. Best-effort — failures are logged, never re-raised — so a metering
hiccup can't roll back the user-facing operation. Used by both the outbound
send path (``channel='email'``) and the inbound ingest path
(``channel='email_inbound'``).
"""

from __future__ import annotations

import logging
from uuid import UUID

from hailhq.core.db import session_scope
from hailhq.core.internal_webhook import notify_usage_event_recorded
from hailhq.core.models import UsageEvent

logger = logging.getLogger(__name__)

__all__ = ["write_usage_event"]


async def write_usage_event(
    *,
    organization_id: UUID,
    channel: str,
    units: int,
    ref: str,
) -> None:
    try:
        async with session_scope() as session:
            usage = UsageEvent(
                organization_id=organization_id,
                channel=channel,
                units=units,
                ref=ref,
            )
            session.add(usage)
            await session.flush()
            usage_event_id = str(usage.id)
            await session.commit()
    except Exception:  # pragma: no cover - logged, never re-raised
        logger.warning("usage_events write failed for ref=%s", ref, exc_info=True)
        return
    notify_usage_event_recorded(usage_event_id)
```

- [ ] **Step 4: Rewire `emails.py`** — replace the body of the existing `_write_usage_event` with a thin delegation (keeps its current call site and signature intact), or replace the call site. Minimal-diff option: keep `_write_usage_event(organization_id, units, ref)` but delegate:

```python
from hailhq.api.usage import write_usage_event

async def _write_usage_event(organization_id: UUID, units: int, ref: str) -> None:
    await write_usage_event(
        organization_id=organization_id, channel="email", units=units, ref=ref
    )
```

(Drop the now-unused `UsageEvent` / `notify_usage_event_recorded` imports from `emails.py` only if nothing else there uses them — grep first.)

- [ ] **Step 5: Run** — `cd api && uv run pytest tests/test_usage_helper.py tests/test_emails_api.py -q` → PASS (outbound metering unchanged).

- [ ] **Step 6: Commit checkpoint (user).** `refactor(api): extract shared write_usage_event helper`

---

### Task 4: Meter inbound at `/internal/ses-events`

**Files:**

- Modify: `api/hailhq/api/routes/internal/ses_events.py`
- Test: `api/tests/test_internal_ses_events.py`

- [ ] **Step 1: Write the failing test** — append to `api/tests/test_internal_ses_events.py` (reuse the module's `_signed`, `inbound_enabled`, `override_internal_deps`, `client` fixtures and the DB `EmailDomain` setup other tests use):

```python
@pytest.mark.asyncio
async def test_inbound_meters_one_usage_event_per_created_row(
    client, async_session, inbound_enabled, override_internal_deps
):
    # Arrange: an org + hail_mail domain that routes "smoke+acme@mail.hail.so".
    # (Mirror the domain-insert helper used by the existing landing test.)
    from unittest.mock import patch
    from sqlalchemy import select, func
    from hailhq.core.models import UsageEvent

    org_id = await _insert_inbound_domain(async_session, user="smoke", org="acme")
    body = json.dumps(_payload(message_id="meter-1",
                               recipient="smoke+acme@mail.hail.so")).encode()

    with patch("hailhq.api.usage.notify_usage_event_recorded"):
        r1 = await client.post("/internal/ses-events", content=body, headers=_signed(body))
        r2 = await client.post("/internal/ses-events", content=body, headers=_signed(body))
    assert r1.status_code == 200 and r2.status_code == 200

    count = (
        await async_session.execute(
            select(func.count()).select_from(UsageEvent).where(
                UsageEvent.organization_id == org_id,
                UsageEvent.channel == "email_inbound",
            )
        )
    ).scalar_one()
    assert count == 1   # first delivery metered; replay not metered
```

If the module lacks an `_insert_inbound_domain`/landing helper, add a small one that inserts an `EmailDomain(kind='hail_mail', local_prefix_user=..., local_prefix_org=..., domain="<u>+<o>@mail.hail.so", verification_status='verified', organization_id=uuid4())` and returns the org id — copy the shape from `core/tests/test_email_ingest.py::_make_inbound_domain`.

- [ ] **Step 2: Run** → FAIL (count 2 — every delivery meters — or 0 if no metering yet).

- [ ] **Step 3: Implement** — in `api/hailhq/api/routes/internal/ses_events.py`:

Add imports:

```python
from hailhq.api.usage import write_usage_event
from hailhq.core.billing import has_funds
```

Pass `funds_check=has_funds` into the `ingest_inbound(...)` call (alongside the existing `forward_enqueue`/`fanout`/`org_rate_per_hour` kwargs). After the call returns, before building the response:

```python
    for created_id in result.created_email_ids:
        await write_usage_event(
            organization_id=...,  # see note
            channel="email_inbound",
            units=1,
            ref=f"email_inbound:{created_id}",
        )
```

Note on `organization_id`: `created_email_ids` is a flat list of email ids; the org owning each is needed for the usage row. Simplest correct approach — have `ingest_inbound` return `created` as `list[tuple[UUID, UUID]]` of `(email_id, organization_id)` instead of bare ids. **Adjust Task 1**: make `created_email_ids: list[tuple[UUID, UUID]]` and append `(email_id, domain.organization_id)`; update Task 1's test to unpack. Then here:

```python
    for created_id, created_org_id in result.created_email_ids:
        await write_usage_event(
            organization_id=created_org_id,
            channel="email_inbound",
            units=1,
            ref=f"email_inbound:{created_id}",
        )
```

(Make this the canonical shape; the billing test in Task 1 asserts `len(...) == 1` which is tuple-agnostic, but update its replay assertion to `second.created_email_ids == []`.)

- [ ] **Step 4: Run** — the new test + `tests/test_internal_ses_events.py` + `tests/test_internal_ses_events_multi_org.py` → PASS. Multi-org: confirm two orgs on one delivery → two `email_inbound` usage rows, one per org.

- [ ] **Step 5: Commit checkpoint (user).** `feat(api): meter inbound email at $0.01 per received message`

---

### Task 4b: Flatten outbound email to 1¢ per send

Per the pricing decision, outbound is also flat 1¢ per send (not per-recipient).

**Files:**

- Modify: `api/hailhq/api/routes/emails.py` (the `_write_usage_event` call site, ~line 411)
- Test: `api/tests/test_emails_api.py`

- [ ] **Step 1: Write/adjust the failing test** — POST an email to 3 recipients (to + cc + bcc), assert exactly one `usage_events` row with `channel='email'` and `units=1` (not 3). If an existing test asserts `units == recipient_count`, update it to `units == 1`.

- [ ] **Step 2: Run** → FAIL (units is the recipient count today).

- [ ] **Step 3: Implement** — change the metering call in `create_email` from:

```python
    await _write_usage_event(
        organization_id=principal.organization_id,
        units=(len(email.to_addresses) + len(email.cc_addresses or []) + len(email.bcc_addresses or [])),
        ref=f"email:{email.id}",
    )
```

to:

```python
    # Flat 1¢ per send regardless of recipient count (units=1).
    await _write_usage_event(
        organization_id=principal.organization_id,
        units=1,
        ref=f"email:{email.id}",
    )
```

- [ ] **Step 4: Run** → PASS; full `tests/test_emails_api.py` green (fix any recipient-count assertion).

- [ ] **Step 5: Commit checkpoint (user).** `feat(billing): flat 1-cent-per-send outbound email pricing`

---

### Task 5: Website rater prices `email_inbound`

**Files:**

- Modify: `hail-website/lib/private-rates.ts`, `hail-website/lib/usage-rater.ts`
- Test: `hail-website/lib/__tests__/private-rates.test.ts` (or the existing rater/rates test file — read the dir first)

- [ ] **Step 1: Write the failing test** (vitest):

```ts
import { describe, it, expect } from "vitest";
import { rateUsageCents } from "@/lib/private-rates";

describe("inbound email rate", () => {
  it("charges 1 cent per received message", () => {
    expect(rateUsageCents("email_inbound", 1)).toBe(1);
  });
});
```

- [ ] **Step 2: Run** — `cd hail-website && pnpm vitest run lib/__tests__/private-rates.test.ts` → FAIL (type error / unhandled channel).

- [ ] **Step 3: Implement** — in `lib/private-rates.ts`:

```ts
const RATES_CENTS_PER_UNIT = {
  voice_cents_per_ms: 1.25 / 60_000,
  sms_cents_per_segment: 0.79,
  // Flat 1¢ per message, both directions (units is always 1 for email now).
  email_cents_per_message: 1.0,
} as const;

export type UsageChannel = "voice" | "sms" | "email" | "email_inbound";
```

Update the `rateUsageCents` switch so **both** email channels use the flat
per-message rate (units=1 from the API for both):

```ts
      case "email":
      case "email_inbound":
        return units * RATES_CENTS_PER_UNIT.email_cents_per_message;
```

(Removes the old `email_cents_per_send: 0.01` per-recipient constant.)

In `lib/usage-rater.ts`, wherever the channel string from a `usage_events` row is passed to `rateUsageCents`, ensure `'email_inbound'` flows through unchanged (if the rater whitelists channels, add it).

- [ ] **Step 4: Run** → PASS; then `pnpm vitest run` (full) to confirm the union change didn't break other call sites.

- [ ] **Step 5: Commit checkpoint (user).** `feat(billing): price inbound email channel at 1 cent/message`

---

### Task 6: Ledger label + tier copy

**Files:**

- Modify: `hail-website/lib/billing-queries.ts`, `hail-website/lib/billing-tiers.ts`, `hail-website/app/console/billing/BillingClient.tsx`
- Test: `hail-website/lib/__tests__/billing-queries.test.ts` (or existing)

- [ ] **Step 1: Write the failing test** — assert the ledger label/qty for an `email_inbound` debit row. Read `lib/billing-queries.ts` for the label/qty formatter (the function that maps a ledger row to display text — around the "Email batch"/"N sends" logic) and write a unit test:

```ts
import { describe, it, expect } from "vitest";
import { describeLedgerRow } from "@/lib/billing-queries"; // use the real exported fn name

it("labels inbound email debits", () => {
  const row = {
    channel: "email_inbound",
    kind: "debit",
    qty: 3,
    source: "usage_event",
  };
  const out = describeLedgerRow(row as any);
  expect(out.label).toContain("Inbound email");
  expect(out.qty).toContain("received");
});
```

(If the label logic isn't a standalone exported function, extract the channel→label mapping into one small pure function first, then test it — that refactor is in-scope.)

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement:**
  - `billing-queries.ts`: in the channel→label map add `email_inbound → "Inbound email"`, and qty formatting "N received" (mirror the existing "N sends" branch).
  - `billing-tiers.ts`: update the email tier-estimate copy to acknowledge both directions (e.g. "≈5,000 emails in or out for $50"); no numeric change (same rate).
  - `BillingClient.tsx`: the channel filter chip for "email" should also match `email_inbound` rows (so inbound debits appear under the Email filter). Update the filter predicate to treat both channels as "email".

- [ ] **Step 4: Run** — the new test + `pnpm vitest run` + `pnpm build` (or `pnpm tsc --noEmit`) → clean.

- [ ] **Step 5: Commit checkpoint (user).** `feat(billing): inbound-email ledger label + tier copy`

---

### Task 7: Verification sweep

- [ ] **Step 1:** `cd core && uv run pytest -q` ; `cd ../api && uv run pytest -q` → all green.
- [ ] **Step 2:** `cd hail-website && pnpm vitest run && pnpm tsc --noEmit` → clean.
- [ ] **Step 3:** Manual trace: a funded org receiving 1 mail → 1 `email_inbound` usage row → rater → 1-cent debit labeled "Inbound email". An unfunded org → mail stored, charged, forward suppressed (`insufficient_funds` event), webhook still delivered.
- [ ] **Step 4: Commit checkpoint (user).** Suggested final message: `feat(billing): inbound email metering end-to-end`.

---

## Self-review

- **Spec coverage:** decision 1 (one charge) → Task 4 (`units=1`); decision 2 (meter spam-suppressed, not replays) → Task 1 + Task 4 (`created_email_ids`, which is set regardless of `suppress`); decision 3 (out-of-credit) → Task 2; decision 4 (`email_inbound` channel) → Tasks 4–6; decision 5 (self-host unmetered) → inherited (rater only debits orgs with ledgers; `has_funds` on the sentinel org returns true only if it has credits — confirm in Task 7 trace). All covered.
- **Type consistency:** `created_email_ids` is `list[tuple[UUID, UUID]]` `(email_id, org_id)` (locked in Task 4, back-referenced into Task 1). `write_usage_event` is keyword-only `(*, organization_id, channel, units, ref)` everywhere. `funds_check: FundsCheck = Callable[[AsyncSession, UUID], Awaitable[bool]]` matches `has_funds(db, org_id)`.
- **Placeholder scan:** none — concrete code or explicit "read this file then mirror" where an existing component's exact text is required.
