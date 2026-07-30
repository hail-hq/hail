# Multi-language voicebot: Speechmatics STT + language matrix

**Date:** 2026-07-29
**Status:** Approved design, pre-implementation
**Scope:** hail repo (PR 1) + hail-website console (PR 2)

## Goal

Let a caller place a call in any of 39 supported languages by setting
`voice_config.language`, with the right STT provider and turn-detection
strategy chosen automatically. Add Speechmatics as a second STT provider
(house-routed and BYO). Exclude languages where the pipeline's STT and TTS
capabilities mismatch.

## Research basis

A 10-agent research workflow (2026-07-29, adversarially verified, zero
corrections) produced a language-support matrix from official provider docs:

| Capability     | Model                                         | Streaming languages                                                 |
| -------------- | --------------------------------------------- | ------------------------------------------------------------------- |
| STT            | Deepgram nova-3                               | ~50, incl. da/sv/no/fi (expanded through 2025)                      |
| STT            | Speechmatics enhanced/standard                | ~50, incl. all Nordics; built-in end-of-utterance for all languages |
| TTS            | Cartesia sonic-3.5 (house primary)            | 42                                                                  |
| TTS            | ElevenLabs eleven_turbo_v2_5 (house fallback) | 32                                                                  |
| Turn detection | LiveKit MultilingualModel                     | 14: en es fr de it pt nl zh ja ko id tr ru hi                       |

Key sources: docs.cartesia.ai/build-with-cartesia/tts-models/latest,
docs.speechmatics.com/speech-to-text/realtime/end-of-turn,
docs.livekit.io/agents/integrations/stt/speechmatics/,
docs.livekit.io/agents/build/turns/turn-detector/, deepgram.com/pricing,
speechmatics.com/pricing.

Findings that shaped the design:

- **nova-3 already covers the Nordics** — Speechmatics adds ~zero raw
  coverage inside the Cartesia-TTS-bounded set. Its value is transcription
  quality plus built-in ML turn detection.
- **Speechmatics is the only path to ML turn detection outside LiveKit's
  14 languages.** `livekit-plugins-speechmatics` (v1.6.7) exposes
  `turn_detection_mode` FIXED/ADAPTIVE/SMART_TURN and integrates via
  `AgentSession(turn_detection="stt")`. Deepgram nova-3 has only
  silence endpointing.
- **hail today wires no turn detector at all** (pure Silero VAD); the
  MultilingualModel is added as part of this feature.
- Pricing (streaming, official, 2026-07-29): Deepgram nova-3 $0.0048/min
  mono (promo; $0.0077 regular); Speechmatics standard $0.0075/min,
  enhanced $0.0133/min.

## Decisions (locked with user)

1. **Routing:** by turn-detection capability, not by provider preference.
2. **Default STT stays Deepgram** where LiveKit's semantic turn detector
   covers the language; Speechmatics where it doesn't.
3. **Validation:** hard enum in OpenAPI + server-side provider-aware 422.
4. **`stt: "auto"`** replaces the hardcoded `"deepgram"` default.
5. **ADAPTIVE, not SMART_TURN** in v1 (SMART_TURN needs the `[smart]`
   local-model extra; defer).
6. **ElevenLabs fallback trimmed** for the 7 Cartesia-only languages
   rather than excluding those languages.
7. **gu/kn/te ship VAD-only** (nova-3-only STT, no Speechmatics).
8. Speechmatics also becomes a **console BYO STT option** (hail-website).

## Supported languages (39)

