# Costs Dataset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish validated, agent-accessible JSON cost and capability data for LLM, STT, and TTS providers in `costs/`, dual-licensed under CC-BY-4.0 so external tools can embed it without AGPL friction.

**Architecture:** JSON files are the source of truth; a JSON Schema (Draft 2020-12) per category enforces shape. Each row carries `last_verified` (ISO date), `verified_by` (GitHub handle), and `source_url` for accountability. CI validates on every PR touching `costs/**`. A weekly cron opens an issue listing rows whose `last_verified` is older than 30 days. No code consumes this data yet — that lands in the docs-site and costs-UI plans. This plan ships the dataset and its guardrails standalone.

**Tech Stack:** JSON Schema Draft 2020-12, [`check-jsonschema`](https://github.com/python-jsonschema/check-jsonschema) for validation (Python, already available since the repo uses uv/Python heavily), Node ≥20 for the stale-check script (matches the existing pnpm tooling), GitHub Actions for CI.

---

## File Structure

**Created:**

- `costs/LICENSE` — CC-BY-4.0 carve-out for the data and schemas in this directory
- `costs/README.md` — entry point: what this is, contribution flow, canonical URLs
- `costs/schema/llm.schema.json` — JSON Schema for LLM costs and capabilities
- `costs/schema/stt.schema.json` — JSON Schema for STT costs and capabilities
- `costs/schema/tts.schema.json` — JSON Schema for TTS costs and capabilities
- `costs/llm.json` — LLM cost rows
- `costs/stt.json` — STT cost rows
- `costs/tts.json` — TTS cost rows
- `scripts/costs/check-stale.mjs` — finds rows where `last_verified` > 30 days old
- `scripts/costs/check-stale.test.mjs` — Node test runner unit tests for the script
- `.github/workflows/costs-validate.yml` — runs `check-jsonschema` on PRs touching `costs/**`
- `.github/workflows/costs-stale.yml` — weekly cron; opens an issue listing stale rows

**Modified:**

- `docs/contributing.md` — adds a "Model costs contributions" section
- `package.json` (root) — adds a `costs:validate` and `costs:stale` script for local use

**Boundaries:** schema files describe shape; data files contain rows; scripts contain logic; workflows automate CI. No file mixes concerns. The dataset is consumed only by future plans (docs site, costs UI) — keep this plan's scope to the data and its guardrails.

---

## Task 1: Scaffold `costs/` with LICENSE and README

**Files:**

- Create: `costs/LICENSE`
- Create: `costs/README.md`

- [ ] **Step 1: Create the directory and add the CC-BY-4.0 license text**

The official Creative Commons CC-BY-4.0 license text is at <https://creativecommons.org/licenses/by/4.0/legalcode.txt>. Fetch and save verbatim:

```bash
mkdir -p costs
curl -fsSL https://creativecommons.org/licenses/by/4.0/legalcode.txt > costs/LICENSE
```

Prepend a short scope header to the file. Open `costs/LICENSE` and add these three lines at the very top (above the `Creative Commons Attribution 4.0 International` heading):

```
SPDX-License-Identifier: CC-BY-4.0
Scope: this license applies to the JSON data files and JSON Schemas in this directory only. Code elsewhere in the Hail repository is licensed AGPLv3.
Attribution: when reusing, credit "Hail (https://hail.so)" and link back to https://github.com/hail-hq/hail/tree/main/costs.

```

- [ ] **Step 2: Verify the file is well-formed**

```bash
head -5 costs/LICENSE
wc -l costs/LICENSE
```

Expected: first line is `SPDX-License-Identifier: CC-BY-4.0`; total length 100+ lines (the official text is ~150 lines).

- [ ] **Step 3: Write `costs/README.md`**

Create `costs/README.md` with this content:

```markdown
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

\`\`\`bash
pnpm costs:validate
\`\`\`

## Adding a new category

If you need a category beyond LLM/STT/TTS (e.g. embedding models, image generation), open an issue first to agree on the shape — once a schema ships and consumers depend on it, breaking changes are expensive.
```

- [ ] **Step 4: Commit**

```bash
git add costs/LICENSE costs/README.md
git commit -m "docs(costs): scaffold costs/ directory with CC-BY-4.0 license and README"
```

---

## Task 2: Write the LLM JSON Schema

**Files:**

- Create: `costs/schema/llm.schema.json`

The schema goes first; data follows. We test the schema by validating a tiny inline fixture pair (one valid, one invalid) before populating the full data file.

- [ ] **Step 1: Write a failing test fixture**

Create a temporary file `/tmp/llm-fixture-valid.json`:

```json
{
  "version": 1,
  "updated": "2026-05-05",
  "license": "CC-BY-4.0",
  "models": [
    {
      "provider": "TestCo",
      "model_id": "test-model-1",
      "display_name": "Test Model 1",
      "context_window": 128000,
      "max_output_tokens": 8192,
      "input_per_mtok_usd": 1.0,
      "output_per_mtok_usd": 3.0,
      "modalities": { "input": ["text"], "output": ["text"] },
      "tool_use": true,
      "structured_output": true,
      "last_verified": "2026-05-05",
      "verified_by": "redouane-achouri",
      "source_url": "https://example.com/pricing"
    }
  ]
}
```

And `/tmp/llm-fixture-invalid.json` (missing `last_verified`):

```json
{
  "version": 1,
  "updated": "2026-05-05",
  "license": "CC-BY-4.0",
  "models": [
    {
      "provider": "TestCo",
      "model_id": "test-model-1",
      "display_name": "Test Model 1",
      "context_window": 128000,
      "max_output_tokens": 8192,
      "input_per_mtok_usd": 1.0,
      "output_per_mtok_usd": 3.0,
      "modalities": { "input": ["text"], "output": ["text"] },
      "tool_use": true,
      "structured_output": true,
      "verified_by": "redouane-achouri",
      "source_url": "https://example.com/pricing"
    }
  ]
}
```

- [ ] **Step 2: Run validation against the (not-yet-existing) schema and verify it fails**

```bash
mkdir -p costs/schema
pipx run check-jsonschema --schemafile costs/schema/llm.schema.json /tmp/llm-fixture-valid.json
```

Expected: error — schema file does not exist.

- [ ] **Step 3: Write `costs/schema/llm.schema.json`**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://hail.so/costs/schema/llm.json",
  "title": "LLM Costs",
  "description": "Cost and capability data for large language model providers.",
  "type": "object",
  "required": ["version", "updated", "license", "models"],
  "additionalProperties": false,
  "properties": {
    "version": { "const": 1 },
    "updated": { "type": "string", "format": "date" },
    "license": { "const": "CC-BY-4.0" },
    "models": {
      "type": "array",
      "minItems": 1,
      "items": { "$ref": "#/$defs/model" }
    }
  },
  "$defs": {
    "model": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "provider",
        "model_id",
        "display_name",
        "context_window",
        "max_output_tokens",
        "input_per_mtok_usd",
        "output_per_mtok_usd",
        "modalities",
        "last_verified",
        "verified_by",
        "source_url"
      ],
      "properties": {
        "provider": { "type": "string", "minLength": 1 },
        "provider_url": { "type": "string", "format": "uri" },
        "model_id": { "type": "string", "minLength": 1 },
        "display_name": { "type": "string", "minLength": 1 },
        "model_family": { "type": "string" },
        "release_date": { "type": "string", "format": "date" },
        "knowledge_cutoff": { "type": "string", "format": "date" },
        "context_window": { "type": "integer", "minimum": 1 },
        "max_output_tokens": { "type": "integer", "minimum": 1 },
        "input_per_mtok_usd": { "type": "number", "minimum": 0 },
        "output_per_mtok_usd": { "type": "number", "minimum": 0 },
        "cached_input_per_mtok_usd": { "type": "number", "minimum": 0 },
        "modalities": {
          "type": "object",
          "additionalProperties": false,
          "required": ["input", "output"],
          "properties": {
            "input": {
              "type": "array",
              "minItems": 1,
              "uniqueItems": true,
              "items": { "enum": ["text", "image", "audio", "video"] }
            },
            "output": {
              "type": "array",
              "minItems": 1,
              "uniqueItems": true,
              "items": { "enum": ["text", "image", "audio"] }
            }
          }
        },
        "tool_use": { "type": "boolean" },
        "structured_output": { "type": "boolean" },
        "last_verified": { "type": "string", "format": "date" },
        "verified_by": {
          "type": "string",
          "pattern": "^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$"
        },
        "source_url": { "type": "string", "format": "uri" },
        "notes": { "type": "string" }
      }
    }
  }
}
```

- [ ] **Step 4: Run validation against the valid fixture and verify it passes**

```bash
pipx run check-jsonschema --schemafile costs/schema/llm.schema.json /tmp/llm-fixture-valid.json
```

Expected: `ok -- validation done`.

- [ ] **Step 5: Run validation against the invalid fixture and verify it fails**

```bash
pipx run check-jsonschema --schemafile costs/schema/llm.schema.json /tmp/llm-fixture-invalid.json
```

Expected: error mentioning `'last_verified' is a required property`.

- [ ] **Step 6: Clean up fixtures and commit**

```bash
rm /tmp/llm-fixture-valid.json /tmp/llm-fixture-invalid.json
git add costs/schema/llm.schema.json
git commit -m "feat(costs): add JSON Schema for LLM costs"
```

---

## Task 3: Add LLM seed data (5 providers)

**Files:**

- Create: `costs/llm.json`

Populate with 5 widely-used providers. **Verify every price against the provider's official pricing page on the day you commit this task.** Prices change frequently; do not trust the values in this plan as authoritative — they are template values to show structure.

- [ ] **Step 1: Open each provider pricing page and record current prices**

Open these URLs in a browser, record `input_per_mtok_usd`, `output_per_mtok_usd`, `cached_input_per_mtok_usd` (where applicable), `context_window`, and `max_output_tokens`:

- Anthropic: <https://www.anthropic.com/pricing>
- OpenAI: <https://openai.com/api/pricing/>
- Google (Gemini API): <https://ai.google.dev/pricing>
- Groq: <https://groq.com/pricing/>
- DeepSeek: <https://api-docs.deepseek.com/quick_start/pricing>

- [ ] **Step 2: Write `costs/llm.json`**

Use this structure. **Replace every numeric price with the value you just recorded** — the values below are illustrative only:

```json
{
  "version": 1,
  "updated": "2026-05-05",
  "license": "CC-BY-4.0",
  "models": [
    {
      "provider": "Anthropic",
      "provider_url": "https://www.anthropic.com",
      "model_id": "claude-opus-4-7",
      "display_name": "Claude Opus 4.7",
      "model_family": "Claude 4",
      "context_window": 1000000,
      "max_output_tokens": 32000,
      "input_per_mtok_usd": 15.0,
      "output_per_mtok_usd": 75.0,
      "cached_input_per_mtok_usd": 1.5,
      "modalities": { "input": ["text", "image"], "output": ["text"] },
      "tool_use": true,
      "structured_output": true,
      "last_verified": "2026-05-05",
      "verified_by": "<your-gh-handle>",
      "source_url": "https://www.anthropic.com/pricing"
    },
    {
      "provider": "Anthropic",
      "provider_url": "https://www.anthropic.com",
      "model_id": "claude-sonnet-4-6",
      "display_name": "Claude Sonnet 4.6",
      "model_family": "Claude 4",
      "context_window": 200000,
      "max_output_tokens": 8192,
      "input_per_mtok_usd": 3.0,
      "output_per_mtok_usd": 15.0,
      "cached_input_per_mtok_usd": 0.3,
      "modalities": { "input": ["text", "image"], "output": ["text"] },
      "tool_use": true,
      "structured_output": true,
      "last_verified": "2026-05-05",
      "verified_by": "<your-gh-handle>",
      "source_url": "https://www.anthropic.com/pricing"
    },
    {
      "provider": "OpenAI",
      "provider_url": "https://openai.com",
      "model_id": "gpt-5",
      "display_name": "GPT-5",
      "context_window": 400000,
      "max_output_tokens": 16384,
      "input_per_mtok_usd": 5.0,
      "output_per_mtok_usd": 20.0,
      "cached_input_per_mtok_usd": 0.5,
      "modalities": { "input": ["text", "image", "audio"], "output": ["text"] },
      "tool_use": true,
      "structured_output": true,
      "last_verified": "2026-05-05",
      "verified_by": "<your-gh-handle>",
      "source_url": "https://openai.com/api/pricing/"
    },
    {
      "provider": "Google",
      "provider_url": "https://ai.google.dev",
      "model_id": "gemini-2.5-pro",
      "display_name": "Gemini 2.5 Pro",
      "context_window": 2000000,
      "max_output_tokens": 8192,
      "input_per_mtok_usd": 1.25,
      "output_per_mtok_usd": 5.0,
      "cached_input_per_mtok_usd": 0.31,
      "modalities": {
        "input": ["text", "image", "audio", "video"],
        "output": ["text"]
      },
      "tool_use": true,
      "structured_output": true,
      "last_verified": "2026-05-05",
      "verified_by": "<your-gh-handle>",
      "source_url": "https://ai.google.dev/pricing"
    },
    {
      "provider": "DeepSeek",
      "provider_url": "https://platform.deepseek.com",
      "model_id": "deepseek-v3",
      "display_name": "DeepSeek V3",
      "context_window": 128000,
      "max_output_tokens": 8192,
      "input_per_mtok_usd": 0.27,
      "output_per_mtok_usd": 1.1,
      "cached_input_per_mtok_usd": 0.07,
      "modalities": { "input": ["text"], "output": ["text"] },
      "tool_use": true,
      "structured_output": true,
      "last_verified": "2026-05-05",
      "verified_by": "<your-gh-handle>",
      "source_url": "https://api-docs.deepseek.com/quick_start/pricing"
    }
  ]
}
```

Replace `<your-gh-handle>` with your actual GitHub username. Replace placeholder prices with what you recorded in Step 1.

- [ ] **Step 3: Validate the file passes**

```bash
pipx run check-jsonschema --schemafile costs/schema/llm.schema.json costs/llm.json
```

Expected: `ok -- validation done`. If it fails, read the error and fix the offending row.

- [ ] **Step 4: Sanity-check that prices are realistic**

A common mistake is putting input prices in the output column or vice versa. For each model, confirm `output_per_mtok_usd >= input_per_mtok_usd`. Run:

```bash
python3 -c "
import json
data = json.load(open('costs/llm.json'))
for m in data['models']:
    if m['output_per_mtok_usd'] < m['input_per_mtok_usd']:
        print(f\"WARN: {m['model_id']}: output < input — verify\")
print('Done.')
"
```

If any warnings print, double-check the provider's pricing page. (Some providers do price equally; others price output cheaper as a deliberate choice — but it's rare. The warning is a heuristic, not an error.)

- [ ] **Step 5: Commit**

```bash
git add costs/llm.json
git commit -m "feat(costs): add LLM seed data for 5 providers"
```

---

## Task 4: Write the STT JSON Schema

**Files:**

- Create: `costs/schema/stt.schema.json`

- [ ] **Step 1: Write a failing test fixture**

Create `/tmp/stt-fixture-valid.json`:

```json
{
  "version": 1,
  "updated": "2026-05-05",
  "license": "CC-BY-4.0",
  "models": [
    {
      "provider": "TestCo",
      "model_id": "test-stt",
      "display_name": "Test STT",
      "price_per_minute_usd": 0.01,
      "languages": "100+",
      "streaming": true,
      "realtime": true,
      "diarization": "included",
      "last_verified": "2026-05-05",
      "verified_by": "redouane-achouri",
      "source_url": "https://example.com/pricing"
    }
  ]
}
```

- [ ] **Step 2: Run validation against the (not-yet-existing) schema and verify it fails**

```bash
pipx run check-jsonschema --schemafile costs/schema/stt.schema.json /tmp/stt-fixture-valid.json
```

Expected: error — schema file does not exist.

- [ ] **Step 3: Write `costs/schema/stt.schema.json`**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://hail.so/costs/schema/stt.json",
  "title": "STT Costs",
  "description": "Cost and capability data for speech-to-text providers.",
  "type": "object",
  "required": ["version", "updated", "license", "models"],
  "additionalProperties": false,
  "properties": {
    "version": { "const": 1 },
    "updated": { "type": "string", "format": "date" },
    "license": { "const": "CC-BY-4.0" },
    "models": {
      "type": "array",
      "minItems": 1,
      "items": { "$ref": "#/$defs/model" }
    }
  },
  "$defs": {
    "model": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "provider",
        "model_id",
        "display_name",
        "price_per_minute_usd",
        "languages",
        "streaming",
        "last_verified",
        "verified_by",
        "source_url"
      ],
      "properties": {
        "provider": { "type": "string", "minLength": 1 },
        "provider_url": { "type": "string", "format": "uri" },
        "model_id": { "type": "string", "minLength": 1 },
        "display_name": { "type": "string", "minLength": 1 },
        "price_per_minute_usd": { "type": "number", "minimum": 0 },
        "price_per_minute_batch_usd": { "type": "number", "minimum": 0 },
        "languages": {
          "oneOf": [
            { "type": "array", "items": { "type": "string" }, "minItems": 1 },
            { "type": "string", "pattern": "^[0-9]+\\+$" }
          ]
        },
        "streaming": { "type": "boolean" },
        "realtime": { "type": "boolean" },
        "diarization": {
          "enum": ["included", "extra-cost", "unsupported"]
        },
        "wer_benchmark": {
          "type": "object",
          "additionalProperties": false,
          "required": ["dataset", "wer_pct"],
          "properties": {
            "dataset": { "type": "string" },
            "wer_pct": { "type": "number", "minimum": 0, "maximum": 100 },
            "source_url": { "type": "string", "format": "uri" }
          }
        },
        "time_to_first_word_ms": { "type": "integer", "minimum": 0 },
        "last_verified": { "type": "string", "format": "date" },
        "verified_by": {
          "type": "string",
          "pattern": "^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$"
        },
        "source_url": { "type": "string", "format": "uri" },
        "notes": { "type": "string" }
      }
    }
  }
}
```

- [ ] **Step 4: Run validation against the valid fixture and verify it passes**

```bash
pipx run check-jsonschema --schemafile costs/schema/stt.schema.json /tmp/stt-fixture-valid.json
```

Expected: `ok -- validation done`.

- [ ] **Step 5: Build an invalid fixture and verify rejection**

Create `/tmp/stt-fixture-invalid.json` (negative price):

```json
{
  "version": 1,
  "updated": "2026-05-05",
  "license": "CC-BY-4.0",
  "models": [
    {
      "provider": "TestCo",
      "model_id": "test-stt",
      "display_name": "Test STT",
      "price_per_minute_usd": -0.01,
      "languages": "100+",
      "streaming": true,
      "last_verified": "2026-05-05",
      "verified_by": "redouane-achouri",
      "source_url": "https://example.com/pricing"
    }
  ]
}
```

```bash
pipx run check-jsonschema --schemafile costs/schema/stt.schema.json /tmp/stt-fixture-invalid.json
```

Expected: error mentioning minimum 0 violation on `price_per_minute_usd`.

- [ ] **Step 6: Clean up and commit**

```bash
rm /tmp/stt-fixture-valid.json /tmp/stt-fixture-invalid.json
git add costs/schema/stt.schema.json
git commit -m "feat(costs): add JSON Schema for STT costs"
```

---

## Task 5: Add STT seed data (3 providers)

**Files:**

- Create: `costs/stt.json`

- [ ] **Step 1: Verify current pricing for each provider**

Open each pricing page and record `price_per_minute_usd`, language coverage, streaming/realtime support, and diarization handling:

- Deepgram: <https://deepgram.com/pricing>
- AssemblyAI: <https://www.assemblyai.com/pricing>
- OpenAI Whisper API: <https://openai.com/api/pricing/>

- [ ] **Step 2: Write `costs/stt.json`**

Replace placeholder values with the prices you just recorded:

```json
{
  "version": 1,
  "updated": "2026-05-05",
  "license": "CC-BY-4.0",
  "models": [
    {
      "provider": "Deepgram",
      "provider_url": "https://deepgram.com",
      "model_id": "nova-3",
      "display_name": "Deepgram Nova-3",
      "price_per_minute_usd": 0.0043,
      "price_per_minute_batch_usd": 0.0036,
      "languages": "40+",
      "streaming": true,
      "realtime": true,
      "diarization": "included",
      "time_to_first_word_ms": 300,
      "last_verified": "2026-05-05",
      "verified_by": "<your-gh-handle>",
      "source_url": "https://deepgram.com/pricing"
    },
    {
      "provider": "AssemblyAI",
      "provider_url": "https://www.assemblyai.com",
      "model_id": "universal-2",
      "display_name": "Universal-2",
      "price_per_minute_usd": 0.0067,
      "languages": "99+",
      "streaming": true,
      "realtime": true,
      "diarization": "included",
      "last_verified": "2026-05-05",
      "verified_by": "<your-gh-handle>",
      "source_url": "https://www.assemblyai.com/pricing"
    },
    {
      "provider": "OpenAI",
      "provider_url": "https://openai.com",
      "model_id": "whisper-1",
      "display_name": "Whisper",
      "price_per_minute_usd": 0.006,
      "languages": "100+",
      "streaming": false,
      "realtime": false,
      "diarization": "unsupported",
      "last_verified": "2026-05-05",
      "verified_by": "<your-gh-handle>",
      "source_url": "https://openai.com/api/pricing/"
    }
  ]
}
```

- [ ] **Step 3: Validate**

```bash
pipx run check-jsonschema --schemafile costs/schema/stt.schema.json costs/stt.json
```

Expected: `ok -- validation done`.

- [ ] **Step 4: Commit**

```bash
git add costs/stt.json
git commit -m "feat(costs): add STT seed data for 3 providers"
```

---

## Task 6: Write the TTS JSON Schema

**Files:**

- Create: `costs/schema/tts.schema.json`

- [ ] **Step 1: Write a valid fixture**

Create `/tmp/tts-fixture-valid.json`:

```json
{
  "version": 1,
  "updated": "2026-05-05",
  "license": "CC-BY-4.0",
  "models": [
    {
      "provider": "TestCo",
      "model_id": "test-tts",
      "display_name": "Test TTS",
      "price_per_1m_chars_usd": 5.0,
      "voice_quality": "neural",
      "voice_count": 30,
      "languages": ["en", "fr"],
      "ssml_support": true,
      "voice_cloning": false,
      "output_formats": ["mp3", "wav", "opus"],
      "last_verified": "2026-05-05",
      "verified_by": "redouane-achouri",
      "source_url": "https://example.com/pricing"
    }
  ]
}
```

- [ ] **Step 2: Verify validation fails (schema doesn't exist yet)**

```bash
pipx run check-jsonschema --schemafile costs/schema/tts.schema.json /tmp/tts-fixture-valid.json
```

Expected: error — schema file does not exist.

- [ ] **Step 3: Write `costs/schema/tts.schema.json`**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://hail.so/costs/schema/tts.json",
  "title": "TTS Costs",
  "description": "Cost and capability data for text-to-speech providers.",
  "type": "object",
  "required": ["version", "updated", "license", "models"],
  "additionalProperties": false,
  "properties": {
    "version": { "const": 1 },
    "updated": { "type": "string", "format": "date" },
    "license": { "const": "CC-BY-4.0" },
    "models": {
      "type": "array",
      "minItems": 1,
      "items": { "$ref": "#/$defs/model" }
    }
  },
  "$defs": {
    "model": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "provider",
        "model_id",
        "display_name",
        "price_per_1m_chars_usd",
        "voice_quality",
        "languages",
        "last_verified",
        "verified_by",
        "source_url"
      ],
      "properties": {
        "provider": { "type": "string", "minLength": 1 },
        "provider_url": { "type": "string", "format": "uri" },
        "model_id": { "type": "string", "minLength": 1 },
        "display_name": { "type": "string", "minLength": 1 },
        "price_per_1m_chars_usd": { "type": "number", "minimum": 0 },
        "voice_quality": { "enum": ["standard", "neural", "cloned"] },
        "voice_count": { "type": "integer", "minimum": 1 },
        "languages": {
          "oneOf": [
            { "type": "array", "items": { "type": "string" }, "minItems": 1 },
            { "type": "string", "pattern": "^[0-9]+\\+$" }
          ]
        },
        "ssml_support": { "type": "boolean" },
        "voice_cloning": {
          "oneOf": [
            { "type": "boolean" },
            {
              "type": "object",
              "additionalProperties": false,
              "required": ["price_usd", "unit"],
              "properties": {
                "price_usd": { "type": "number", "minimum": 0 },
                "unit": { "enum": ["per-clone", "monthly", "per-1m-chars"] }
              }
            }
          ]
        },
        "output_formats": {
          "type": "array",
          "minItems": 1,
          "uniqueItems": true,
          "items": { "type": "string" }
        },
        "time_to_first_byte_ms": { "type": "integer", "minimum": 0 },
        "last_verified": { "type": "string", "format": "date" },
        "verified_by": {
          "type": "string",
          "pattern": "^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$"
        },
        "source_url": { "type": "string", "format": "uri" },
        "notes": { "type": "string" }
      }
    }
  }
}
```

- [ ] **Step 4: Validate**

```bash
pipx run check-jsonschema --schemafile costs/schema/tts.schema.json /tmp/tts-fixture-valid.json
```

Expected: `ok -- validation done`.

- [ ] **Step 5: Test invalid fixture (unknown voice_quality)**

Create `/tmp/tts-fixture-invalid.json`:

```json
{
  "version": 1,
  "updated": "2026-05-05",
  "license": "CC-BY-4.0",
  "models": [
    {
      "provider": "TestCo",
      "model_id": "test-tts",
      "display_name": "Test TTS",
      "price_per_1m_chars_usd": 5.0,
      "voice_quality": "ultra",
      "languages": ["en"],
      "last_verified": "2026-05-05",
      "verified_by": "redouane-achouri",
      "source_url": "https://example.com/pricing"
    }
  ]
}
```

```bash
pipx run check-jsonschema --schemafile costs/schema/tts.schema.json /tmp/tts-fixture-invalid.json
```

Expected: error mentioning `'ultra' is not one of ['standard', 'neural', 'cloned']`.

- [ ] **Step 6: Clean up and commit**

```bash
rm /tmp/tts-fixture-valid.json /tmp/tts-fixture-invalid.json
git add costs/schema/tts.schema.json
git commit -m "feat(costs): add JSON Schema for TTS costs"
```

---

## Task 7: Add TTS seed data (3 providers)

**Files:**

- Create: `costs/tts.json`

- [ ] **Step 1: Verify current pricing for each provider**

Record current prices, voice quality tier, language coverage, voice cloning availability:

- ElevenLabs: <https://elevenlabs.io/pricing>
- OpenAI TTS: <https://openai.com/api/pricing/>
- Cartesia: <https://cartesia.ai/pricing>

- [ ] **Step 2: Write `costs/tts.json`**

```json
{
  "version": 1,
  "updated": "2026-05-05",
  "license": "CC-BY-4.0",
  "models": [
    {
      "provider": "ElevenLabs",
      "provider_url": "https://elevenlabs.io",
      "model_id": "eleven_turbo_v2_5",
      "display_name": "Eleven Turbo v2.5",
      "price_per_1m_chars_usd": 50.0,
      "voice_quality": "neural",
      "voice_count": 5000,
      "languages": "32+",
      "ssml_support": false,
      "voice_cloning": { "price_usd": 0, "unit": "per-clone" },
      "output_formats": ["mp3", "pcm", "ulaw"],
      "time_to_first_byte_ms": 250,
      "last_verified": "2026-05-05",
      "verified_by": "<your-gh-handle>",
      "source_url": "https://elevenlabs.io/pricing"
    },
    {
      "provider": "OpenAI",
      "provider_url": "https://openai.com",
      "model_id": "tts-1-hd",
      "display_name": "TTS-1 HD",
      "price_per_1m_chars_usd": 30.0,
      "voice_quality": "neural",
      "voice_count": 6,
      "languages": "57+",
      "ssml_support": false,
      "voice_cloning": false,
      "output_formats": ["mp3", "opus", "aac", "flac", "wav", "pcm"],
      "last_verified": "2026-05-05",
      "verified_by": "<your-gh-handle>",
      "source_url": "https://openai.com/api/pricing/"
    },
    {
      "provider": "Cartesia",
      "provider_url": "https://cartesia.ai",
      "model_id": "sonic-2",
      "display_name": "Sonic 2",
      "price_per_1m_chars_usd": 65.0,
      "voice_quality": "neural",
      "languages": "15+",
      "ssml_support": false,
      "voice_cloning": { "price_usd": 0, "unit": "per-clone" },
      "output_formats": ["mp3", "wav", "pcm", "ulaw"],
      "time_to_first_byte_ms": 90,
      "last_verified": "2026-05-05",
      "verified_by": "<your-gh-handle>",
      "source_url": "https://cartesia.ai/pricing"
    }
  ]
}
```

- [ ] **Step 3: Validate**

```bash
pipx run check-jsonschema --schemafile costs/schema/tts.schema.json costs/tts.json
```

Expected: `ok -- validation done`.

- [ ] **Step 4: Commit**

```bash
git add costs/tts.json
git commit -m "feat(costs): add TTS seed data for 3 providers"
```

---

## Task 8: GitHub Action — schema validation on PR

**Files:**

- Create: `.github/workflows/costs-validate.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: costs-validate

