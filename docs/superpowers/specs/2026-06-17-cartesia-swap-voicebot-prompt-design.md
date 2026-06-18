# Cartesia TTS swap + structured voicebot prompt

**Date:** 2026-06-17
**Status:** Approved — ready for implementation plan

## Summary

Two coupled changes to the voicebot, delivered as one spec:

- **Part A — TTS providers.** Add Cartesia as the **primary** runtime
  text-to-speech provider with **ElevenLabs as a fallback**, via the SDK's
  `tts.FallbackAdapter`. This mirrors the existing hardcoded LLM fallback chain
  (OpenAI → Google → Anthropic), so the SDK adapter now has a second concrete
  use (tenet 2 satisfied). ElevenLabs is retained, not removed.
- **Part B — Prompt rewrite (the headline).** Replace the single dense
  `VOICE_PREAMBLE` paragraph with the LiveKit prompting guide's structured
  Markdown sections, tuned for Cartesia's capabilities.
- **Part C — Docs.** Update README, CHANGELOG, and prose docs that name
  ElevenLabs as the runtime TTS.

The two engineering parts are coupled: the prompt's realism techniques depend
on what the TTS provider can render, so the provider choice decides the prompt
content.

## Background

The pipeline is **Deepgram STT → LLM → ElevenLabs TTS** (cascaded). The LLM is
either Hail's fallback chain (OpenAI → Google → Anthropic) or a caller-supplied
OpenAI-compatible endpoint — the LLM already uses `agents_llm.FallbackAdapter`,
the established precedent this design extends to TTS. Today the agent's
instructions are a single dense
paragraph (`VOICE_PREAMBLE` in `voicebot/hailhq/voicebot/agent.py`) prepended
to any caller-supplied `system_prompt` via `build_instructions`.

Two facts shape the design:

1. **Cartesia ≠ ElevenLabs on inline markup.** Cartesia renders `<break>`
   (pauses) and `<spell>…</spell>` (character readout) inline, and is driven
   primarily by **punctuation** for prosody. It does **not** support
   ElevenLabs-style inline tags (`[laughs]`, `[sighs]`, inline emotion) — it
   reads them aloud literally. Emotion/speed/volume are TTS **constructor
   parameters**, not prompt techniques.
2. **Inline tags leak into stored transcripts.** `agent.py` stores the LLM's
   raw `text_content` on every `conversation_item_added` event with no
   sanitization (line ~479). Any tag the LLM emits lands in the human-read call
   record, not just in TTS — the same class of problem the existing no-emoji
   rule fixes.

Together these dictate a **punctuation-first** prompt: minimal inline tags
(`<break>` sparingly, `<spell>` for codes), no inline emotion/sound tags,
emotion left to the voice default. This is both Cartesia-appropriate and keeps
transcripts clean.

## Part A — Cartesia primary TTS + ElevenLabs fallback

Cartesia primary voice ID: `4bc3cb8c-adb9-4bb8-b5d5-cbbef950b991`. Model:
`sonic-3` (model names live in `.env` per house rule; `Settings` fields default
to `""`). ElevenLabs is kept as the fallback provider — its config, keys, and
plugin all stay.

