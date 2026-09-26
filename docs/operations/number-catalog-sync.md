# Number catalog sync runbook

> **For developers and coding agents:** this page is self-contained. It says what the three carrier catalogs are, who reads them, how they are refreshed every week, which secrets that needs, how to run it by hand, and how to review the pull request it opens.

## What this is

One JSON file per carrier, each row one (country, number type): monthly price, what the number can do (calls, texts, MMS), whether the buyer must be verified first, and whether the carrier still sells it.

- [`costs/twilio.json`](../../costs/twilio.json), [`costs/telnyx.json`](../../costs/telnyx.json), [`costs/didww.json`](../../costs/didww.json). Schema: [`costs/schema/numbers.schema.json`](../../costs/schema/numbers.schema.json).
- Public data, CC-BY-4.0. Canonical URL: `https://raw.githubusercontent.com/hail-hq/hail/main/costs/<carrier>.json`.

Who reads them:

| Reader            | How                                                                                                                                                                                                                                                                          | Notes                                                                                                                                                                                                                                                 |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| API purchase gate | [`core/hailhq/core/telephony_catalog.py`](../../core/hailhq/core/telephony_catalog.py) reads `costs/<provider>.json` from disk. `provider="auto"` tries Twilio, then Telnyx (`PROVIDERS` in [`core/hailhq/core/number_offers.py`](../../core/hailhq/core/number_offers.py)). | `POST /numbers/quotes` and `POST /numbers` answer 422 `we don't offer a <type> number in <CC> yet` for a pair the carrier's file does not list. DIDWW numbers are not purchasable through the API, so `didww.json` is never consulted for a purchase. |
| hail-website      | Fetches `costs/twilio.json` by URL: the monthly-fee rater bills held numbers from it, the console picker and marketing "from $X" price read it.                                                                                                                              | Rows with `available: false` are hidden from the picker and the minimum, but still price held numbers.                                                                                                                                                |
| `/costs` page     | Renders all three files.                                                                                                                                                                                                                                                     |                                                                                                                                                                                                                                                       |

`HAIL_TELEPHONY_CATALOG_DIR` points the API at the directory holding the files. Default: the repo's `costs/`. The Docker image sets `/app/costs` ([`api/Dockerfile`](../../api/Dockerfile)). Only override it for tests.

## Weekly refresh

[`.github/workflows/costs-sync-numbers.yml`](../../.github/workflows/costs-sync-numbers.yml) runs every Monday 08:00 UTC (and on **Actions → costs-sync-numbers → Run workflow**). It runs the sync for all three carriers, validates the files against the schema, and opens a pull request on branch `costs/numbers-sync` labelled `costs-sync`. The PR body is the sync summary. No change → no PR.

It needs four repository secrets on `hail-hq/hail` (**Settings → Secrets and variables → Actions**). Set on 2026-09-26. A missing secret skips that carrier with a line in the summary, not an error. It also needs **Settings → Actions → General → Workflow permissions → "Allow GitHub Actions to create and approve pull requests"** ticked; without it the sync step succeeds and the "Open PR on change" step fails with `GitHub Actions is not permitted to create or approve pull requests`.

| Secret                                    | Carrier | Where the value comes from                                                                 |
| ----------------------------------------- | ------- | ------------------------------------------------------------------------------------------ |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio  | Twilio Console → Account Info. Same values as the API's `.env`.                            |
| `TELNYX_API_KEY`                          | Telnyx  | Telnyx Mission Control → API Keys. Same value as the API's `.env`.                         |
| `DIDWW_API_KEY`                           | DIDWW   | my.didww.com → API → DIDWW API 3. Only the sync uses it; the API service does not read it. |

To set or rotate one from a local `.env`:

```bash
grep -E '^DIDWW_API_KEY=' .env | cut -d= -f2- | gh secret set DIDWW_API_KEY --repo hail-hq/hail
gh secret list --repo hail-hq/hail
gh workflow run costs-sync-numbers.yml --repo hail-hq/hail   # prove it works
```

