# Direct TTS Voices Implementation Plan

**Goal** — Add Gemini, xAI, and AssemblyAI speech to Hail and make Charon the default for inherited English voices.

**Architecture** — Keep voice and credential settings in the existing provider rows. Add one core adapter per vendor and use the existing call and sample paths. Gemini uses Google Cloud Agent Platform with Hail's application credentials or a customer project and key.

[Voice audition and rollout plan](https://chatgpt.com/space/page_dcec800738c88191bdefd7c8e9232056)

## Backend

- [x] Add regression tests for provider routing, legacy explicit selections, authentication, and advertised voices.
- [x] Add Gemini, xAI, and AssemblyAI adapters in core. Connect them to the existing TTS pipeline and catalogue sample endpoint.
- [x] Add BYO parameter validation and keep the existing fallback policy. Show Charon, Ara, and Audrey when configured for English. Keep current non-English defaults.

## Console and release

- [x] Update provider settings, previews, and the shared voice picker. Save an explicit voice only when the user chooses one.
- [x] Update the changelog and verify the rollout copy against the implementation.
- [ ] Review saved-line playback and caching separately.
- [ ] Verify the backend before the console. Measure direct synthesis and real call latency before enabling the production default.