| File                                   | Change                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `voicebot/pyproject.toml`              | **add** `livekit-plugins-cartesia>=1.5,<2` (match the per-plugin style); keep `livekit-plugins-elevenlabs`                                                                                                                                                                                                                                                                                                                                                                                                        |
| `core/hailhq/core/config.py`           | **add** `cartesia_api_key`, `cartesia_voice_id`, `cartesia_model` (default `""`); **keep** `eleven_api_key`, `elevenlabs_voice_id`, `elevenlabs_model`                                                                                                                                                                                                                                                                                                                                                            |
| `.env.example`                         | add a Cartesia block above the (retained) ElevenLabs block under the Voice section (see below)                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `core/hailhq/core/schemas.py`          | `VoiceConfig.tts: Literal["cartesia"] = "cartesia"` — the **primary** provider. The fallback is operator/infra config, not caller-selectable (consistent with the LLM fallback chain, which is also not caller-selectable).                                                                                                                                                                                                                                                                                       |
| `voicebot/hailhq/voicebot/pipeline.py` | build the TTS from a `build_tts()` helper (mirrors `build_llm`): assemble a list — Cartesia first when `cartesia_api_key` set, ElevenLabs appended when `eleven_api_key` set. Wrap in `agents_tts.FallbackAdapter([...])` when the list has ≥2 entries; use the single instance directly when only one provider is configured (graceful single-provider degrade, tenet 4). Cartesia uses a plain UUID voice (plugin path; the `cartesia/sonic-3:<uuid>` form is for LiveKit Inference only, which we do not use). |
| `openapi/openapi.yaml`                 | regenerate — `VoiceConfig.tts` const/default `elevenlabs` → `cartesia` (invariant: OpenAPI is source of truth for the CLI)                                                                                                                                                                                                                                                                                                                                                                                        |
| `sdk/hail/models.py`                   | regenerate `VoiceConfig` to match the schema change                                                                                                                                                                                                                                                                                                                                                                                                                                                               |

`build_tts()` ordering rule: Cartesia is always primary when present; ElevenLabs
is always the fallback. Order is fixed in code (mirrors the hardcoded LLM
fallback chain), not env-driven. If only one provider's key is set, that
provider is used alone with no `FallbackAdapter`.

`.env.example` Voice section (keep logically sorted: STT, then primary TTS, then
fallback TTS):

```
# ─── Voice ───────────────────────────────────────────────────────────────────
# Deepgram (STT)
DEEPGRAM_API_KEY=
DEEPGRAM_MODEL=nova-3

# Cartesia (primary TTS) — CARTESIA_VOICE_ID is required at runtime.
CARTESIA_API_KEY=
CARTESIA_VOICE_ID=4bc3cb8c-adb9-4bb8-b5d5-cbbef950b991
CARTESIA_MODEL=sonic-3

# ElevenLabs (fallback TTS) — optional; used only when ELEVEN_API_KEY is set.
ELEVEN_API_KEY=
ELEVENLABS_VOICE_ID=
ELEVENLABS_MODEL=eleven_turbo_v2_5
```

**Pipeline imports:** add `from livekit.agents import tts as agents_tts` and the
cartesia plugin (mirrors the existing `agents_llm` / `agents_vad` imports).

**Tests to update / add:**

- `voicebot/tests/test_pipeline.py` — assert `build_tts()` returns a
  `FallbackAdapter` with Cartesia primary + ElevenLabs fallback when both keys
  are set; assert a single Cartesia (or single ElevenLabs) instance with no
  adapter when only one key is set.
- `core/tests/test_schemas.py` — update `VoiceConfig.tts` default to `cartesia`.
- `voicebot/tests/test_agent.py` — asserts on `VOICE_PREAMBLE` text the rewrite changes.

## Part B — Structured voicebot prompt

Replace the single-paragraph `VOICE_PREAMBLE` with structured Markdown
following the LiveKit prompting guide, Cartesia-tuned. Sections:

- **Identity** — an AI voice assistant on a live phone call, placing it on
  behalf of the person who set it up. It hears the caller through
  speech-to-text and replies through text-to-speech; it must never say it is
  "text-based" or that it cannot hear audio, and never claim to be human.
