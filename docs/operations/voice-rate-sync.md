# Call rate sync runbook

> **For developers and coding agents:** this page says what `costs/voice.json` holds, where each carrier's rates come from, how the weekly sync runs, and how to review its pull request.

## What this is

[`costs/voice.json`](../../costs/voice.json) lists what each carrier charges Hail per minute of call. Schema: [`costs/schema/voice.schema.json`](../../costs/schema/voice.schema.json). Public data, CC-BY-4.0.

- `outbound` row: a call Hail places through the carrier. It covers the numbers in `destination_prefixes`. It applies when the caller ID starts with one of `origination_prefixes`: `ALL` means any caller ID, `ROW` means caller IDs that no other row of that destination lists, `LOCAL` means a caller ID in the same country as the destination. Example: Twilio charges less for a call to a Spanish mobile from an EEA caller ID (the "From Eea" row) than from any other caller ID.
- `usd_per_call` (optional, outbound only): a fixed fee per call, charged on top of `usd_per_minute`.
- `exact_match` (optional, outbound only, always `true`): the row applies only when the dialed number equals a listed prefix, not when it starts with one.
- `inbound` row: a call the carrier delivers to Hail, by the `number_type` of the number that was called.
- `line_type` (`landline`, `mobile`, `toll_free`, `special`, `other`) comes from the carrier's label. `other` is a named zone such as a city. The prefixes decide which row a number matches: take the longest matching destination prefix.

Example: what Twilio charges to call a Spanish mobile, by caller ID.

```bash
jq '.rates[] | select(.provider=="twilio" and .country_code=="ES" and .line_type=="mobile") | {name, origination_prefixes, usd_per_minute}' costs/voice.json
```

## Where the rates come from

| Carrier | Source                                                                                                   | Synced                                 |
| ------- | -------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| Twilio  | Elastic SIP Trunking rates from the Pricing API: `https://pricing.twilio.com/v2/Trunking/Countries/{CC}` | Yes                                    |
| Telnyx  | No rate API is known yet. The rate deck is a file in Mission Control, under the Outbound Voice Profile.  | No: added by hand (see below)          |
| DIDWW   | No public rate list or API is known yet.                                                                 | No: listed under "Carriers not synced" |

A carrier that is not synced keeps any rows it already has.

**Telnyx rows** come from the "Global Voice Conversational" rate file (CSV), added by hand when Telnyx publishes a new one. One row per description, caller ID group, price and per-call fee; `destination_prefixes` lists every prefix of that group. Rows use `verification_method: manual-confirmed`, and `last_changed_at` is the date the rates take effect. In the file, an empty caller ID is `ALL`, `local` is `LOCAL`, and a list is kept as digit prefixes. A prefix with an `X` (Canada N11, `1XXX311`) is skipped, because the schema holds digits only. A row with `Exact Match` gets `exact_match: true`. Nothing reads this file for billing yet: calls are still billed at one rate per minute.

## Build order

This file is step 1 of per-destination call pricing. The steps after it ship in a fixed order: see "Order of work" in [CLAUDE.md](../../CLAUDE.md#order-of-work).

## Weekly refresh

[`.github/workflows/costs-sync-voice.yml`](../../.github/workflows/costs-sync-voice.yml) runs every Monday 08:30 UTC, after the number sync and before the stale check ([order of the Monday jobs](../../costs/README.md#weekly-jobs-in-order)) (and on **Actions → costs-sync-voice → Run workflow**). It runs the sync, validates the file against the schema, and opens a pull request on branch `costs/voice-sync` labelled `costs-sync`. The PR body is the sync summary. No change, no PR.

It uses the `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` repository secrets, the same ones as the number sync ([number-catalog-sync.md](number-catalog-sync.md)). The "Allow GitHub Actions to create and approve pull requests" setting described there applies here too.

Run it by hand:

```bash
uv run python scripts/costs/sync_voice.py --env-file .env
```

Rules the sync follows:

- A synced carrier's rows are replaced by what the carrier returns. A row with the same price and prefixes keeps its `last_changed_at`.
- A fetch with fewer than 80% of the countries the file already holds for that carrier is refused. The file is left unchanged and the summary says why.
- The other carriers' rows are not touched.

## Reviewing the PR

1. Read "Carriers not synced" first. Telnyx and DIDWW are always there until they get a rate source.
2. Check the price changes. A large jump on a destination Hail calls often (US, Canada, the EU) is worth a look on the carrier's pricing page, linked in each row's `source_url`.
3. Merge. The file is data only: nothing deploys and no price changes for customers.