Supported = (nova-3 ∪ Speechmatics STT) ∩ Cartesia sonic-3.5.
Excluded for capability mismatch: `ka`, `ml`, `pa` (STT only on
Whisper-based models hail doesn't run).

| Code | Language   | Auto STT route | Turn detection     | ElevenLabs TTS fallback |
| ---- | ---------- | -------------- | ------------------ | ----------------------- |
| ar   | Arabic     | speechmatics   | STT-adaptive       | yes                     |
| bg   | Bulgarian  | speechmatics   | STT-adaptive       | yes                     |
| bn   | Bengali    | speechmatics   | STT-adaptive       | no                      |
| cs   | Czech      | speechmatics   | STT-adaptive       | yes                     |
| da   | Danish     | speechmatics   | STT-adaptive       | yes                     |
| de   | German     | deepgram       | semantic (LiveKit) | yes                     |
| el   | Greek      | speechmatics   | STT-adaptive       | yes                     |
| en   | English    | deepgram       | semantic (LiveKit) | yes                     |
| es   | Spanish    | deepgram       | semantic (LiveKit) | yes                     |
| fi   | Finnish    | speechmatics   | STT-adaptive       | yes                     |
| fr   | French     | deepgram       | semantic (LiveKit) | yes                     |
| gu   | Gujarati   | deepgram       | VAD only           | no                      |
| he   | Hebrew     | speechmatics   | STT-adaptive       | no                      |
| hi   | Hindi      | deepgram       | semantic (LiveKit) | yes                     |
| hr   | Croatian   | speechmatics   | STT-adaptive       | yes                     |
| hu   | Hungarian  | speechmatics   | STT-adaptive       | yes                     |
| id   | Indonesian | deepgram       | semantic (LiveKit) | yes                     |
| it   | Italian    | deepgram       | semantic (LiveKit) | yes                     |
| ja   | Japanese   | deepgram       | semantic (LiveKit) | yes                     |
| kn   | Kannada    | deepgram       | VAD only           | no                      |
| ko   | Korean     | deepgram       | semantic (LiveKit) | yes                     |
| mr   | Marathi    | speechmatics   | STT-adaptive       | no                      |
| ms   | Malay      | speechmatics   | STT-adaptive       | yes                     |
| nl   | Dutch      | deepgram       | semantic (LiveKit) | yes                     |
| no   | Norwegian  | speechmatics   | STT-adaptive       | yes                     |
| pl   | Polish     | speechmatics   | STT-adaptive       | yes                     |
| pt   | Portuguese | deepgram       | semantic (LiveKit) | yes                     |
| ro   | Romanian   | speechmatics   | STT-adaptive       | yes                     |
| ru   | Russian    | deepgram       | semantic (LiveKit) | yes                     |
| sk   | Slovak     | speechmatics   | STT-adaptive       | yes                     |
| sv   | Swedish    | speechmatics   | STT-adaptive       | yes                     |
| ta   | Tamil      | speechmatics   | STT-adaptive       | yes                     |
| te   | Telugu     | deepgram       | VAD only           | no                      |
| th   | Thai       | speechmatics   | STT-adaptive       | no                      |
| tl   | Tagalog    | speechmatics   | STT-adaptive       | yes                     |
| tr   | Turkish    | deepgram       | semantic (LiveKit) | yes                     |
| uk   | Ukrainian  | speechmatics   | STT-adaptive       | yes                     |
| vi   | Vietnamese | speechmatics   | STT-adaptive       | yes                     |
| zh   | Chinese    | deepgram       | semantic (LiveKit) | yes                     |

Provider capability sets (for BYO validation):

- Deepgram STT: all 39.
- Speechmatics STT: 36 (all except gu, kn, te).
- Cartesia TTS: all 39.
- ElevenLabs TTS: 32 (all except bn, gu, he, kn, te, th, mr).

## Components

### 1. `core/hailhq/core/languages.py` (new)

Single source of truth. A frozen mapping
`SUPPORTED_LANGUAGES: dict[str, LanguageCaps]` where `LanguageCaps` is a
frozen dataclass: `name`, `stt: frozenset[str]` (of
`{"deepgram", "speechmatics"}`), `tts: frozenset[str]` (of
`{"cartesia", "elevenlabs"}`), `semantic_turn: bool` (in LiveKit's 14).

Helpers:

- `default_stt_for(lang: str | None) -> str` — `None` or semantic-turn
  language → `"deepgram"`; else `"speechmatics"` when it supports the
  language, else `"deepgram"`.
- `tts_providers_for(lang: str | None) -> frozenset[str]` — both providers
  when `lang` is `None`.
- `has_semantic_turn(lang: str | None) -> bool` — `True` for `None`
  (English default).

Docstring records the research date and source URLs.

### 2. Schemas (`core/hailhq/core/schemas.py`, `core/hailhq/core/provider_config.py`)

- `VoiceConfig.language`: enum of the 39 codes (Literal built from
  `languages.py` keys, or an `enum.StrEnum` — implementation's choice, but
  the OpenAPI output must be a proper `enum`). `None` default unchanged.
- `VoiceConfig.stt`: `Literal["auto", "deepgram", "speechmatics"]`,
  default `"auto"`.
- `STTParams.provider`: `Literal["deepgram", "speechmatics"]`.

### 3. Voicebot (`voicebot/hailhq/voicebot/pipeline.py`, `agent.py`, deps, Dockerfile)

- New dep: `livekit-plugins-speechmatics`; new extra
  `livekit-agents[turn-detector]`; MultilingualModel prewarmed alongside
  Silero in `prewarm` and baked into the Docker image (`.models` download
  step).
- `build_stt` gains a Speechmatics branch (house key
  `settings.speechmatics_api_key`; BYO org rows with
  `provider == "speechmatics"`, fallback semantics identical to the
  Deepgram branch). Speechmatics instances are built with
  `operating_point="enhanced"`, `language=<lang>`, and, when it drives
  turn detection, `turn_detection_mode=ADAPTIVE`.
- STT resolution: explicit `voice_config.stt` pins the provider; `"auto"`
  calls `default_stt_for(language)`.
- `build_session` sets `turn_detection`:
  - semantic-turn language (or no language) → `MultilingualModel()`;
  - Speechmatics-routed language → `"stt"`;
  - else → `"vad"`.
- `_house_tts` filters instances by `tts_providers_for(language)` so the
  7 Cartesia-only languages never get an ElevenLabs fallback instance.
  BYO TTS is unchanged (API-side validation guarantees the combo).
- STT fallback nuance: when a Speechmatics-routed call has
  `fallback_enabled`, the Deepgram fallback instance is still valid for
  transcription, but turn detection stays `"stt"` for the session — a
  failover call degrades turn-taking; acceptable, documented.

### 4. API (`api/`, `core/hailhq/core/provider_validation.py`)

- Call-creation route: after resolving org BYO config, reject with 422 if
  the requested `language` is unsupported by the pinned/routed STT provider
  or by the org's BYO TTS provider. Error message lists the supported codes
  for that combination.
- `provider_validation.py`: Speechmatics probe (cheap authenticated GET
  against their management API), following the existing per-provider probe
  pattern.
- Config: `speechmatics_api_key: str = ""` in `core/config.py`;
  `SPEECHMATICS_API_KEY` added to `.env.example` under a Speechmatics
  section in the same commit. **Deploy TODO: set the real key in the prod
  VM `.env` before deploying.**
- Regenerate `openapi/openapi.yaml` in the same PR.

### 5. CLI / SDK / MCP

- CLI: regen `client.gen.go` from the updated spec; add `--stt` flag to
  `hail call` next to the existing `--language`.
- SDK (`sdk/hail/models.py`, `client.py`): language enum + `stt` field on
  the voice-config model.
- MCP (`mcp/hailhq/mcp/tools.py`): `place_call` schema gains the language
  enum values and `stt` param; tool description states the auto-routing in
  one line.

### 6. Docs

- New `docs/languages.md`: the 39-language table above (code, name, STT
  route, turn-detection tier, ElevenLabs fallback), the exclusion rule,
  and provider source links. Lead with a runnable `hail call --language da`
  example (agent-first tenet).
- README: short "Languages" section linking to `docs/languages.md`.
- Setup docs: `SPEECHMATICS_API_KEY` mention where the other provider keys
  are documented.

### 7. Console (hail-website, separate PR)

`app/console/calls/providers/`: add Speechmatics to the STT layer's
provider choices (API key + `operating_point` param select,
enhanced default), mirroring the ElevenLabs BYO drawer pattern; key
validated via the new probe through the existing validation endpoint.

## Error handling

- Unsupported `language` code → 422 at request time (enum violation).
- Supported code but incompatible with org BYO provider or pinned `stt`
  → 422 with the valid codes for that combination.
- Voicebot-side: missing `SPEECHMATICS_API_KEY` for a Speechmatics-routed
  house call → `ProviderKeyError` → `end_reason='provider_key_error'`
  (existing pattern). BYO Speechmatics row without key and fallback
  disabled → same.

## Testing

- `core`: unit tests for `languages.py` helpers (routing table cases:
  semantic-turn lang, Speechmatics lang, VAD-only lang, `None`).
- `voicebot`: extend `tests/test_pipeline.py` — STT branch per provider,
  auto vs pinned `stt`, turn_detection selection per language tier,
  ElevenLabs trimmed for Cartesia-only languages, BYO Speechmatics with
  and without fallback.
- `api`: 422 cases (bad code, BYO-elevenlabs + `th`, pinned
  speechmatics + `gu`).
- CLI/SDK/MCP: existing round-trip tests extended with `language` +
  `stt`.

## Out of scope

- SMART_TURN mode (v2; needs `speechmatics-voice[smart]`).
- Costs surface (`web/`) Speechmatics pricing rows — optional follow-up.
- Cartesia STT (ink-2/ink-whisper) as a provider.
- Regional variants (en-US vs en-GB) — base ISO 639-1 codes only.
- Auto language detection (caller must specify the language).

## Amendment 2026-07-30

Owner decision: per-call STT provider pinning is removed. STT provider
selection happens **only** via the org's console BYO row. Routing
becomes: org BYO STT row > language auto-route (the per-call pin layer
above it is gone; the `stt_choice`/pin parameter is deleted from
`resolve_stt_provider` and `build_session`).

This supersedes:

- Section 2's `voice_config.stt` field (the `VoiceConfig.stt` Literal
  described above) — deleted from the public schema. A client that still
  sends `voice_config.stt` gets a clean 422 via `extra="forbid"`.
- The CLI's `--stt` flag, the SDK's `Client.calls.create(stt=...)`
  parameter, and the MCP `place_call` tool's `stt` parameter — all
  removed from their respective public surfaces.

Unchanged by this amendment: the `tts`/`vad`/`turn_detection` descriptor
fields on `VoiceConfig`; the auto-degrade behavior for org-speechmatics +
unsupported language (warn + house deepgram — still an open product
question); the `language` enum; all turn-detection logic.
