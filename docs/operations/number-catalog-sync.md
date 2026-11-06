# Number catalog sync runbook

> **For developers and coding agents:** this page is self-contained. It says what the three carrier catalogs are, who reads them, how they are refreshed every week, which secrets that needs, how to run it by hand, and how to review the pull request it opens.

## What this is

One JSON file per carrier, each row one (country, number type): monthly price, what the number can do (calls, texts, MMS), whether the buyer must be verified first, and whether the carrier still sells it.

- [`costs/twilio.json`](../../costs/twilio.json), [`costs/telnyx.json`](../../costs/telnyx.json), [`costs/didww.json`](../../costs/didww.json). Schema: [`costs/schema/numbers.schema.json`](../../costs/schema/numbers.schema.json).
- Public data, CC-BY-4.0. Canonical URL: `https://raw.githubusercontent.com/hail-hq/hail/main/costs/<carrier>.json`.

Who reads them:

| Reader            | How                                                                                                                                                                                                                                                                                                                                   | Notes                                                                                                                                                                                                                 |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| API purchase gate | [`core/hailhq/core/telephony_catalog.py`](../../core/hailhq/core/telephony_catalog.py) reads `costs/<provider>.json` from disk. `provider="auto"` tries Twilio, then Telnyx, then DIDWW (calls only: DIDWW rows never count for SMS) (`PROVIDERS` in [`core/hailhq/core/number_offers.py`](../../core/hailhq/core/number_offers.py)). | `POST /numbers/quotes` and `POST /numbers` answer 422 `we don't offer a <type> number in <CC> yet` for a pair the carrier's file does not list. A DIDWW purchase is checked against `didww.json`, like the other two. |
| hail-website      | Fetches `costs/twilio.json` by URL: the monthly-fee rater bills held numbers from it, the console picker and marketing "from $X" price read it.                                                                                                                                                                                       | Rows with `available: false` are hidden from the picker and the minimum, but still price held numbers.                                                                                                                |
| `/costs` page     | Renders all three files.                                                                                                                                                                                                                                                                                                              |                                                                                                                                                                                                                       |

`HAIL_TELEPHONY_CATALOG_DIR` points the API at the directory holding the files. Default: the repo's `costs/`. The Docker image sets `/app/costs` ([`api/Dockerfile`](../../api/Dockerfile)). Only override it for tests.

## Weekly refresh

[`.github/workflows/costs-sync-numbers.yml`](../../.github/workflows/costs-sync-numbers.yml) runs every Monday 08:00 UTC (and on **Actions → costs-sync-numbers → Run workflow**). It runs the sync for all three carriers, validates the files against the schema, and opens a pull request on branch `costs/numbers-sync` labelled `costs-sync`. The PR body is the sync summary. No change → no PR.

It needs four repository secrets on `hail-hq/hail` (**Settings → Secrets and variables → Actions**). Set on 2026-09-26. A missing secret skips that carrier with a line in the summary, not an error. It also needs "Allow GitHub Actions to create and approve pull requests" ticked in two places, organization first: <https://github.com/organizations/hail-hq/settings/actions> → Workflow permissions, then the repository's **Settings → Actions → General → Workflow permissions**. The setting only lets a workflow open a pull request; nothing in this repo approves or merges one. Without it the sync step succeeds, the branch is pushed, and "Open PR on change" fails with `GitHub Actions is not permitted to create or approve pull requests`; open the PR by hand with `gh pr create --repo hail-hq/hail --base main --head costs/numbers-sync --title "chore(costs): number catalog sync"`.

The PR requests review from `r13i`. That review request is what triggers GitHub's email; a bot's new PR alone notifies nobody. CI checks do not run on a bot-opened PR, so the workflow validates the files itself before it opens one.

| Secret                                    | Carrier | Where the value comes from                                                                            |
| ----------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------- |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio  | Twilio Console → Account Info. Same values as the API's `.env`.                                       |
| `TELNYX_API_KEY`                          | Telnyx  | Telnyx Mission Control → API Keys. Same value as the API's `.env`.                                    |
| `DIDWW_API_KEY`                           | DIDWW   | my.didww.com → API → DIDWW API 3. Same value as the API's `.env`: the API buys DIDWW numbers with it. |

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

