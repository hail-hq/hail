# Deleting one call from a workspace

Use this when a customer asks to remove one call, or to remove a test call
([first-number-call-test.md](first-number-call-test.md)). It deletes the
call, its recording, its usage rows and its charges. Deleting the charge
gives the money back: the balance is the sum of `account_credits`.
Phone calls only: a browser voice call has other ledger refs
(`browser:<id>`), and step 4 refuses it.

What it removes, in order:

1. The recording object in storage.
2. In one transaction: the `voice_cost_settlements` rows and the
   `account_credits` rows of the call's usage events, the `usage_events`
   rows, and the `calls` row.
   - `call_events`, `call_legs` and `call_balance_holds` go with the call
     (`ON DELETE CASCADE`).
   - `phone_numbers.reserved_call_id` is set to null (`ON DELETE SET NULL`).
   - `voice_cost_settlements` is a website table (the call price settle
     step). It is not in this repo's models or migrations.

What stays: `webhook_deliveries` rows (`event_id` is the call id; the
customer's endpoint already got them) and `audit_log` rows.

Schema: [`models.py`](../../core/hailhq/core/models.py) (`Call`,
`UsageEvent`, `AccountCredit`). A call's usage rows have `channel = 'voice'`
and a `ref` that starts with the call id (`<call id>` or
`<call id>:recording-v1`). Their charges have the ref
`usage_event:<usage event id>`; the settle step writes the difference as
`voice_cost:<usage event id>` (a debit or a credit).

## 1. Find the call

```bash
psql "$DATABASE_URL" <<'SQL'
\set org '<org id>'
\set e164 '+<number on the call>'
SELECT id, direction, from_e164, to_e164, status, end_reason, requested_at,
       metadata->>'recording_status' AS recording_status,
       COALESCE(recording_s3_key, metadata->>'recording_object_key') AS recording_key
FROM calls
WHERE organization_id = :'org' AND (to_e164 = :'e164' OR from_e164 = :'e164')
ORDER BY requested_at DESC LIMIT 5;
SQL
```

Note the call `id` and the `recording_key`. Then look at its usage rows and
charges:

```bash
psql "$DATABASE_URL" <<'SQL'
\set org '<org id>'
\set call '<call id>'
SELECT ue.id AS usage_event_id, ue.ref, ue.units, ue.priced_at,
       ac.ref AS charge_ref, ac.amount_cents, s.settled_at, s.failures
FROM usage_events ue
LEFT JOIN account_credits ac ON ac.organization_id = ue.organization_id
  AND ac.ref IN ('usage_event:' || ue.id, 'voice_cost:' || ue.id)
LEFT JOIN voice_cost_settlements s ON s.usage_event_id = ue.id
WHERE ue.organization_id = :'org' AND ue.channel = 'voice'
  AND split_part(ue.ref, ':', 1) = :'call';
SQL
```

No rows: the call was not billed (nobody answered). Step 4 still deletes
the call row.

## 2. Check that the rows can go

Run the script in step 4 with `\set apply false`. It makes every check and
every delete, then rolls back. Go on only when it ends with
`Checks passed`. Otherwise the recording would be gone while the call stays
in the customer's call log.

The script stops, and changes nothing, when:

- no phone call has this id in this organization (check the ids);
- the call is live or ended less than 15 minutes ago (the voicebot writes
  the call legs after the call ends);
- the recording upload may still be running;
- a usage row is not priced yet (the usage rater has not run);
- a charge still waits for the settle step. It runs once no call leg cost
  is `pending`: usually within minutes, and within 48 hours for most calls
  (Hail stops waiting for a leg cost then). When the step keeps failing for
  a call, the script can refuse for up to 14 days after the charge.
  Deleting before it would leave a stray `voice_cost:` row.

Run it again later when it stops for one of the last four.

## 3. Delete the recording

Skip this when `recording_key` is empty. Use your operator AWS credentials.
The production app's storage credentials may not allow deletes. The bucket
is `S3_BUCKET` in the server `.env`.

```bash
aws s3 rm "s3://<recordings bucket>/<recording_key>"
aws s3 ls "s3://<recordings bucket>/<recording_key>"
```

The second command prints nothing when the object is gone. Keys under
`recordings/7d/` and `recordings/30d/` also expire on their own after 7 or
30 days, and `recordings/365d/` after a year
([`s3_assets.tf`](../../infra/terraform/s3_assets.tf) lifecycle rules).
Delete it now anyway.

## 4. Delete the rows

The same script runs both times: `apply` is `false` in step 2 and `true`
here. Any other value rolls back and deletes nothing.

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
\set org '<org id>'
\set call '<call id>'
\set apply '<false in step 2, true in step 4>'
BEGIN;
-- The workspace's billing lock, as purchases and the raters take it.
SELECT pg_advisory_xact_lock(hashtextextended((:'org')::uuid::text, 0));
SELECT count(*) = 1 AS found,
       COALESCE(bool_and(
         status IN ('completed', 'failed', 'busy', 'no_answer', 'canceled')
         AND COALESCE(ended_at, updated_at) < now() - interval '15 minutes'),
         false) AS ended,
       COALESCE(bool_and(COALESCE(metadata->>'recording_status', 'disabled')
         NOT IN ('requested', 'starting', 'recording', 'processing')),
         false) AS recording_done
FROM calls
WHERE id = :'call' AND organization_id = :'org' AND transport = 'phone' \gset
SELECT count(*) FILTER (WHERE priced_at IS NULL) = 0 AS priced,
       count(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM account_credits ac
         WHERE ac.organization_id = ue.organization_id
           AND ac.ref = 'usage_event:' || ue.id
           AND ac.created_at > now() - interval '14 days')
         AND EXISTS (SELECT 1 FROM call_legs l WHERE l.call_id = :'call')
         AND NOT EXISTS (
           SELECT 1 FROM voice_cost_settlements s
           WHERE s.usage_event_id = ue.id
             AND (s.settled_at IS NOT NULL OR s.failures >= 3))) = 0 AS settled
