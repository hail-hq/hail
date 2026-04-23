# Architecture

Hail v1 is three Python services + a Go CLI, wrapped around LiveKit Cloud.

```
 AI agent                                     Hail                                LiveKit Cloud         PSTN
(caller)  ─────MCP URL──►  Hail MCP ─HTTP─►  Hail API  ◄────►  SIP+WebRTC  ◄────► Twilio ◄────► 📞
                          (SSE :8081)      (FastAPI :8080)
                                                 │
                                                 └─dispatch──►  Hail voicebot  (LiveKit Agents worker)
                                                                     │
                                                                     ├─ VAD:   Silero
                                                                     ├─ STT:   Deepgram
                                                                     ├─ LLM:   fallback(OpenAI → Gemini → Anthropic)
                                                                     │        or caller-provided endpoint
                                                                     └─ TTS:   ElevenLabs
```

## Services

- **api** (`:8080`, FastAPI) — REST surface; accepts `POST /calls` etc. Source of truth for OpenAPI.
- **mcp** (`:8081`, SSE) — MCP server wrapping the API; what agent clients (Claude.ai, ChatGPT, Claude Code, Cursor) connect to. See [docs/setup/mcp.md](setup/mcp.md).
- **voicebot** (LiveKit Agents worker) — registers with LiveKit Cloud; dispatched into a room per call.
- **postgres** — call records, phone numbers, API keys.
- **minio** (dev only) — S3-compatible local object storage. Swap for real S3 in prod.

LiveKit Cloud is external. The `hail` Go CLI is a human-facing scriptable tool, not a service.

## Outbound call flow

1. Caller (agent via MCP, or CLI, or direct HTTP) → `POST /calls` with `{to, from, first_message?, …llm}`.
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
