# Daily activity Compose worker

**Goal:** Run the owner's daily activity email as a small Hail backend service.

**Architecture:** A Python worker reuses the API image and the production Compose stack. An opt-in `activity-digest` profile controls the service. It reads a read-only database snapshot and sends through the Hail API on the Compose network. The website and GitHub Actions do not run the report.

## Requirements

- Deliver to the approved internal recipient, `r@hail.so`, around 08:00 Europe/Stockholm.
- Keep the existing 08:00-to-08:00 Stockholm reporting window, including daylight saving changes.
- List active organizations, their owner emails, and inbound/outbound activity by channel. Omit empty categories.
- Show recorded authentication honestly. Do not guess console, MCP, or direct API attribution.
- Separate forwarding, agent sends, and incoming traffic from manual sends.
- Preserve existing `daily-activity-v1` keys and `daily_activity` email receipts. A restart must not resend today's email.
- Use `EmailCreate` to validate the public API payload. Service mail uses `message_type="informational"`.
- Reuse existing email receipts and API idempotency. Do not add a schema migration.
- Keep secrets and customer content out of logs. Preview files stay outside Git.
- No public ports, provider credentials, website secret, extra image, or persistent worker volume.
- Keep production disabled. Do not deploy, merge, change production settings, or send another email during implementation.

## Task 1: Report and queries

- [x] Add failing tests for exact cutoffs, both daylight saving transitions, grouped counts, omitted empty categories, escaped HTML, and source labels.
- [x] Port the tested report and read-only SQL into `api/hailhq/api/activity_digest/`.
- [x] Cover owner joins, automatic sends, report exclusion, oldest-membership key attribution, and scoped receipts with Postgres fixture tests.

## Task 2: Delivery and scheduling

- [x] Add failing tests for disabled mode, sender verification, public payload validation, restart deduplication, ambiguous sends, terminal rejections, and graceful stop.
- [x] Add the Hail API client, one-shot/preview command, and worker loop. Wait until the next local cutoff after a completed report.
- [x] Use fixed daily keys and existing receipts. Never rotate keys automatically after a rejection.
- [x] Reuse the existing API package and image. Ensure timezone data is available in the slim runtime image.

## Task 3: Compose and handover

- [x] Add an opt-in production service with restart policy, bounded logs, resource limits, minimal environment, and an API health dependency.
- [x] Include the enabled worker in deployment freshness checks using the API image SHA.
- [x] Document configuration, local preview, activation, failure recovery, stopping, and source limitations.
- [x] Validate Compose, run applicable suites and lint, build the image, and inspect a fixture email.
- [x] Obtain a separate code review and fix important findings.
- [ ] Commit and push the task branch. Open a Hail PR and mark the website PR as superseded once the replacement is reviewable.

## Review focus

- Opt-in profiles must survive normal deployment and remain inactive by default.
- A crash after API acceptance must not create a duplicate on restart.
- Cached pre-send failures need review, not an automatic new idempotency key.
- Report data must stay inside the approved internal recipient and private preview files.
- The worker must not require a running website or a browser session.

## Verification record

- Full API suite: 1,116 tests passed.
- Full core suite: 1,469 tests passed.
- Final activity-targeted suite: 40 tests passed, including six real Postgres cases.
- Shared database URL converter: nine tests passed, including the Node-only `pgbouncer` flag regression.
- Root Ruff and changed-file Black checks passed.
- Production image build passed. The image starts disabled and exits cleanly on SIGTERM.
- Read-only platform preview passed. No email was sent during this migration.
- Desktop and phone fixture email previews were inspected.
- A separate review found no critical or important issue. Its sender-workspace and versioned-route notes were addressed.
- The deployment freshness check was exercised against accepted and stale API images.
