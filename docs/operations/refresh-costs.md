# Weekly cost-data refresh runbook

> **For Claude Code sessions:** This document is self-contained. Read all of it and follow the procedure. You do not need other session context. This document and the canonical sources that it links to are sufficient.

## What this is

You must verify the `costs/` dataset (`costs/llm.json`, `costs/stt.json`, `costs/tts.json`) against the vendor pricing pages at regular intervals. Prices change, vendors deprecate models, and new variants launch. This runbook gives the procedure for a weekly pass.

The pass is a sequence of WebFetch dispatches, one for each provider family. Each dispatch compares the live pricing page with the existing rows and produces updates. A Claude Code session (or an agent with web-fetch and git-edit access) runs the pass.

## When to run

- The `costs-stale.yml` GitHub Action triggers the pass each week. The action opens an issue each Monday that lists the rows with a `last_verified` more than 30 days old.
- You can also run the pass manually at any time, for example after a high-profile model launch.
- You can also run the pass as part of a release-tag cut (`costs-v0.2.<N>` or higher).

## Pre-flight

Before you start, confirm these items:

1. **The working tree is clean** for `costs/` (no data PRs in flight). Stash or commit pending work first.
2. **The schema is at v2 with the v2.1+v2.2 extensions.** Make sure that the `version` const in `costs/schema/llm.schema.json` is `2`. Make sure that `audio_input_per_mtok_usd`, `price_per_second_usd`, and `aggregators[]` are declared.
3. **You can run `pnpm costs:validate`.** If `pipx run check-jsonschema` is broken on the host (Python ABI problems are common), use `check-jsonschema --schemafile <schema> <data>` directly with the `check-jsonschema` binary that is on PATH.

## Conventions you must follow

These rules are mandatory per the project tenets and the v0.2 design spec. For context, refer to [`costs/README.md`](../../costs/README.md) and [`docs/superpowers/specs/2026-05-17-costs-v0.2-design.md`](../superpowers/specs/2026-05-17-costs-v0.2-design.md).

1. **Two-source rule.** For every price-affecting change, cite the vendor pricing page plus one secondary source. A secondary source is a vendor announcement, a third-party aggregator, or the vendor API docs. If only one source is available, set `confidence: medium` on the row and give the reason in `notes`.
2. **Decimal-string prices.** All `*_per_*_usd` fields are JSON strings, not numbers. They must match the pattern `^(0|[1-9][0-9]*)(\.[0-9]{1,8})?$`. The validator rejects floats.
3. **Canonical model_ids.** Use the vendor's real API model_id string. Do not invent one. If a marketing name does not have a stable API identifier, defer the row.
4. **Two source verifications are the bar.** If the vendor pricing page is unreachable, or the model_id is not in the docs, defer the row with a documented reason. Do not guess.
5. **Always bump `last_verified`** on each row that you touch. **Bump `last_changed_at` only** if a structured price field changed.
6. **Field order convention** (refer to "Field order" below). Match it exactly.
7. **No destructive git in the shared tree.** In the default model of this runbook, every provider-agent edits the **same** working tree. Thus a subagent must never run a git command that reverts or discards the tree — `git checkout`, `git restore`, `git stash`, `git reset` — and must never run `git commit`/`git add`. A `checkout`/`restore` on a shared `costs/*.json` reverts the full file and silently erases the in-flight edits of every other provider — one such error caused the loss of a full LLM sweep. Repair broken formatting with the `Edit` tool or `pnpm exec prettier --write costs/<category>.json`, never with git. Leave the files modified but unstaged. Produce a commit message. Let the operator commit. (A separate git worktree for each provider makes git safe for each agent. But then you must add a step to collate the single-file edit of each worktree back. Thus the default is one shared tree plus sequential dispatch.)

## Per-row data model (cheat sheet)

LLM rows (`costs/llm.json`):

