# Daily activity email worker

## Ownership

The `activity-digest` service belongs to the base `docker-compose.yml` stack.
The base and local stacks build it from the API source.
The production override selects the published API image and adds restart and bounded logging settings.
All stacks use this command: `python -m hailhq.api.activity_digest`.
It runs separately from the API process. The website and GitHub Actions do not schedule or render it.
GitHub still builds and deploys the normal API image when the owner merges a release.

The service has no public ports or persistent volume.
Its environment contains the database connection and dedicated sending credentials only.
It does not inherit carrier keys, AWS credentials, or the website's internal secret.
It has resource limits. The production override adds bounded logs and a restart policy.

This is an internal platform report. It needs the platform database's organization, member, user, and activity tables.
It stays disabled for ordinary self-host installations.

## Report and schedule

The owner requested delivery to the internal recipient, around 08:00 Europe/Stockholm each day.
Each report covers the previous local 08:00 through today's local 08:00. The end is exclusive.
This is not a midnight-to-midnight UTC report.
Daylight saving changes produce 23-hour and 25-hour windows without overlap.

The worker checks the clock at least once a minute.
After success, it remembers the date and avoids repeated API or database work.
A restart uses the existing email receipt to check the same date safely.
A fully missed reporting day needs a separately reviewed backfill.

Each active organization shows its name and owner emails.
Calls, SMS, and emails have separate inbound and outbound lines, with counts and current statuses.
Setup actions, new workspaces, and observed API-key access appear when present.
Inactive organizations and empty categories do not appear.

The report distinguishes incoming traffic, automatic forwarding, and agent sends.
Other source labels describe recorded authentication: API key, user token, or staff user token.
Existing audit data cannot reliably separate console, MCP, and direct API use. The report does not guess.
API access shows the latest recorded API-key request, not a request count or complete history.
The reporting key and the sending workspace's report emails are excluded from totals.

Queries use one read-only, repeatable-read transaction. A failed query stops the report.
Owner joins do not multiply communication counts.
The report excludes message bodies, prompts, transcripts, phone numbers, and secrets.

## Configuration and activation

Production deployment and activation need the owner's explicit instruction.
The code leaves both the service profile and sending disabled by default.

Configure these values in the VM's existing secret file during an approved activation:

- `COMPOSE_PROFILES=activity-digest` includes the service in normal deployment.
- `HAIL_ACTIVITY_DIGEST_ENABLED=true` enables sends.
- `HAIL_ACTIVITY_DIGEST_API_KEY` supplies a dedicated sending-workspace key.
- `HAIL_ACTIVITY_DIGEST_FROM` supplies one bare, verified sender address.
- `HAIL_ACTIVITY_DIGEST_TO` supplies the approved internal recipient.
- `DATABASE_URL` supplies the existing platform database connection.

Compose sets `HAIL_ACTIVITY_DIGEST_API_URL=http://api:8080/v1` on the private network.
An approved local one-shot run can use the public HTTPS API instead.
The key needs `GET /whoami`, `GET /email-domains`, and `POST /emails` access.
The worker verifies the sender in that workspace before sending. Email credits must be available.
It uses the shared `EmailCreate` model, including `message_type="informational"` and `recipient_consent=true`.

Keep the profile in the VM's secret file after activation.
Later deployments pull, recreate, and check the worker against the API image SHA.
There is no separate image or GitHub cron to maintain.

## Preview and manual operation

Run the targeted tests from `api/`. They use the existing Postgres fixture.

```sh
uv run --frozen --package hailhq-api pytest tests/test_activity_digest_*.py
```

A private local preview from the repository root can use an authorized environment file:

```sh
uv run --env-file .env --package hailhq-api python -m hailhq.api.activity_digest --preview --output-dir /tmp/hail-activity-preview
```

Preview reads the database. It never calls the Hail API or sends email.
Files have mode `0600`. Keep them outside Git and remove them after review.
The command logs only a status and date. A preview before 08:00 returns `before_schedule`.

These commands belong to an approved activation or maintenance action:

```sh
# Start after configuration and activation approval.
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d activity-digest

# Read status and bounded logs.
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps activity-digest
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail=50 activity-digest

# One due check. This can send a real email. The enabled flag still applies.
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm activity-digest python -m hailhq.api.activity_digest --once
```

Implementation and preview instructions do not approve a production start or another recipient send.

## Duplicates and recovery

The worker preserves existing `daily-activity-v1:<date>:<recipient-hash>` keys and `daily_activity` email receipts.
Keep the same sending workspace and recipient when moving from the earlier implementation.
Receipts and API idempotency are workspace-scoped. Changing workspaces can resend an accepted date.
No migration or separate job-state table is needed.

A queued, sent, or delivered receipt prevents another submission.
A newer queued receipt is checked again each minute.
A receipt queued for more than 15 minutes requires review.
Failed, bounced, or complained receipts also require review.
An ambiguous send is not retried inside the same run.
A later check first reads the receipt, then reuses the same key if needed.

Hail can cache pre-send rejections, including insufficient credits.
A top-up alone does not clear a cached submission. The worker never rotates keys to bypass it.
API 301, 308, and any 4xx except 408 and 429 are terminal rejections, on reads as well as sends.
A terminal rejection is logged once for that process and date. The next day has its own daily key.
Inspect the receipt and API result before any separately approved recovery send.

Other API 3xx, 408, 429, and 5xx responses, network errors, and database errors retry after one minute.
A 409 for a send still in progress also retries, so the next check can read the receipt.
If the API recorded the email as failed, the next check finds that receipt and closes the date without a second send.
A 409 for the same daily key with a different body closes the date. Inspect the `emails` rows for that key before any recovery send.
Logs show a stable code or the error type, without customer data or credentials.
`submitted` means API acceptance. Use the email ID to check delivery events and final status.

The report reads have a 30 second limit. A read past it fails that check, and the worker tries again one minute later.
The full read took under 2 seconds on 2 million local rows per table. If a real run hits the limit, add `requested_at` indexes on `calls`, `sms`, and `emails`, and an `occurred_at` index on `audit_log`, each built concurrently.

## Stop

During an approved stop, set `HAIL_ACTIVITY_DIGEST_ENABLED=false` and recreate or stop the service.
Remove `activity-digest` from `COMPOSE_PROFILES` to keep it out of future deployments.
Stopping the worker does not retract an email already accepted by Hail.