- **Output rules** — interacting via voice; apply TTS-friendly formatting:
  - Plain text only — no emoji, markdown, lists, tables, code, or symbols that
    cannot be read aloud.
  - Keep replies brief: one to two sentences, then pause for the other party.
    Ask one question at a time.
  - Use proper punctuation and capitalization (Cartesia's prosody driver).
  - Spell out numbers, phone numbers, and email addresses in conventional
    written form.
  - Use `<spell>CODE</spell>` for confirmation codes, IDs, and serial numbers.
  - Omit `https://` and other formatting when saying a web address.
- **Conversational flow** — accomplish the objective efficiently; provide
  guidance in small steps and confirm completion before continuing; summarize
  key results when closing a topic.
- **Guardrails** — stay within safe, lawful, in-scope use; decline harmful or
  out-of-scope requests; for medical/legal/financial topics give general
  information only and suggest a professional; protect privacy and minimize
  sensitive data.

**Deliberately excluded:**

- **No Tools section** — the voicebot has no LLM function tools (only the
  soft-cap timer). Omit it (YAGNI).
- **No inline emotion / non-verbal sound tags** (`[laughs]`, `[sighs]`, etc.) —
  Cartesia reads them literally and they leak into stored transcripts.
- **No emotion in the prompt** — it is a Cartesia TTS constructor parameter, not
  a prompt technique; left at the voice default for v1.
- **`<break>` used sparingly** — punctuation handles most pacing; minimal tag
  use keeps transcripts clean.

**`build_instructions` composition.** Keep the preamble non-overridable and
first. Append a caller-supplied `system_prompt` under an explicit
`# Caller instructions` boundary so the caller's own `#` headers do not collide
with the preamble's section structure. When no caller prompt is supplied, the
structured preamble alone is the instruction set.

## Part C — README / CHANGELOG / docs

Runtime-TTS references (update to Cartesia):

| File:line                    | Change                                                                                                                                                     |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `README.md:14`               | quickstart env list: `Deepgram, ElevenLabs` → `Deepgram, Cartesia` (Cartesia is the primary; ElevenLabs fallback is optional)                              |
| `README.md:95-96`            | Voice-pipeline milestones: mark **both** `[x] Cartesia` and keep `[x] ElevenLabs` checked (both are now supported — Cartesia primary, ElevenLabs fallback) |
| `CHANGELOG.md:95`            | `ElevenLabs TTS.` → `Cartesia TTS (primary) with ElevenLabs fallback.` (unreleased Voice-pipeline entry)                                                   |
| `docs/architecture.md:16`    | pipeline diagram `TTS: ElevenLabs` → `TTS: Cartesia (→ ElevenLabs fallback)`                                                                               |
| `docs/operations.md:104`     | provider-key list: replace the single ElevenLabs line with Cartesia (primary; `CARTESIA_API_KEY` + voice ID) and ElevenLabs (optional fallback)            |
| `docs/setup/vm-deploy.md:86` | secrets list: `…Deepgram, ElevenLabs…` → `…Deepgram, Cartesia (+ optional ElevenLabs fallback)…`                                                           |

**Left untouched (not runtime config):**

- `docs/operations/refresh-costs.md:188` — multi-provider TTS cost-comparison
  table; Cartesia is already a row. Tracks all providers for comparison, not
  what Hail runs.
- `docs/superpowers/{specs,plans}/*` — historical records.

## Testing

- Unit: update the three test files in Part A; add/adjust assertions on the new
  structured `build_instructions` output (preamble sections present, caller
  prompt appended under its boundary).
- CI gates unchanged: ruff + black + mypy + pytest (Python), gofmt (Go).
- OpenAPI/SDK regeneration verified in the same PR (invariant).

## Out of scope

- **Caller-facing TTS provider selection.** The Cartesia→ElevenLabs fallback
  order is fixed in code; callers cannot choose or reorder providers per call
  (consistent with the LLM fallback chain).
- **Env-configurable fallback order.** Order is hardcoded; not driven by env.
- Transcript sanitization changes (minimal-tag approach keeps records clean;
  revisit only if richer tag use is adopted later).
- Cartesia emotion/speed/volume tuning.
- LiveKit Inference and its server-side Inference Fallback Adapter (we use the
  plugin path with our own API keys, so we use the Agent `tts.FallbackAdapter`).

```

```