```
Required: provider, provider_url, model_id, display_name, context_window,
max_output_tokens, input_per_mtok_usd, output_per_mtok_usd, modalities,
last_verified, last_changed_at, verification_method, verified_by, source_url.

Optional (v2): aliases[], deployment_options[], free_tier, deprecated_at,
replaced_by_model_id, confidence, model_family, knowledge_cutoff,
cache_read_per_mtok_usd, cache_write_per_mtok_usd, batch_input_per_mtok_usd,
batch_output_per_mtok_usd, pricing_tiers[], reasoning_tokens_billed,
supports_tool_use, structured_output, supports_vision, supports_audio_in,
supports_audio_out, supports_pdf, latency_benchmark, notes.

Optional (v2.1): cache_storage_per_mtok_per_hour_usd, per_request_usd,
per_search_usd.

Optional (v2.2): audio_input_per_mtok_usd, aggregators[].

Optional (v2.3): featured.
```

STT rows (`costs/stt.json`):

```
Required: provider, provider_url, model_id, display_name, languages,
streaming, last_verified, last_changed_at, verification_method,
verified_by, source_url. PLUS one of price_per_minute_usd OR
price_per_second_usd (v2.2 anyOf constraint).

Optional: price_per_minute_batch_usd, diarization_per_minute_usd,
pii_redaction_per_minute_usd, realtime, realtime_latency_ms, diarization
enum, punctuation_included, formatting_included, min_billed_seconds,
max_audio_minutes_per_file, concurrent_streams_included, wer_benchmark,
time_to_first_word_ms, aliases[], deployment_options[], aggregators[],
free_tier, deprecated_at, replaced_by_model_id, confidence, notes.

Optional (v2.3): featured.
```

TTS rows (`costs/tts.json`):

```
Required: provider, provider_url, model_id, display_name, voice_quality,
languages, last_verified, last_changed_at, verification_method,
verified_by, source_url. PLUS one of price_per_1m_chars_usd OR
price_per_second_usd (v2.2 anyOf constraint).

Optional: voice_count, voices_count, voices_premium_count, ssml_supported,
emotion_control_supported, streaming_supported, voice_cloning,
output_formats[], sample_rates_hz[], time_to_first_byte_ms, min_billed_chars,
aliases[], deployment_options[], aggregators[], free_tier, deprecated_at,
replaced_by_model_id, confidence, notes.

Optional (v2.3): featured.
```

Authoritative source: [`costs/schema/llm.schema.json`](../../costs/schema/llm.schema.json), [`costs/schema/stt.schema.json`](../../costs/schema/stt.schema.json), [`costs/schema/tts.schema.json`](../../costs/schema/tts.schema.json).

## Field order (all categories)

Keep this order the same across all rows. The order makes diffs easy to review and agrees with the existing committed data.

**LLM:**

```
provider, provider_url, model_id, display_name, featured (when true),
model_family, knowledge_cutoff, aliases (when present),
context_window, max_output_tokens,
input_per_mtok_usd, output_per_mtok_usd,
audio_input_per_mtok_usd (when set),
cache_read_per_mtok_usd, cache_write_per_mtok_usd (when set),
cache_storage_per_mtok_per_hour_usd (when set),
batch_input_per_mtok_usd, batch_output_per_mtok_usd (when set),
per_request_usd, per_search_usd (when set),
pricing_tiers (when set),
modalities,
supports_tool_use, structured_output, supports_vision,
supports_audio_in, supports_audio_out, supports_pdf,
reasoning_tokens_billed,
deployment_options, aggregators (when set),
free_tier (when set),
deprecated_at, replaced_by_model_id (when present),
confidence (when non-default),
last_verified, last_changed_at, verification_method, verified_by,
source_url, notes
```

**STT:**

```
provider, provider_url, model_id, display_name,
featured (when true),
aliases (when present),
price_per_minute_usd, price_per_second_usd (whichever applies),
price_per_minute_batch_usd, diarization_per_minute_usd, pii_redaction_per_minute_usd (when set),
languages, streaming, realtime, realtime_latency_ms (when set),
diarization, punctuation_included, formatting_included (when set),
min_billed_seconds, max_audio_minutes_per_file, concurrent_streams_included (when set),
wer_benchmark (when set), time_to_first_word_ms (when set),
deployment_options, aggregators (when set),
free_tier (when set),
deprecated_at, replaced_by_model_id (when present),
confidence (when non-default),
last_verified, last_changed_at, verification_method, verified_by,
source_url, notes
```

