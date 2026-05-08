# Model Costs

Public, validated cost and capability data for AI model providers — LLMs, speech-to-text, and text-to-speech.

The JSON files in this directory are the source of truth. Each row is validated against a JSON Schema in [`schema/`](./schema/) on every pull request.

## Canonical URLs

Programmatic consumers (agents, scripts, dashboards) should fetch directly:

- LLMs: <https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json>
- STT: <https://raw.githubusercontent.com/hail-hq/hail/main/costs/stt.json>
- TTS: <https://raw.githubusercontent.com/hail-hq/hail/main/costs/tts.json>

Schemas live alongside the data and are versioned with `version: 1` at the top of each file.

## License

This dataset is licensed [CC-BY-4.0](./LICENSE) — reuse it freely with attribution. Source code in the rest of the repository is AGPLv3; this carve-out applies only to `costs/`.

## How to contribute a cost update

1. Fork the repo or use GitHub's web editor.
2. Edit the relevant file in this directory (e.g. `llm.json`).
3. Bump `last_verified` to today's date (ISO `YYYY-MM-DD`) and set `verified_by` to your GitHub handle.
4. Update `source_url` if it has changed.
5. Open a pull request. CI validates the schema automatically.

## Refresh cadence

A scheduled GitHub Action runs every Monday and opens an issue listing rows whose `last_verified` is more than 30 days old. Anyone can pick a row off that list, verify the price against the provider's published pricing page, and PR the update.

## Adding a new provider or model

Add a new object to the `models` array of the relevant data file. The schema rejects unknown fields, missing required fields, and prices below zero. Run validation locally before pushing:

```bash
pnpm costs:validate
```

## Adding a new category

If you need a category beyond LLM/STT/TTS (e.g. embedding models, image generation), open an issue first to agree on the shape — once a schema ships and consumers depend on it, breaking changes are expensive.
