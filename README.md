# Hail

> Universal communication platform for AI agents.
> Phone calls, SMS, email — outbound first, inbound next. Self-hostable. Open source (AGPLv3).

Your agent wants to place a call: *"Call +1… and ask if they want to reschedule."* Hail does the carrier glue, runs the voice pipeline, and lets the agent plug in its own brain (or fall back to OpenAI → Gemini → Claude).

## Quickstart

```bash
git clone https://github.com/hail-hq/hail
cd hail
cp .env.example .env
# fill in Twilio, LiveKit Cloud, Deepgram, ElevenLabs, and one of OpenAI / Gemini / Anthropic
docker compose up
```

Use it:

```bash
# CLI (for humans scripting Hail)
hail call +15551234567 --prompt "You are calling to confirm a reschedule."

# HTTP
curl -X POST http://localhost:8080/calls \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -d '{"to":"+15551234567","system_prompt":"..."}'

# MCP (for AI agents — Claude.ai, ChatGPT, Claude Code, Cursor, …)
# Add a remote MCP connector in your client pointing at:
#   http://<your-host>:8081/sse    (self-hosted)
#   https://mcp.hail.so/sse        (Hail Cloud, later)
```

Full setup: [docs/setup/twilio.md](docs/setup/twilio.md), [docs/setup/livekit-cloud.md](docs/setup/livekit-cloud.md), [docs/setup/mcp.md](docs/setup/mcp.md).

## Tenets

1. **Clear comms.** Explicit OpenAPI contracts. No magic.
2. **Simple code.** Boring is best. No abstractions without two uses.
3. **Brief docs.** One screen per page. Setup ≤ 10 minutes from a fresh clone.
4. **Self-hostable.** `docker compose up` runs everything except LiveKit Cloud.
5. **Pluggable brain.** BYO endpoint compatible with OpenAI's completions API, or use Hail's bundled fallback (OpenAI → Gemini → Anthropic). Voice pipeline + transport are always Hail's.

## Milestones

Legend: `[x]` done · `[~]` in progress · `[ ]` todo · `[-]` future.

### Phone calls

- Outbound
  - [~] Twilio (v1)
  - [ ] Telnyx (v2)
- Inbound
  - [ ] Twilio (v1.1)

### SMS

- Outbound
  - [ ] Twilio (v1.2)
- Inbound
  - [ ] Twilio (v1.3)

### Email

- Outbound
  - [ ] AWS SES (v1.4)
- Inbound
  - [ ] AWS SES (v1.5)

### Voice pipeline

- STT
  - [~] Deepgram (v1)
  - [ ] Whisper (v1.2)
  - [ ] AssemblyAI (v1.2)
- TTS
  - [~] ElevenLabs (v1)
  - [ ] Cartesia (v1.2)
  - [ ] Deepgram Aura (v1.2)
- VAD
  - [~] Silero (v1)
- Turn detection
  - [~] LiveKit turn-detector (v1)
- LLM — system-prompt mode
  - [~] Fallback: OpenAI → Gemini → Anthropic, fast models (v1)
- LLM — BYO-endpoint mode
  - [~] OpenAI chat-completion-compatible (v1)
- Recording
  - [ ] S3 upload (v1)
  - [ ] Diarization (v1.2)

### Distribution

- API
  - [~] OpenAPI spec (v1)
- CLI
  - [~] `hail` binary via GitHub Releases (v1)
- MCP server
  - [~] Remote SSE endpoint bundled with every Hail deploy (v1)
  - [-] PyPI stdio package — intentionally not shipped; see [docs/setup/mcp.md](docs/setup/mcp.md)

### Infrastructure

- [x] Docker Compose scaffold
- Self-hosted LiveKit SFU
  - [ ] docker compose integration (v1.6)

## Architecture

```
AI agent ──► Hail API ──dispatch──► Voicebot ──► LiveKit Cloud ──SIP──► Twilio ──► 📞
```

Full diagram: [docs/architecture.md](docs/architecture.md).

## Contributing

See [docs/contributing.md](docs/contributing.md). TL;DR: fork, branch, conventional-commit, PR. Provider adapters go in `core/hail/core/providers/`. Update `.env.example` for any new env var.

## License

[AGPLv3](./LICENSE). If you run a modified Hail as a service, you must release your source.