**TTS:**

```
provider, provider_url, model_id, display_name,
featured (when true),
aliases (when present),
price_per_1m_chars_usd, price_per_second_usd (whichever applies),
min_billed_chars (when set),
voice_quality, voice_count, voices_count, voices_premium_count (when set),
languages,
ssml_supported, emotion_control_supported, streaming_supported (when set),
voice_cloning (when set),
output_formats (when set), sample_rates_hz (when set),
time_to_first_byte_ms (when set),
deployment_options, aggregators (when set),
free_tier (when set),
deprecated_at, replaced_by_model_id (when present),
confidence (when non-default),
last_verified, last_changed_at, verification_method, verified_by,
source_url, notes
```

## Featured models

`featured: true` puts a model into the prerendered `/costs/compare/<a>-vs-<b>` page set. Every within-category pair of featured models becomes a static page. For the design rationale, refer to [`docs/superpowers/specs/2026-07-27-costs-compare-crawl-trap-design.md`](../superpowers/specs/2026-07-27-costs-compare-crawl-trap-design.md).

Rules for a refresh pass:

- **If a new marquee model launched**, the model is a candidate for `featured: true`. When you add the flag, the next deploy creates the comparison pages for the model. You do not edit anything else.
- **If a featured model is deprecated**, **keep the flag.** The page stays live with a deprecation banner, so an indexed URL never returns a 404. Make sure that `replaced_by_model_id` resolves to a model in the same file.
- **Never remove a featured flag** to clean up. If you remove one, you delete indexed pages, and `scripts/costs/check-featured.mjs` fails the build.
- When you **add** a flag, you change the generated slug set. Regenerate and commit the lockfile:

```bash
pnpm costs:featured --write   # regenerates web/lib/featured.lock.json
pnpm costs:featured           # verifies invariants; must print "Featured set OK"
```

- The removal of a flag is a deliberate de-indexing decision. Do **not** run `--write` to silence the failure that results. Restore the flag, or hand-edit `web/lib/featured.lock.json` in the same commit, so the deletion is visible in review.

Keep the set small. Each model that you add creates a page against each existing featured model in its category. Thus N models produce N×(N−1)/2 pages.

## Provider catalog

The agent verifies each provider again against these primary and secondary sources. Append new providers as the dataset grows.

