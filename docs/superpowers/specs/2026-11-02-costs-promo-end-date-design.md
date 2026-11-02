# Costs promotion end date

## Problem

Some rows store a promotional or introductory price, because that is what the vendor charges today. The end date was only in `notes`. No check read it, so a row could show an old price for up to 30 days after the promotion ended. `eleven_v4` showed $22 per 1M characters for 3 weeks after its promotion ended, when the price was $80.

## Design

- Add an optional `promo_ends_at` date to the `llm`, `stt`, and `tts` schemas.
- The date is the last day the promotional price applies.
- `scripts/costs/check-stale.mjs` lists a row the day after that date, in its own section, and exits 1. The weekly `costs-stale.yml` run then opens or updates the tracking issue.
- A row whose date has passed stays listed until a pass records the list price and removes the field.

## Limits

- The check says a promotion ended. It does not read the new price.
- The check runs weekly, so a row can be listed up to 7 days after the end date.
- Rows that hold the list price and only mention a promotion in `notes` do not get the field.
