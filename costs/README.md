# Model Costs

Public, validated cost and capability data for AI model providers — LLMs, speech-to-text, and text-to-speech.

The JSON files in this directory are the source of truth. Each row is validated against a JSON Schema in [`schema/`](./schema/) on every pull request. Schema version is `2`.

## Canonical URLs

Programmatic consumers (agents, scripts, dashboards) should fetch directly:

- LLMs: <https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json>
- STT: <https://raw.githubusercontent.com/hail-hq/hail/main/costs/stt.json>
- TTS: <https://raw.githubusercontent.com/hail-hq/hail/main/costs/tts.json>

Schemas are served at their `$id` URLs:

- <https://hail.so/costs/schema/llm.json>
- <https://hail.so/costs/schema/stt.json>
- <https://hail.so/costs/schema/tts.json>

## How AI agents should use this

The data files share a common envelope: `{ version, license, models[] }`. Every model row has `provider`, `model_id`, `display_name`, primary price fields (as decimal strings, e.g. `"5.0"`), `last_verified`, `last_changed_at`, `verification_method`, `verified_by`, and `source_url`. See the JSON Schemas above for the exact shape per category.

Canonical fetch pattern:

```bash
curl -s https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json | jq '.models[] | select(.model_id == "claude-opus-4-7")'
```

Resolving an alias (e.g. a Bedrock model ID):

```bash
curl -s https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json | jq '.models[] | select(.aliases // [] | index("anthropic.claude-opus-4-7-20250101-v1:0"))'
```

Price history for a row (changelog):

```bash
git log -p costs/llm.json | grep -A 5 '"model_id": "claude-opus-4-7"'
```

## Currency and units

- All prices are in USD (`currency: "USD"` implicit).
- LLM prices are per 1M tokens (`*_per_mtok_usd`).
- STT prices are per minute of audio (`price_per_minute_usd`).
- TTS prices are per 1M characters of input (`price_per_1m_chars_usd`).
- Prices are stored as decimal strings — `"0.0048"`, not `0.0048` — to avoid float roundtrip errors. Parse with `Decimal` (Python), `decimal.js` or `big.js` (JS), or any fixed-point library — do not use native `Number` or `parseFloat`.

## License

This dataset is licensed [CC-BY-4.0](./LICENSE) — reuse it freely with attribution. Source code in the rest of the repository is AGPLv3; this carve-out applies only to `costs/`.

## Citation

```
Hail Costs Dataset v0.X.Y, https://hail.so/costs, accessed YYYY-MM-DD
```

Release tags follow `costs-v0.X.Y`. Pin a version if you depend on a stable shape.

## How to contribute a cost update

1. Fork the repo or use GitHub's web editor.
2. Edit the relevant file in this directory (e.g. `llm.json`).
3. Update `last_verified` to today's date (ISO `YYYY-MM-DD`) — when you last _checked_ the price.
4. If the price actually changed, also update `last_changed_at` to today's date. Otherwise leave it alone.
5. Set `verified_by` to your GitHub handle.
6. Set `verification_method` to `community-pr` for PR-driven updates (or `manual-confirmed` if you cross-checked the price against the vendor's pricing page yourself with a second source).
7. Update `source_url` if it has changed.
8. **Two-source rule**: any price change must include two independent links in the PR description (vendor pricing page + a secondary source — vendor announcement, third-party aggregator, doc page). If a second source is genuinely unavailable, set `confidence: medium` on the affected row and call it out in the PR.
9. Open a pull request. CI validates the schema and runs alias-uniqueness + `replaced_by_model_id` resolution checks.

## Refresh cadence

A scheduled GitHub Action runs every Monday and opens an issue listing rows whose `last_verified` is more than 30 days old. Anyone can pick a row off that list, verify the price against the provider's published pricing page, and PR the update.

## Adding a new provider or model

Add a new object to the `models` array of the relevant data file. The schema rejects unknown fields, missing required fields, and prices below zero. Run validation locally before pushing:

```bash
pnpm costs:validate
```

## Adding a new category

If you need a category beyond LLM/STT/TTS (e.g. embedding models, image generation), open an issue first to agree on the shape — once a schema ships and consumers depend on it, breaking changes are expensive.