| Category | Provider         | Primary pricing URL                                                                   | Secondary URL                                                                     | Notes                                                                                                                                                                                                                          |
| -------- | ---------------- | ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| LLM      | Anthropic        | https://www.anthropic.com/pricing                                                     | https://docs.anthropic.com/en/docs/about-claude/models                            | Bedrock + Vertex confirmed via `https://aws.amazon.com/bedrock/pricing/`                                                                                                                                                       |
| LLM      | OpenAI           | https://platform.openai.com/docs/pricing                                              | https://developers.openai.com/api/docs/models                                     | `openai.com/api/pricing/` 403s; use developers.openai.com mirror                                                                                                                                                               |
| LLM      | Google (Gemini)  | https://ai.google.dev/pricing                                                         | https://ai.google.dev/gemini-api/docs/models                                      | Pricing tiers >200k tokens documented per row                                                                                                                                                                                  |
| LLM      | Meta (Llama)     | https://huggingface.co/meta-llama                                                     | per-host (Together / Fireworks / Groq / Bedrock)                                  | Multi-host single row; use lowest direct-host rate; aggregators[] for OpenRouter                                                                                                                                               |
| LLM      | Mistral          | https://mistral.ai/pricing                                                            | https://docs.mistral.ai/getting-started/models/models_overview/                   | Deprecation table on docs is authoritative                                                                                                                                                                                     |
| LLM      | DeepSeek         | https://api-docs.deepseek.com/quick_start/pricing                                     | https://api-docs.deepseek.com/news/news                                           | Watch the cache-discount math                                                                                                                                                                                                  |
| LLM      | Cohere           | https://cohere.com/pricing                                                            | https://docs.cohere.com/docs/models                                               | Deprecation table at docs.cohere.com/docs/deprecations                                                                                                                                                                         |
| LLM      | xAI (Grok)       | https://docs.x.ai/docs/models                                                         | https://docs.x.ai/docs/pricing                                                    | Retirements via docs.x.ai/developers/migration/\*                                                                                                                                                                              |
| LLM      | Alibaba (Qwen)   | https://www.alibabacloud.com/help/en/model-studio/billing                             | https://www.alibabacloud.com/help/en/model-studio/models                          | DashScope is the canonical price; cache pricing tiers by context bracket                                                                                                                                                       |
| LLM      | Perplexity       | https://docs.perplexity.ai/getting-started/pricing                                    | https://docs.perplexity.ai/docs/sonar/models                                      | Per-request and per-search fees in v2.1 fields                                                                                                                                                                                 |
| STT      | Deepgram         | https://deepgram.com/pricing                                                          | https://developers.deepgram.com/docs/models-languages-overview                    | Nova-3 family + Aura TTS in same vendor                                                                                                                                                                                        |
| STT      | AssemblyAI       | https://www.assemblyai.com/pricing                                                    | https://www.assemblyai.com/docs/getting-started/models                            | Universal-1/Slam-1 frequently absent from pricing page; defer if so                                                                                                                                                            |
| STT      | Microsoft Azure  | https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/ | https://learn.microsoft.com/en-us/azure/ai-services/speech-service/               | Real-time + batch as separate rows                                                                                                                                                                                             |
| STT      | Google Cloud STT | https://cloud.google.com/speech-to-text/pricing                                       | https://cloud.google.com/speech-to-text/v2/docs/transcription-model               | Pricing page sometimes truncates via WebFetch                                                                                                                                                                                  |
| STT      | OpenAI Whisper   | https://platform.openai.com/docs/models                                               | https://developers.openai.com/api/docs/models                                     | Single row                                                                                                                                                                                                                     |
| STT      | Groq Whisper     | https://console.groq.com/docs/models                                                  | https://console.groq.com/docs/deprecations                                        | Hosted variants of OSS Whisper. groq.com/pricing now redirects to the marketing homepage and groq.com/groqcloud-models 404s (confirmed 2026-08-11) — use console.groq.com                                                      |
| STT      | Cartesia (Ink)   | https://cartesia.ai/pricing                                                           | https://docs.cartesia.ai                                                          | STT product, not TTS                                                                                                                                                                                                           |
| STT      | Speechmatics     | https://www.speechmatics.com/pricing                                                  | https://docs.speechmatics.com                                                     | Tier names instead of model_ids                                                                                                                                                                                                |
| STT      | Rev.ai           | https://www.rev.ai/pricing                                                            | https://docs.rev.ai                                                               | Whisper Fusion + Reverb                                                                                                                                                                                                        |
| STT      | Gladia           | https://www.gladia.io/pricing                                                         | https://docs.gladia.io                                                            | Solaria-1                                                                                                                                                                                                                      |
| STT      | Soniox           | https://soniox.com/pricing                                                            | https://soniox.com/docs/stt/models                                                | Token-priced; confidence: medium per row                                                                                                                                                                                       |
| TTS      | ElevenLabs       | https://elevenlabs.io/pricing/api                                                     | https://elevenlabs.io/docs/models                                                 | Credit-per-char system; anchor to Flash v2.5                                                                                                                                                                                   |
| TTS      | Cartesia         | https://cartesia.ai/pricing                                                           | https://docs.cartesia.ai/build-with-cartesia/tts-models/older-models              | Sonic family                                                                                                                                                                                                                   |
| TTS      | OpenAI           | https://platform.openai.com/docs/models                                               | https://developers.openai.com/api/docs/models                                     | tts-1, tts-1-hd, gpt-4o-mini-tts                                                                                                                                                                                               |
| TTS      | Groq (Orpheus)   | https://console.groq.com/docs/models                                                  | https://console.groq.com/docs/deprecations                                        | Multiple language variants. groq.com/pricing now redirects to the marketing homepage and groq.com/groqcloud-models 404s (confirmed 2026-08-11) — use console.groq.com                                                          |
| TTS      | Google Cloud TTS | https://cloud.google.com/text-to-speech/pricing                                       | https://cloud.google.com/text-to-speech/docs/voices                               | model_id uses Hail-coined tier slugs                                                                                                                                                                                           |
| TTS      | Azure TTS        | https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/ | https://learn.microsoft.com/en-us/azure/ai-services/speech-service/text-to-speech | model_id uses Hail-coined tier slugs                                                                                                                                                                                           |
| TTS      | Deepgram (Aura)  | https://deepgram.com/pricing                                                          | https://developers.deepgram.com/docs/tts-models                                   | aura-2 family                                                                                                                                                                                                                  |
| TTS      | Inworld          | https://inworld.ai/pricing                                                            | https://docs.inworld.ai/tts/tts-models                                            | inworld-tts-2                                                                                                                                                                                                                  |
| TTS      | Smallest.ai      | https://smallest.ai/pricing                                                           | https://docs.smallest.ai/waves/documentation/text-to-speech-lightning/overview    | lightning-v3.1                                                                                                                                                                                                                 |
| TTS      | Rime             | https://rime.ai/pricing                                                               | https://docs.rime.ai/api-reference/models                                         | mistv3                                                                                                                                                                                                                         |
| TTS      | LMNT             | https://www.lmnt.com/pricing                                                          | https://docs.lmnt.com/models/overview                                             | **Vendor shut down (confirmed 2026-09-02)** — "Our speech generation journey has come to an end." `blizzard` row marked `deprecated_at`, no successor. Skip re-dispatching; re-check only if the vendor resurfaces.            |
| TTS      | Resemble AI      | https://www.resemble.ai/pricing                                                       | https://github.com/resemble-ai/chatterbox                                         | chatterbox-turbo via v2.2 `price_per_second_usd`                                                                                                                                                                               |
| TTS      | Hume             | https://hume.ai/pricing                                                               | https://dev.hume.ai/docs/text-to-speech-tts/overview                              | Currently deferred — no canonical API model_id                                                                                                                                                                                 |
| TTS      | PlayHT           | https://play.ht/pricing                                                               | https://docs.play.ht/reference/models                                             | **Vendor shut down (confirmed 2026-09-02)** — acqui-hired by Meta, service fully shut down 2025-12-31, `play.ht` no longer resolves. No row existed to deprecate. Skip re-dispatching; re-check only if the vendor resurfaces. |
| TTS      | Unreal Speech    | https://unrealspeech.com/pricing                                                      | n/a                                                                               | Currently deferred — subscription-only                                                                                                                                                                                         |