FROM usage_events ue
WHERE ue.organization_id = :'org' AND ue.channel = 'voice'
  AND split_part(ue.ref, ':', 1) = :'call' \gset
\if :found
\else
  \echo 'No phone call with this id in this organization: nothing deleted. Check the ids.'
  ROLLBACK;
  \q
\endif
\if :ended
\else
  \echo 'The call is live or ended less than 15 minutes ago: nothing deleted. Try again in 15 minutes.'
  ROLLBACK;
  \q
\endif
\if :recording_done
\else
  \echo 'Recording upload may still run: nothing deleted. Try again in a few minutes.'
  ROLLBACK;
  \q
\endif
\if :priced
\else
  \echo 'Usage not priced yet: nothing deleted. Try again in a few minutes.'
  ROLLBACK;
  \q
\endif
\if :settled
\else
  \echo 'A charge waits for the settle step: nothing deleted. Try again later.'
  ROLLBACK;
  \q
\endif
CREATE TEMP TABLE gone ON COMMIT DROP AS
SELECT id FROM usage_events
WHERE organization_id = :'org' AND channel = 'voice'
  AND split_part(ref, ':', 1) = :'call';
DELETE FROM voice_cost_settlements WHERE usage_event_id IN (SELECT id FROM gone);
DELETE FROM account_credits
WHERE organization_id = :'org'
  AND ref IN (SELECT 'usage_event:' || id FROM gone
              UNION ALL SELECT 'voice_cost:' || id FROM gone);
DELETE FROM usage_events WHERE id IN (SELECT id FROM gone);
DELETE FROM calls WHERE id = :'call' AND organization_id = :'org';
\if :apply
  COMMIT;
\else
  \echo 'Checks passed. Nothing deleted: apply is not true.'
  ROLLBACK;
\endif
SQL
```

With `apply` true the output must end with `COMMIT`. Run the two queries
in step 1 again: both return no rows for the call.
