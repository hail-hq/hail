# Direct TTS Voices Implementation Plan

**Goal** — Add Gemini, xAI, and AssemblyAI speech to Hail and make Charon the default for inherited English voices.

**Architecture** — Keep voice and credential settings in the existing provider rows. Use LiveKit's direct provider plugins for call synthesis and the existing signed API endpoint for samples. Configure Gemini on Vertex AI with a Google Cloud project, or use the existing Gemini API key. Keep direct call synthesis for Vertex credentials the plugin cannot accept.

[Voice audition and rollout plan](https://chatgpt.com/space/page_dcec800738c88191bdefd7c8e9232056)

## Backend

- [x] Add regression tests for provider routing, legacy explicit selections, authentication, and advertised voices.
- [x] Connect the Gemini, xAI, and AssemblyAI plugins to the existing TTS pipeline. Keep provider keys and setup in core. Use the current LiveKit Agents minor series and a compatible RTC patch.
- [x] Bound the audio wait in the native Gemini and xAI streams. Keep xAI word boundaries and honor configured fallback voice IDs.
- [x] Add BYO parameter validation and keep the existing fallback policy. Show Charon, Ara, and Audrey when configured for English. Keep current non-English defaults.

## Console and release

- [x] Update provider settings, previews, and the shared voice picker. Save an explicit voice only when the user chooses one.
- [x] Update the changelog and verify the rollout copy against the implementation.
- [x] Verify Gemini API key routing and Vertex credential isolation. Use the documented xAI and AssemblyAI key names. Check the latest packages for fixes before changing pins.
- [ ] Review saved-line playback and caching separately.
- [ ] Verify the backend before the console. Measure direct synthesis and real call latency before enabling the production default.