## Procedure

Do the steps below in sequence. Each provider gets one focused dispatch.

### Step 0 — Pre-flight

```bash
cd /Users/r/playground/hail   # or wherever the repo lives
git status --short costs/      # must be empty
pnpm costs:validate            # baseline must pass (fall back to direct check-jsonschema if needed)
```

If `git status` shows changes under `costs/`, stop and reconcile them. Do not continue on a dirty tree.

### Step 1 — Dispatch one subagent per provider

For each provider in the catalog above, dispatch a Claude Code subagent with the **per-provider prompt template** (next section). Run the providers in sequence, not in parallel, because they all edit the same files.

Strategy hints:

- Start with the high-leverage providers (Anthropic, OpenAI, Google). They are the most consumed and the most volatile.
- If a vendor pricing page is unreachable on the first WebFetch, retry one time with a fallback URL from the catalog. If the page is still unreachable, defer the full provider for this pass.
- Monitor for deprecations. They occurred in 4 of 15 PRs during Phase 1. Always check.
- Monitor for new models. The marquee providers launch new models most weeks.

### Step 2 — After each dispatch, the subagent must have:

- Edited `costs/{llm,stt,tts}.json` for that provider's rows
- Bumped `last_verified` on touched rows; `last_changed_at` only where a price changed
- Added new model rows if any launched (with two-source verification per row)
- Marked deprecated rows with `deprecated_at` and `replaced_by_model_id` if a successor is in-file
- Run `pnpm costs:validate` — passed
- Run the jq referential checks (next section) — passed
- Run `pnpm site:build` — passed
- Left the file modified-but-unstaged for the controller to stage

