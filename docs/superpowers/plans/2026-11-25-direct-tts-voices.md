# Direct TTS Voices Implementation Plan

**Goal** — Add Gemini, xAI, and AssemblyAI speech to Hail. Use Charon as the initial inherited English voice, with the same override precedence as the current Cartesia default.

The call worker and picker both read `GEMINI_VOICE_ID`. An operator can change that value to swap the default. Agents with explicit voice choices keep them.

**Architecture** — Keep voice and credential settings in the existing provider rows. Use LiveKit's direct provider plugins for call synthesis and the existing signed API endpoint for samples. Hail uses Vertex AI when a Google Cloud project is configured. Workspaces supply Gemini API keys through the existing provider settings.

## Backend

- [x] Add regression tests for provider routing, legacy explicit selections, authentication, and advertised voices.
- [x] Construct Gemini, xAI, and AssemblyAI in the existing LiveKit TTS pipeline.
- [x] Keep xAI word boundaries and honor configured fallback voice IDs.
- [x] Add BYO parameter validation and keep the existing fallback policy. Load Gemini and xAI voices from each provider's voice list; keep one saved default per provider. Offer each provider in the languages its docs list. Only English calls inherit Gemini.

## Console and release

- [x] Update provider settings, previews, and the shared voice picker. Save an explicit voice only when the user chooses one.
- [x] Update the changelog and verify the rollout copy against the implementation.
- [x] Verify Gemini API key routing and Vertex credential isolation. Use the documented xAI and AssemblyAI key names. Check the latest packages for fixes before changing pins.
- [ ] Review saved-line playback and caching separately.
- [ ] Verify the backend before the console. Measure synthesis and real call latency before release.