on:
  pull_request:
    paths:
      - "costs/**"
      - ".github/workflows/costs-validate.yml"

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install check-jsonschema
        run: pip install check-jsonschema==0.29.4
      - name: Validate LLM data
        run: check-jsonschema --schemafile costs/schema/llm.schema.json costs/llm.json
      - name: Validate STT data
        run: check-jsonschema --schemafile costs/schema/stt.schema.json costs/stt.json
      - name: Validate TTS data
        run: check-jsonschema --schemafile costs/schema/tts.schema.json costs/tts.json
```

- [ ] **Step 2: Lint the workflow YAML**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/costs-validate.yml'))"
```

Expected: no output (valid YAML).

- [ ] **Step 3: Run the same commands locally to confirm they pass**

```bash
pip install check-jsonschema==0.29.4
check-jsonschema --schemafile costs/schema/llm.schema.json costs/llm.json
check-jsonschema --schemafile costs/schema/stt.schema.json costs/stt.json
check-jsonschema --schemafile costs/schema/tts.schema.json costs/tts.json
```

Expected: three `ok -- validation done` lines.

- [ ] **Step 4: Smoke-test failure path**

Temporarily break a file and confirm the local command fails:

```bash
python3 -c "
import json
d = json.load(open('costs/llm.json'))
d['models'][0]['input_per_mtok_usd'] = -1
json.dump(d, open('/tmp/llm-broken.json', 'w'))
"
check-jsonschema --schemafile costs/schema/llm.schema.json /tmp/llm-broken.json
echo "exit: $?"
```

