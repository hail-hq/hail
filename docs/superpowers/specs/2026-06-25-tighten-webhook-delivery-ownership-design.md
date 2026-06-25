# Tighten webhook delivery ownership (migration 0014)

Date: 2026-06-25

## Context

`f9ab1aa` consolidated webhook delivery onto a single `WebhookSubscription`
model and dropped the per-domain webhook path. Two altitude findings from the
post-commit review were deferred because their fixes reached beyond the
reviewed lines:

1. The `webhook_deliveries_target_check` CHECK that 0013 left as
   `subscription_id IS NOT NULL` is a degenerate null-guard wearing a
   "target" name. With one owner, the invariant belongs on the column as
   `NOT NULL`.
2. `webhook_deliveries.email_domain_id` is now purely informational (it stamps
   the `X-Hail-Email-Domain` header, not a routing target), but its FK still
   carries `ON DELETE CASCADE` (introduced in 0008). Deleting an email domain
   would silently delete unrelated delivery audit/retry rows.

0013 is already applied to a live database, so the schema change ships as a new
forward migration 0014 rather than an edit to 0013.

## Changes

### `api/migrations/versions/0014_tighten_webhook_delivery_ownership.py` (revises 0013)

`upgrade()`:

1. Drop CHECK `webhook_deliveries_target_check`.
2. `ALTER COLUMN subscription_id SET NOT NULL`. Safe on live data: 0013's CHECK
   already guarantees no NULL `subscription_id` rows exist.
3. Drop FK `webhook_deliveries_email_domain_id_fkey`; recreate it
   `ON DELETE SET NULL`.

`downgrade()` restores the exact 0013 state: FK back to `CASCADE`,
`subscription_id` `DROP NOT NULL`, recreate the `subscription_id IS NOT NULL`
CHECK.

### `core/hailhq/core/models.py` — `WebhookDelivery`

- `subscription_id` → `Mapped[uuid.UUID]`, `nullable=False`.
- `email_domain_id` FK → `ondelete="SET NULL"`.
- Remove the redundant `webhook_deliveries_target_check` CheckConstraint (the
  NOT NULL column now carries the invariant).

### `core/hailhq/core/webhook_worker.py`

Remove three now provably-dead `subscription_id` guards (the column is
non-nullable): the `X-Hail-Subscription` header emit and the two
subscription-update branches in `_record_success` / `_record_failure`. This
also aligns the code with the docs' "`X-Hail-Subscription` — always present."

### Tests

- Pin `test_0013_drops_per_domain_webhook` to revision `0013` (it currently
  upgrades to `head` and asserts `webhook_deliveries_target_check` exists; 0014
  drops it).
- Add `test_0014_*`: NOT NULL enforced, FK is `SET NULL`, down/up round-trip
  clean.
- Add model assertions: `subscription_id` non-nullable; `email_domain_id` FK
  `ondelete='SET NULL'`.
