# Email Deliverability Tracking & Visualization — Design

**Date:** 2026-07-01
**Status:** Approved design, pending implementation plan
**Scope:** Email only. Calls already have `call_events`; SMS follows later. The event model, `/events` stream, and UI timeline component are the cross-channel seams.

## Goal

Track the full outbound-email lifecycle (deliverability **and** engagement: delivered, delayed, bounced, complained, rejected, opened, clicked), expose it per email and as account-level aggregates over a time period, fan events out to customer webhooks, and visualize both in the hail-website console (per-email breadcrumb trail + account dashboard).

Decisions made during brainstorming:

- **Event scope:** deliverability + opens/clicks (SES engagement tracking enabled).
- **API surface:** public endpoints (console, CLI, SDK, MCP all consume them).
- **Webhooks:** all lifecycle events fan out (`email.delivered`, `email.delivery_delayed`, `email.bounced`, `email.complained`, `email.opened`, `email.clicked`).
- **Architecture:** per-channel event log (`email_events`, mirroring `call_events`) + query-time aggregation. No rollup tables in v1.

## 1. Ingestion pipeline

New pieces in bold; everything else reuses existing infrastructure.

1. **SES configuration set** (`hail-events`, managed in `infra/terraform/`) with engagement tracking (open + click) enabled. `SesEmailProvider.send_email()` (`core/hailhq/core/providers/email/ses.py`) attaches it to every outbound send via `ConfigurationSetName`.
2. Config-set event destination publishes Send/Delivery/DeliveryDelay/Bounce/Complaint/Reject/Open/Click to an **SNS topic**.
3. The **existing ingest Lambda** (`infra/ses-ingest-lambda/`) gains the SNS topic as a second event source. It signs the payload with the existing HMAC scheme and POSTs to `POST /internal/ses-events` with a new envelope `type: "delivery_event"` (new branch in `api/hailhq/api/routes/internal/ses_events.py`).
4. The API handler resolves the email row via `provider_message_id` (unique partial index already exists on outbound emails), then in one transaction:
   - inserts an `email_events` row,
   - advances `emails.status` where applicable,
   - enqueues webhook fanout rows (`core/hailhq/core/webhook_fanout.py`).

A synthetic `sent` event row is written at send time (in the `POST /emails` path and the outbound worker) so the per-email timeline reads entirely from `email_events`.

**Click-tracking domain:** v1 uses the default SES tracking domain. A custom tracking domain (CNAME per sending domain, piggybacking on the `email_domains` DNS flow) is an explicit fast-follow, out of scope here.

## 2. Data model

### New table `email_events` (Alembic migration; mirrors `call_events`)

| column            | type             | notes                                                                                                     |
| ----------------- | ---------------- | --------------------------------------------------------------------------------------------------------- |
| `id`              | UUID PK          |                                                                                                           |
| `email_id`        | UUID FK → emails |                                                                                                           |
| `organization_id` | UUID             | denormalized for stats queries                                                                            |
| `kind`            | text             | `sent`, `delivered`, `delivery_delayed`, `bounced`, `complained`, `rejected`, `opened`, `clicked`         |
| `payload`         | JSONB            | raw SES detail: bounce type/subtype, SMTP response, delay reason, clicked URL, user-agent, recipient list |
| `occurred_at`     | timestamptz      | from the SES event timestamp, not ingest time                                                             |
| `created_at`      | timestamptz      |                                                                                                           |

Indexes:

- `(email_id, occurred_at)` — per-email timeline.
- `(organization_id, occurred_at, kind)` — stats aggregation.
- UNIQUE `(email_id, kind, occurred_at)` — dedup; SNS delivery is at-least-once. Inserts use `ON CONFLICT DO NOTHING`.

### `emails` changes

- Status enum gains `delivered`.
- Transitions: `sent → delivered`; `sent|delivered → bounced` (hard bounce only); `sent|delivered|bounced → complained`; a `rejected` event sets `status = failed` with `end_reason` from the SES reason. Soft bounces, delays, opens, and clicks record events only and never change status. Terminal states (`bounced`, `complained`, `failed`) are never overwritten by later non-terminal events.

### Backfill

None. Emails predating the feature have no event rows; the UI renders a degraded trail from `status` + existing timestamps (`sent_at`, `failed_at`).

## 3. API surface

### New public endpoints (org-scoped, in OpenAPI)

- `GET /emails/{id}/events` — chronological events for one email. Each item: `kind`, `occurred_at`, and a curated `detail` object (bounce type/subtype + SMTP response, delay reason, clicked URL, user-agent). Powers the breadcrumb UI.
- `GET /emails/stats?from=&to=&bucket=hour|day` —
  - `totals`: counts for sent, delivered, bounced (with hard/soft split), complained, rejected, delayed, opened, clicked; derived rates: delivery, bounce, complaint, open, click.
  - `series`: the same counters per time bucket.
  - Defaults: last 7 days, `bucket=day`. Bounds: range ≤ 92 days; `bucket=hour` only for ranges ≤ 8 days. Out-of-bounds → 422.
  - Rate semantics: denominators are `sent` in the window; open/click rates use unique-per-email counts (raw totals also returned); rates are `null` when `sent = 0`. Docs flag open counts as approximate (Apple MPP).

