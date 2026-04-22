# Architecture

Hail v1 is three Python deployables + a Go CLI, wrapped around LiveKit Cloud.

```
 AI agent          Hail API          LiveKit Cloud         PSTN
(caller) ─────►  (FastAPI)  ◄───►  (SIP + WebRTC)  ◄───► Twilio ◄───► 📞
                    │
                    └─dispatch──►  Hail voicebot  (LiveKit Agents worker)
                                        │
                                        ├─ VAD:   Silero
                                        ├─ STT:   Deepgram
                                        ├─ LLM:   fallback(OpenAI → Gemini → Anthropic)
                                        │        or caller-provided endpoint
                                        └─ TTS:   ElevenLabs
```

## Outbound call flow

1. Caller → `POST /calls` with `{to, from, first_message?, …llm}`.
2. Hail API creates a LiveKit room and dispatches the voicebot into it.
3. Voicebot joins; LiveKit places a SIP outbound via the Twilio trunk to `to`.
4. On pickup, voicebot speaks `first_message` (if set), then runs the STT → LLM → TTS loop.
5. On hangup, voicebot writes the call record to Postgres and uploads the recording to S3.

## LLM modes

**A — system prompt (default).** Caller supplies `system_prompt`. Voicebot uses LiveKit's `FallbackAdapter` chaining `openai.LLM` → `google.LLM` → `anthropic.LLM` (fast models each). Falls through on error.

**B — BYO endpoint.** Caller supplies `llm: { base_url, api_key, model }`. Voicebot points `openai.LLM` at that endpoint. No fallback.

One mode per call.

## Data

- **Postgres** — call records, phone numbers, API keys.
- **S3** — call recordings.
- **LiveKit Cloud** — transient media (ephemeral).
