# LiteLLM upstream PR — preparation guide

## Why

[LiteLLM](https://github.com/BerriAI/litellm) is a Python SDK / proxy that unifies many model providers behind one interface. It includes an internal pricing database for cost estimation (`litellm.model_cost`, sourced from `model_prices_and_context_window.json` in the repo root). Many agent stacks embed and consume that file. Hail Costs rows referenced there are a visibility win for the dataset.

## What to PR

There are two paths, in increasing scope:

### Path A — Sync individual missing rows (recommended first PR)

LiteLLM's pricing JSON is keyed by model identifier (for example, `"claude-opus-4-7"`, `"gpt-5-mini"`) with a flat schema per entry. Map Hail Costs rows into LiteLLM's shape. Submit a PR that adds only the models that LiteLLM does not have.

Concrete steps:

1. **Clone LiteLLM:** `gh repo fork BerriAI/litellm --clone`
2. **Diff against Hail Costs.** Read `litellm/model_prices_and_context_window.json` and Hail Costs' `costs/llm.json`. List the models that are present in Hail Costs but absent from LiteLLM.
3. **Translate the shape.** LiteLLM's per-model entry has this form:

   ```json
   "claude-opus-4-7": {
     "max_tokens": 128000,
     "max_input_tokens": 1000000,
     "max_output_tokens": 128000,
     "input_cost_per_token": 5e-06,
     "output_cost_per_token": 2.5e-05,
     "cache_read_input_token_cost": 5e-07,
     "litellm_provider": "anthropic",
     "mode": "chat",
     "supports_function_calling": true,
     "supports_vision": true,
     "supports_response_schema": true,
     "supports_prompt_caching": true
   }
   ```

   Hail Costs uses per-1M-token prices. LiteLLM uses per-token prices. Convert: `price_per_mtok / 1_000_000`. A decimal string → number conversion is acceptable here because LiteLLM's schema accepts numbers.

   Mapping table:

   | Hail Costs field                  | LiteLLM field                             |
   | --------------------------------- | ----------------------------------------- |
   | `input_per_mtok_usd`              | `input_cost_per_token` (÷ 1e6)            |
   | `output_per_mtok_usd`             | `output_cost_per_token` (÷ 1e6)           |
   | `cache_read_per_mtok_usd`         | `cache_read_input_token_cost` (÷ 1e6)     |
   | `cache_write_per_mtok_usd`        | `cache_creation_input_token_cost` (÷ 1e6) |
   | `context_window`                  | `max_input_tokens`                        |
   | `max_output_tokens`               | `max_output_tokens` (same name)           |
   | `supports_tool_use`               | `supports_function_calling`               |
   | `supports_vision`                 | `supports_vision` (same)                  |
   | `structured_output`               | `supports_response_schema`                |
   | `cache_read_per_mtok_usd` present | `supports_prompt_caching: true`           |
   | provider                          | `litellm_provider` (lowercased)           |
   | n/a                               | `mode: "chat"` (constant for LLM rows)    |

   Drop the fields that do not have a clean LiteLLM equivalent (`aggregators[]`, `aliases[]`, `cache_storage_per_mtok_per_hour_usd`, and others). Note them in the PR description.

4. **Open the PR.** Title: `feat: add N models from Hail Costs dataset`. Body template:

   ```markdown
   ## Summary

   Adds N model entries to `model_prices_and_context_window.json`, sourced from the [Hail Costs Dataset](https://hail.so/costs) (CC-BY-4.0).

   ## Models added

   - `<model_id>` (provider) — input $X/Mtok, output $Y/Mtok
   - ...

   ## Source

   Each row's source URL and last-verified date is documented in the Hail Costs Dataset at https://github.com/hail-hq/hail/blob/main/costs/llm.json. Hail applies a two-source verification rule per row.

   Citation: Hail Costs Dataset v0.2.0, https://hail.so/costs, accessed YYYY-MM-DD.
   ```

### Path B — Propose Hail Costs as an upstream data source (later, bigger ask)

After Path A is released and shows value, propose a richer integration: LiteLLM optionally consumes the Hail Costs raw JSON URLs (`https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json`) as a source. This requires a config flag and adapter code in LiteLLM itself. It is not in scope for the initial PR.

## Practical setup commands

```bash
# Outside this repo:
gh repo fork BerriAI/litellm --clone --remote
cd litellm

# Update the upstream branch
git remote add upstream https://github.com/BerriAI/litellm.git
git fetch upstream main
git checkout -b hail-costs-models upstream/main

# At this point an agent or human reads Hail Costs' llm.json, generates the
# new entries in LiteLLM's shape, and edits model_prices_and_context_window.json

# Validate (LiteLLM has its own validators — check the contributor guide)
# Then push and open the PR
git push origin hail-costs-models
gh pr create --repo BerriAI/litellm --title "feat: add N models from Hail Costs dataset" --body-file ../litellm-pr-body.md
```

## Status

**Not yet executed.** This document is the recipe. The PR itself needs three steps:

1. Generate the diff (Hail Costs models not in LiteLLM)
2. Write the translated entries
3. Open the PR against `BerriAI/litellm`

A Claude Code session pointed at this document can do all three steps. The human only needs to confirm and click "Open PR" on GitHub.

## Why not automate this?

The LiteLLM project moves fast. Their schema occasionally changes (new fields, renames). A hand-rolled adapter would break under their evolution. Periodic manual PRs (quarterly) that follow this recipe are more robust than a CI integration.