### Step 3 — Stage and close out

When all providers are processed (or deferred):

```bash
git add costs/llm.json costs/stt.json costs/tts.json
git diff --cached --stat
```

Produce one rolled-up commit message that summarizes:

- How many rows touched
- New rows added (provider + model_id)
- New deprecations marked
- Defers (with reasons)

Commit (the operator does this step manually):

```
git commit -m "feat(costs): weekly refresh YYYY-MM-DD"
```

If applicable, bump the patch tag:

```
git tag -a costs-v0.2.<next> -m "Weekly refresh YYYY-MM-DD"
git push origin main costs-v0.2.<next>
```

## Per-provider dispatch prompt template

Copy and paste this into a `Task` tool call (or equivalent). Replace the placeholders.

````
You are running a weekly cost-data refresh for {provider} in the Hail Costs Dataset.

## Working directory

/Users/r/playground/hail

## Workflow constraints

- You share ONE working tree with the other provider-agents. Never run a git command that reverts or discards it — `git checkout`, `git restore`, `git stash`, `git reset` — nor `git commit`/`git add`. A `checkout`/`restore` on `costs/{category}.json` reverts the whole shared file and destroys other providers' uncommitted edits. Fix broken formatting with the `Edit` tool or `pnpm exec prettier --write costs/{category}.json` — never with git.
- You may edit `costs/{category}.json` and leave it modified-but-unstaged.
- The controller runs providers **sequentially**, never in parallel — they all edit the same file.

## Project context

`costs/{category}.json` is a v2 dataset (with v2.1 + v2.2 schema extensions). User: `r13i`. Today: {today}. Schema at `costs/schema/{category}.schema.json`.

Conventions are in `docs/operations/refresh-costs.md`. The field order, decimal-string price format, and two-source rule are non-negotiable.

## Scope

Re-verify the existing {provider} rows in `costs/{category}.json` against the live vendor pricing page. For each row:

1. Confirm the model_id is still active (not silently retired)
2. Confirm the prices match
3. Confirm any other fields (context window, knowledge cutoff, etc.) are still accurate
4. Bump `last_verified` to {today}
5. Bump `last_changed_at` ONLY if a structured price field actually moved
6. If a row is now deprecated, set `deprecated_at` and `replaced_by_model_id` (must resolve in-file post-PR)

Then check whether the vendor has launched any new models worth adding:

1. Compare the vendor's current model list against the existing {provider} rows in the file
2. For any new models, verify the two-source rule can be met
3. Add new rows in the same field-order convention as existing rows
4. Defer with a documented reason if a model is unverifiable

## Sources

- Primary: {primary_url}
- Secondary: {secondary_url}

If the primary page is unreachable via WebFetch, fall back to the secondary. If both fail, defer the entire provider for this pass with a one-line note.

## Steps

1. Read the current {provider} rows:
   ```
   jq '.models[] | select(.provider == "{provider}")' costs/{category}.json
   ```

2. WebFetch the primary source. Capture current prices, context windows, deprecation banners, and any new model_ids.

3. WebFetch the secondary source for cross-verification.

4. Use `Edit` tool on `costs/{category}.json`:
   - Update existing {provider} rows that need price / status changes
   - Bump `last_verified` to {today} on every row touched
   - Bump `last_changed_at` only where a structured price moved
   - Append new rows for verified launches
   - Mark deprecations where applicable

5. Run `pnpm costs:validate`. Fall back to `check-jsonschema --schemafile costs/schema/{category}.schema.json costs/{category}.json` if the pnpm wrapper breaks on local env issues.