1. A row a person verified by hand (`verification_method: "manual-confirmed"`) keeps every field except `requirements`, and is never marked unavailable. Its `requirements` block is refreshed from the carrier when the fetch succeeds and returns a block; otherwise the old block stays. A block change is listed under **changed**. Other differences, and a carrier that no longer lists it, are reported under **kept** in the summary for a person to decide.
2. A row the carrier no longer lists is kept with `available: false` and a dated note (`not offered by the carrier as of YYYY-MM-DD; kept so held numbers stay billable`). A hand-written `notes` value is kept in front of that note. Listed under **vanished**.
3. A row the carrier lists again is rebuilt from the carrier: `available: true`, and its notes are only the ones the sync writes. Older notes, hand-written ones included, are dropped.
4. A row is marked gone only when the carrier's list covered that country and number type and the row was missing from it. When the carrier lists the type but has no numbers for sale, rule 4a applies. In every other case the row is left exactly as it was and listed under **unobserved**: no price, no dial code, Twilio stock the account cannot see (`no numbers offered to this account`), or a country the sync account cannot see. Not seeing stock is not the same as the carrier dropping the type.
   4a. DIDWW out of stock (`listed, no numbers in stock`): DIDWW still lists the type and shows the price of its empty areas. The row takes the price, `voice`, `sms`, `verification_required`, `receive_only` and `by_request` read from the empty areas, exactly as rule 7 reads areas with stock. A row on sale gets `last_verified` = today and the note `carrier lists this type but had no numbers in stock at the last check`, so the stale check stops listing it. A row that had vanished stays exactly as it vanished (price, flags, notes), so held numbers keep their price. Only its `last_verified` moves, as on the vanished path. Sync-written `monthly price varies by` notes are dropped (a vanished row keeps them, rule 2). If the empty areas need no papers, `requirements` is cleared; if they need papers, the block is fetched as for a fetched row. The next run that sees stock rebuilds the row. A hand-verified row is not touched. Listed under **Listed with no numbers in stock**. A DIDWW type with no price on any group (`listed, no price on any group`), or with an area on sale that has no price (`listed, an area on sale has no price`), stays **unobserved**.
   4b. Telnyx out of stock (`no numbers in stock`) stays **unobserved**: Telnyx shows a price only on a number for sale. The row goes stale after 30 days. Read its price on telnyx.com/pricing.md, then set `last_verified` to that day, `source_url` to `https://telnyx.com/pricing.md`, `verification_method` to `community-pr`, `verified_by` to `telnyx-pricing-page`, and the note `Re-verified YYYY-MM-DD: price read on telnyx.com/pricing.md, because the sync has no stock to observe for this row.` If the price changed, also set `usd_per_month`, `setup_usd` and `last_changed_at`, and start the note with `Price changed YYYY-MM-DD:` instead, ending with the old values (`Was 5.00 a month and 5.00 setup.`).
   Twilio only: a catalog country absent from the account listing is asked the Pricing API once. No prices at all means Twilio sells nothing there through the API (Hail's only way to buy), and its rows are marked gone (rule 2) with `last_verified` and `last_changed_at` set to today. Bulgaria sat at $3.00 for three months this way while Twilio sold no Bulgarian number. A country the Pricing API still prices (Uganda) stays **unobserved**: the account may only lack it.
   Twilio only, per type: a listed country whose Pricing API answer leaves out a type the listing also leaves out sells none of that type, so its row is marked gone the same way. Brazil national and seven other types sat unobserved from July to November this way. A type the Pricing API still prices stays unobserved.
5. A run that returns fewer than 40 countries for a carrier that already has a catalog does not write that file (`CatalogShrunk`). The summary says so; the other carriers still run.
6. Every synced row gets `last_verified` = today, `verification_method: "carrier-sync"`, `verified_by: "<carrier>-api-sync"`. `last_changed_at` moves only when a watched field changed (`usd_per_month`, `voice`, `sms`, `mms`, `verification_required`, `setup_usd`, `available`, `receive_only`, `by_request`).
7. DIDWW only: `by_request: true` when every area of the type that can place calls has `available_dids_enabled: false` (DIDWW picks the number at order time). `receive_only: true` when no area of the type can place calls (`voice_out` missing). Both are written only when true, and both are watched fields. The row price comes from the areas Hail can sell. The API and the console never offer a marked row; see [didww.md](../public/self-host/didww.md#numbers-hail-cannot-sell).
8. `voice`, `sms` and `mms`: a feature one of the sampled numbers has is a yes. For a feature none of them has, the carrier is asked directly: Twilio with a filtered search (`VoiceEnabled` / `SmsEnabled` / `MmsEnabled`), Telnyx through the `features` its `country_coverage` list declares for the number type. A `false` is never read off the sample, which changes from run to run. DIDWW flags come from its number-group attributes.

## Requirements block

Each row has an optional `requirements` block: who may buy the number and which paper categories each kind of buyer needs. The API reads it from the file and makes no carrier call. Schema: `costs/schema/numbers.schema.json`.

```json
"requirements": {
  "person":   {"accepted": false, "items": []},
  "business": {"accepted": true,  "items": ["company_registration", "proof_of_address"]}
}
```

- `accepted: false`: this kind of buyer may not buy the number. `items` is empty.
- `items`: category keys in a fixed order (`hailhq.core.requirement_categories`). A paper no category fits is `{"other": "<carrier label>"}`.
- `null` when the row needs no verification (`verification_required: false`), or when no block was ever read for it.
- The sync reads it once per carrier, country and type, for rows with `verification_required: true`. Read: `telephony_catalog.requirements(country, type, carrier)`.
- On a carrier error the row keeps the block from the last sync (or `null` if it never had one). The failure is listed under "Requirements not refreshed" in the PR summary as `ClassName (HTTP n)`, or as our own message for a case the script names (for example DIDWW listing no requirement group). The carrier's own error text is never printed: the PR body is public.
- DIDWW lists one requirement per DID group. The block is the union of the items of all groups of that country and type.
- If a carrier has rows that need verification but no verification plug-in starts (credentials missing), the sync skips that carrier. Its catalog file stays as it was, and the PR summary lists one bullet under `## Carriers not synced` at the top: `- <carrier>: failed, verification is not configured (check the carrier credentials) (catalog left unchanged)`. A request error or a shrunk catalog is listed there too. The other carriers run normally and the run still succeeds. It never writes empty blocks.
- Rows the carrier still sells but the sync did not see this run (listed under "Not observed" with a skip reason) with `verification_required: true` get a fresh block too, since papers do not depend on stock. Rows the carrier dropped and countries the sync could not look at are not fetched. Nothing else on a kept row changes, and a changed block is listed under **changed**.
- Hand-verified rows (`manual-confirmed`) keep every other field. Their block is refreshed when the fetch succeeds and returns a block; otherwise the old block stays. A block change is listed under **changed** as `CC:type (hand-verified row, only requirements refreshed): person <papers> | business <papers> -> person <papers> | business <papers>`. If the carrier no longer lists verification for the row, the **kept** line ends with `(hand-verified requirements block kept)`.

## Reviewing the sync PR

1. Read the summary sections per carrier: **added**, **changed**, **vanished**, **kept**, **unobserved**, **Skipped by the sync**.
2. **changed** prices: spot-check one against the carrier's pricing page (`source_url` on the row).
3. **vanished** rows: confirm on the carrier's site. A vanished row means the listing enumerated that country and type and it was gone. If the carrier's site still sells it, hand-verify the row (below) so the sync leaves it alone. If it is truly gone, merge; held numbers keep billing.
4. **kept** rows: the carrier now disagrees with a hand-verified value. Decide which is right. To hand over to the sync, delete the row's `verification_method: "manual-confirmed"` line; the next run rewrites the row.
5. Merge. The [`costs-revalidate.yml`](../../.github/workflows/costs-revalidate.yml) workflow waits out GitHub's 5-minute raw-file CDN cache, then tells hail-website to drop its cached copy of the cost files, so the console and the marketing prices show the change about 6 minutes after the merge (fallback without the workflow: its 24-hour fetch cache). The API image picks it up on the next deploy.

To hand-verify a row, set `verification_method: "manual-confirmed"`, `verified_by: "<your GitHub handle>"`, `last_verified: <today>`, and say why in `notes`.

## Where to look

- Sync script and tests: [`scripts/costs/sync_numbers.py`](../../scripts/costs/sync_numbers.py), [`scripts/costs/test_sync_numbers.py`](../../scripts/costs/test_sync_numbers.py). Run: `pip install pytest requests && pytest -q scripts/costs/test_sync_numbers.py`.
- Purchase gate and tests: [`core/hailhq/core/telephony_catalog.py`](../../core/hailhq/core/telephony_catalog.py), [`core/tests/test_telephony_catalog.py`](../../core/tests/test_telephony_catalog.py).
- Dataset conventions (decimal strings, field order, licence): [`costs/README.md`](../../costs/README.md). The weekly LLM/STT/TTS refresh is a different, hand-run procedure: [`refresh-costs.md`](./refresh-costs.md).