### Existing surfaces extended

- `GET /events` stream (`api/hailhq/api/routes/events.py`): email events join calls with `source: "email"`, cursor-paginated — fulfilling the existing code comment.
- `GET /emails/{id}`: gains `delivered` status and `last_event_at`.
- Webhooks: `WebhookEventType` gains `email.delivered`, `email.delivery_delayed`, `email.opened`, `email.clicked`; the declared-but-dormant `email.bounced` / `email.complained` go live. Payloads share the events-endpoint item shape. Fanout/retry worker unchanged.

### Downstream

OpenAPI regenerated. Python SDK (hand-maintained, `sdk/hail/client.py`): `emails.events(email_id)` and `emails.stats(...)` methods. CLI: `hail email events <id>`, `hail email stats`. MCP: two new tools, `get_email_events` and `get_email_stats`, matching the existing one-tool-per-endpoint granularity.

## 4. Console UI (hail-website)

Information architecture and data contracts are fixed here; visual/UX design is done with the **frontend-design skill during implementation**.

### Per-email breadcrumbs — in `ActivityDrawer` (`app/console/activity/ActivityDrawer.tsx`)

- Horizontal milestone trail at the top of the email detail: `Queued → Sent → Delivered → Opened → Clicked`. Reached steps solid with timestamps. Failure events (`bounced`, `complained`, `rejected`) replace the trail's tail as a red terminal node with the reason inline. `delivery_delayed` renders as an amber marker on the Delivered step until delivery or bounce resolves it.
- Below: collapsible full event timeline (every row incl. repeat opens, clicked URLs, user-agents) from `GET /emails/{id}/events`.
- Legacy emails (no event rows): trail derived from `status` + timestamps.

### Account overview dashboard

- Header: time-range picker (24h / 7d / 30d / 90d / custom) driving the page via `GET /emails/stats`.
- Headline cards: delivery, bounce, complaint, open, click rates — absolute counts plus health treatment tied to SES reputation thresholds (bounce ≥ 5% and complaint ≥ 0.1% = danger; approaching = warning).
- Main chart: time series of sent/delivered/bounced/complained per bucket; opens/clicks as a second series or toggle.
- "Recent problems" list: latest bounced/complained emails linking into the ActivityDrawer.

### Placement / IA (flexible — final call at frontend-design time)

The dashboard becomes the default email surface. Current `/console/emails` content (domain onboarding, identity, DNS) may be renamed/moved — e.g. `/console/emails` = Overview dashboard with domains under a "Domains" tab, or a separate `/console/domains` nav item. The team is explicitly flexible here; frontend-design picks whichever makes the better UX. The API contract is identical in all variants. Activity stays a pure log.

Chart library: whatever hail-website already uses; if none, frontend-design picks a lightweight one.

## 5. Error handling

- **Duplicate events (SNS at-least-once):** unique index + `ON CONFLICT DO NOTHING`; webhook fanout enqueued only when the insert happened — no duplicate customer webhooks.
- **Out-of-order events:** guarded status transitions (late `delivery_delayed` after `delivered` records the event, never regresses status; terminal states never overwritten).
- **Unmatched `provider_message_id`:** 200-acknowledged, logged with a counter metric. Expected for emails sent outside Hail from the same SES account.
- **Lambda → API failures:** Lambda async retries, then SNS DLQ (new, Terraform) — no silent drops.
- **Bad HMAC / malformed payload:** 401 / 422, same as existing `/internal/ses-events` handling.
- **Stats:** bounds validation (422); `null` rates instead of division by zero.

## 6. Testing

- **core/api (pytest):** SES event-parser fixtures for all 7 provider event types using real SES JSON shapes; status-transition matrix incl. out-of-order and duplicates; ingest endpoint integration tests (HMAC, dedup, fanout enqueued in-transaction); stats SQL tests with seeded events across bucket boundaries and timezone edges.
- **Webhooks:** new event types fan out only to matching subscriptions; payload shape snapshots.
- **hail-website (vitest):** breadcrumb state derivation (events → trail model, incl. legacy fallback); stats page data mapping.
- **CLI (Go):** output tests following existing `email_list.go` patterns.
- **Infra:** Terraform plan reviewed manually; Lambda unit test for the SNS event source branch.

## Out of scope (explicit)

- Custom click/open tracking domains (fast-follow).
- Call/SMS deliverability UI (calls reuse `call_events` later; SMS not yet implemented).
- Pre-aggregated rollup tables (add later behind the same stats API if query-time aggregation gets slow).
- Suppression-list management (bounce/complaint auto-suppression policy is a separate design).