Related workflows: [`costs-validate.yml`](../../.github/workflows/costs-validate.yml) checks every `costs/*.json` against its schema and runs the sync's tests on each PR; [`costs-stale.yml`](../../.github/workflows/costs-stale.yml) opens a `costs-stale` issue on Mondays 09:00 UTC listing rows whose `last_verified` is older than 30 days.

## Run it by hand

```bash
uv run python scripts/costs/sync_numbers.py --provider all --env-file .env --summary-file /tmp/sync.md
pnpm run costs:validate
git diff --stat costs/
```

Flags: `--provider {didww,telnyx,twilio,all}`, `--env-file` (reads the keys from a dotenv file instead of the shell), `--costs-dir` (default `costs/`), `--summary-file` (Markdown, the same text the workflow puts in the PR body). The script only sends GET requests: no purchases, no account changes.

## Merge rules

[`scripts/costs/sync_numbers.py`](../../scripts/costs/sync_numbers.py), function `merge()`, applies these in order:

1. A row a person verified by hand (`verification_method: "manual-confirmed"`) is never overwritten and never marked unavailable. Differences, and a carrier that no longer lists it, are reported under **kept** in the summary for a person to decide.
2. A row the carrier no longer lists is kept with `available: false` and a dated note (`not offered by the carrier as of YYYY-MM-DD; kept so held numbers stay billable`). A hand-written `notes` value is kept in front of that note. Listed under **vanished**.
3. A row the carrier lists again gets `available: true` and the dated note removed. Hand-written notes stay.
4. A row the sync could not observe this run is left exactly as it was and listed under **unobserved**: the account was offered no numbers, no price, no dial code, or (Twilio) the country is missing from the account's own listing. Twilio's `AvailablePhoneNumbers` list only has countries the account is enabled for, so a missing country says nothing about what Twilio sells. Not seeing stock is not the same as the carrier dropping the type.
5. A run that returns fewer than 40 countries for a carrier that already has a catalog does not write that file (`CatalogShrunk`). The summary says so; the other carriers still run.
6. Every synced row gets `last_verified` = today, `verification_method: "carrier-sync"`, `verified_by: "<carrier>-api-sync"`. `last_changed_at` moves only when a watched field changed (`usd_per_month`, `voice`, `sms`, `mms`, `verification_required`, `setup_usd`, `available`).

## Reviewing the sync PR

1. Read the summary sections per carrier: **added**, **changed**, **vanished**, **kept**, **unobserved**, **Skipped by the sync**.
2. **changed** prices: spot-check one against the carrier's pricing page (`source_url` on the row).
3. **vanished** rows: confirm on the carrier's site. A vanished row means the listing enumerated that country and type and it was gone. If the carrier's site still sells it, hand-verify the row (below) so the sync leaves it alone. If it is truly gone, merge; held numbers keep billing.
4. **kept** rows: the carrier now disagrees with a hand-verified value. Decide which is right. To hand over to the sync, delete the row's `verification_method: "manual-confirmed"` line; the next run rewrites the row.
5. Merge. hail-website picks up `twilio.json` within 24 hours (its fetch cache); the API image picks it up on the next deploy.

To hand-verify a row, set `verification_method: "manual-confirmed"`, `verified_by: "<your GitHub handle>"`, `last_verified: <today>`, and say why in `notes`.

## Where to look

- Sync script and tests: [`scripts/costs/sync_numbers.py`](../../scripts/costs/sync_numbers.py), [`scripts/costs/test_sync_numbers.py`](../../scripts/costs/test_sync_numbers.py). Run: `pip install pytest requests && pytest -q scripts/costs/test_sync_numbers.py`.
- Purchase gate and tests: [`core/hailhq/core/telephony_catalog.py`](../../core/hailhq/core/telephony_catalog.py), [`core/tests/test_telephony_catalog.py`](../../core/tests/test_telephony_catalog.py).
- Dataset conventions (decimal strings, field order, licence): [`costs/README.md`](../../costs/README.md). The weekly LLM/STT/TTS refresh is a different, hand-run procedure: [`refresh-costs.md`](./refresh-costs.md).