6. Run jq referential checks (alias uniqueness + replaced_by resolution):
   ```bash
   for f in costs/llm.json costs/stt.json costs/tts.json; do
     dupes=$(jq -r '[.models[] | .aliases // [] | .[]] | group_by(.) | map(select(length > 1) | .[0])' "$f")
     unresolved=$(jq -r '(.models | map(.model_id)) as $ids | [.models[] | select(.replaced_by_model_id != null) | select((.replaced_by_model_id as $r | $ids | index($r)) == null) | .model_id]' "$f")
     echo "$f aliases: $dupes ; replaced_by: $unresolved"
   done
   ```
   Each line must end `[] ; replaced_by: []`.

7. Run `pnpm site:build`. Must pass.

8. Do NOT run any git command — no stage, commit, checkout, restore, stash, or reset.

## Report

- Status: DONE | DONE_WITH_CONCERNS | BLOCKED
- For each existing {provider} row: same / price changed / deprecated / removed
- New rows added (with model_id + price)
- Defers (with reasons)
- Validate / jq / build outputs
- Concerns
````

## jq referential checks (run after every dispatch)

```bash
for f in costs/llm.json costs/stt.json costs/tts.json; do
  dupes=$(jq -r '[.models[] | .aliases // [] | .[]] | group_by(.) | map(select(length > 1) | .[0])' "$f")
  unresolved=$(jq -r '(.models | map(.model_id)) as $ids | [.models[] | select(.replaced_by_model_id != null) | select((.replaced_by_model_id as $r | $ids | index($r)) == null) | .model_id]' "$f")
  echo "$f aliases: $dupes ; replaced_by: $unresolved"
done
```

Each line must end with `[] ; replaced_by: []`. The CI workflow at `.github/workflows/costs-validate.yml` enforces the same checks.

## Closing checklist

Before you sign off the refresh:

- [ ] `pnpm costs:validate` passes (all three categories `ok -- validation done`)
- [ ] jq referential checks pass (every line ends `[] ; replaced_by: []`)
- [ ] `pnpm site:build` passes (Next.js compiles, all static pages generated)
- [ ] `pnpm costs:featured` passes (`Featured set OK`); if any `featured` flag changed, `web/lib/featured.lock.json` was regenerated with `--write` and staged
- [ ] `node --test "scripts/costs/*.test.mjs"` passes
- [ ] All touched rows have `last_verified` bumped to today
- [ ] No row has both per-unit price fields set with conflicting math (STT: `price_per_minute = price_per_second × 60`; TTS: analogous if both populated)
- [ ] No new aggregator added without at least one direct host in `deployment_options[]`
- [ ] `git diff --cached` shows ONLY rows in `costs/{llm,stt,tts}.json` — no schema, no README, no unrelated files
- [ ] One rolled-up commit message documents the row count, new additions, deprecations, and defers
- [ ] Stop. Let the operator run `git commit`.

## How to point a fresh Claude Code session at this document

This is the fastest workflow:

```
1. Open a new Claude Code session in the Hail repo.
2. Say: "Read docs/operations/refresh-costs.md and run a weekly refresh."
3. The agent should:
   a. Read this document end-to-end.
   b. Run pre-flight checks (Step 0).
   c. Dispatch one subagent per provider per the prompt template (Step 1).
   d. Stage the result and produce a commit message (Step 3).
   e. Stop without committing.
4. You review the diff, commit when satisfied, optionally tag.
```

The agent does not need prior session context. This document plus the canonical sources that it links to are the complete inputs.

## When this runbook needs updates

Edit this file when:

- You add a new provider to the catalog (append it to the table in "Provider catalog")
- A vendor URL changes (the most common edit; vendors frequently rename pricing pages)
- A new schema field is released (the v2.x evolution path) — update the "Per-row data model" section
- The two-source rule, decimal-string rule, or field-order convention changes (this also requires a schema-version bump)
- The featured-model policy changes (refer to "Featured models") — update that section and the closing checklist together

Schema-shaped changes get their own design spec under `docs/superpowers/specs/`. Catalog edits do not — they are maintenance of this runbook.