Expected: error output and non-zero exit code.

```bash
rm /tmp/llm-broken.json
```

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/costs-validate.yml
git commit -m "ci(costs): validate JSON Schema on PRs touching costs/"
```

---

## Task 9: Stale-row detection script

**Files:**

- Create: `scripts/costs/check-stale.mjs`
- Create: `scripts/costs/check-stale.test.mjs`

The script reads all `costs/*.json` files and prints rows whose `last_verified` is more than `--max-age` days old (default 30). Exit code 0 if none stale, 1 if any stale (so CI can branch on it).

- [ ] **Step 1: Write the failing test**

```javascript
// scripts/costs/check-stale.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { findStale } from "./check-stale.mjs";

const today = new Date("2026-05-05T00:00:00Z");

test("returns empty when all rows are fresh", () => {
  const rows = [
    { model_id: "a", last_verified: "2026-05-01" },
    { model_id: "b", last_verified: "2026-04-20" },
  ];
  assert.deepEqual(findStale(rows, 30, today), []);
});

test("returns rows older than max age", () => {
  const rows = [
    { model_id: "fresh", last_verified: "2026-05-01" },
    { model_id: "stale", last_verified: "2026-03-01" },
  ];
  const stale = findStale(rows, 30, today);
  assert.equal(stale.length, 1);
  assert.equal(stale[0].model_id, "stale");
});

test("boundary: exactly maxAge days old is not stale", () => {
  const rows = [{ model_id: "edge", last_verified: "2026-04-05" }]; // 30 days
  assert.deepEqual(findStale(rows, 30, today), []);
});

test("boundary: maxAge + 1 days old is stale", () => {
  const rows = [{ model_id: "edge", last_verified: "2026-04-04" }]; // 31 days
  const stale = findStale(rows, 30, today);
  assert.equal(stale.length, 1);
});
```

- [ ] **Step 2: Run the test and confirm it fails**

```bash
mkdir -p scripts/costs
node --test scripts/costs/check-stale.test.mjs
```

Expected: failure — `Cannot find module './check-stale.mjs'`.

- [ ] **Step 3: Write the script**

```javascript
// scripts/costs/check-stale.mjs
import { readFile, readdir } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, "..", "..");
const DATA_DIR = join(REPO_ROOT, "costs");

const MS_PER_DAY = 1000 * 60 * 60 * 24;

export function findStale(rows, maxAgeDays, now = new Date()) {
  const cutoffMs = now.getTime() - maxAgeDays * MS_PER_DAY;
  return rows.filter((row) => {
    const verifiedMs = new Date(row.last_verified + "T00:00:00Z").getTime();
    return verifiedMs < cutoffMs;
  });
}

async function main() {
  const args = process.argv.slice(2);
  const maxAgeIdx = args.indexOf("--max-age");
  const maxAge = maxAgeIdx >= 0 ? Number(args[maxAgeIdx + 1]) : 30;

  const files = (await readdir(DATA_DIR)).filter((f) => f.endsWith(".json"));
  const results = [];

  for (const file of files) {
    const category = file.replace(/\.json$/, "");
    const data = JSON.parse(await readFile(join(DATA_DIR, file), "utf-8"));
    const stale = findStale(data.models, maxAge);
    for (const row of stale) {
      results.push({ category, ...row });
    }
  }

  if (results.length === 0) {
    console.log(`No stale rows (max age: ${maxAge} days).`);
    process.exit(0);
  }

  console.log(
    `Found ${results.length} stale row(s) (max age: ${maxAge} days):\n`,
  );
  for (const row of results) {
    const days = Math.floor(
      (Date.now() - new Date(row.last_verified + "T00:00:00Z").getTime()) /
        MS_PER_DAY,
    );
    console.log(
      `- [${row.category}] ${row.provider} / ${row.model_id} — last verified ${row.last_verified} (${days} days ago)`,
    );
  }
  process.exit(1);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    console.error(err);
    process.exit(2);
  });
}
```

- [ ] **Step 4: Run the test and confirm it passes**

```bash
node --test scripts/costs/check-stale.test.mjs
```

Expected: 4 passing tests.

- [ ] **Step 5: Run the script against real data**

```bash
node scripts/costs/check-stale.mjs --max-age 30
echo "exit: $?"
```

Expected: `No stale rows (max age: 30 days).` and exit 0 (since you just verified all rows today).

- [ ] **Step 6: Smoke-test the failing branch**

```bash
node scripts/costs/check-stale.mjs --max-age 0
echo "exit: $?"
```

Expected: lists every row as stale and exits with code 1.

- [ ] **Step 7: Add `costs:validate` and `costs:stale` scripts to root `package.json`**

Read the current `package.json`:

```bash
cat package.json
```

Add these two entries to the `scripts` block (preserve existing entries):

```json
"costs:validate": "pipx run check-jsonschema --schemafile costs/schema/llm.schema.json costs/llm.json && pipx run check-jsonschema --schemafile costs/schema/stt.schema.json costs/stt.json && pipx run check-jsonschema --schemafile costs/schema/tts.schema.json costs/tts.json",
"costs:stale": "node scripts/costs/check-stale.mjs --max-age 30"
```

Verify both scripts run:

```bash
pnpm costs:validate
pnpm costs:stale
```

Expected: validation passes, stale check reports no stale rows.

- [ ] **Step 8: Commit**

```bash
git add scripts/costs/check-stale.mjs scripts/costs/check-stale.test.mjs package.json
git commit -m "feat(costs): add stale-row detection script and pnpm aliases"
```

---

## Task 10: GitHub Action — weekly stale-row issue

**Files:**

- Create: `.github/workflows/costs-stale.yml`

The workflow runs every Monday at 09:00 UTC and opens (or updates) a single tracking issue listing all rows older than 30 days.

- [ ] **Step 1: Write the workflow**

````yaml
name: costs-stale

on:
  schedule:
    # Every Monday at 09:00 UTC
    - cron: "0 9 * * 1"
  workflow_dispatch: {}

permissions:
  contents: read
  issues: write

jobs:
  stale:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - name: Run stale check
        id: stale
        run: |
          set +e
          node scripts/costs/check-stale.mjs --max-age 30 > /tmp/stale-report.txt
          echo "exit=$?" >> "$GITHUB_OUTPUT"
      - name: Open or update tracking issue
        if: steps.stale.outputs.exit == '1'
        uses: actions/github-script@v7
        env:
          REPORT_PATH: /tmp/stale-report.txt
        with:
          script: |
            const fs = require('node:fs');
            const report = fs.readFileSync(process.env.REPORT_PATH, 'utf-8');
            const title = 'Model costs: stale rows';
            const body = [
              'The weekly costs-stale workflow found rows whose `last_verified` is older than 30 days.',
              '',
              'Pick one off this list, verify the price against the provider\'s pricing page, and open a PR bumping `last_verified` (and any changed fields).',
              '',
              '```',
              report,
              '```',
              '',
              `_Last updated: ${new Date().toISOString().slice(0, 10)}._`,
            ].join('\n');

            const { data: existing } = await github.rest.issues.listForRepo({
              owner: context.repo.owner,
              repo: context.repo.repo,
              state: 'open',
              labels: 'costs-stale',
            });

            if (existing.length > 0) {
              await github.rest.issues.update({
                owner: context.repo.owner,
                repo: context.repo.repo,
                issue_number: existing[0].number,
                body,
              });
            } else {
              await github.rest.issues.create({
                owner: context.repo.owner,
                repo: context.repo.repo,
                title,
                body,
                labels: ['costs-stale'],
              });
            }
````

- [ ] **Step 2: Lint the YAML**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/costs-stale.yml'))"
```

Expected: no output.

- [ ] **Step 3: Confirm the script's exit-code contract matches what the workflow expects**

The workflow branches on `exit == '1'` (rows are stale). The script must exit 1 when stale rows exist and 0 otherwise. Verify by hand:

```bash
node scripts/costs/check-stale.mjs --max-age 30; echo "fresh exit: $?"
node scripts/costs/check-stale.mjs --max-age 0;  echo "stale exit: $?"
```

Expected: `fresh exit: 0` and `stale exit: 1`.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/costs-stale.yml
git commit -m "ci(costs): weekly cron opens tracking issue for stale rows"
```

- [ ] **Step 5: Manual smoke test after merge (post-execution note)**

After this lands on `main`, trigger the workflow manually from the Actions tab via `workflow_dispatch` with `--max-age 0` to confirm it can open an issue. Close the resulting test issue afterward. Document this in the PR description so reviewers can verify.

---

## Task 11: Document the costs contribution flow in `docs/contributing.md`

**Files:**

- Modify: `docs/contributing.md`

- [ ] **Step 1: Read the current file to find insertion point**

```bash
grep -n "## " docs/contributing.md
```

Expected: a list of section headings. Insert the new section after `## Adding a provider` and before `## What we won't merge (v1)`.

- [ ] **Step 2: Add the new section**

Open `docs/contributing.md`. After the line ending the `## Adding a provider` section (just before `## What we won't merge (v1)`), insert:

```markdown
## Model costs contributions

Public AI model costs live in [`costs/`](../costs/) under CC-BY-4.0. JSON files at the top of that directory are the source of truth and are validated against schemas in `costs/schema/` on every PR.

To update a price:

1. Edit `costs/<category>.json` (e.g. `costs/llm.json`).
2. Bump `last_verified` to today (`YYYY-MM-DD`) and set `verified_by` to your GitHub handle.
3. Update `source_url` if it has changed.
4. Run `pnpm costs:validate` locally before pushing.

A weekly cron opens a tracking issue listing rows older than 30 days — see [`.github/workflows/costs-stale.yml`](../.github/workflows/costs-stale.yml).
```

- [ ] **Step 3: Verify Markdown renders cleanly**

```bash
grep -A 3 "## Model costs contributions" docs/contributing.md
```

Expected: the new section heading and its first lines.

- [ ] **Step 4: Commit**

```bash
git add docs/contributing.md
git commit -m "docs(contributing): document model costs contribution flow"
```

---

## Wrap-up

After all 11 tasks land:

- `costs/` is publicly readable under CC-BY-4.0
- All three datasets are validated on every PR
- A weekly cron surfaces stale rows for human attention
- `pnpm costs:validate` and `pnpm costs:stale` work locally
- Canonical raw URLs (e.g. `https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json`) are stable and documented in `costs/README.md`

The dataset is consumed by the docs site and costs UI in subsequent plans (`2026-05-05-docs-site-bootstrap.md` and `2026-05-05-costs-ui.md`, written next).
