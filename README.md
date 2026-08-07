# Hail

**Give your agent a real phone number and an email inbox — in minutes, not days.**

[![License: AGPL v3](https://img.shields.io/github/license/hail-hq/hail)](./LICENSE)
[![PyPI — hail-sdk](https://img.shields.io/pypi/v/hail-sdk?label=hail-sdk)](https://pypi.org/project/hail-sdk/)
[![CLI release](https://img.shields.io/github/v/release/hail-hq/hail?label=hail%20CLI)](https://github.com/hail-hq/hail/releases)
[![Docs](https://img.shields.io/badge/docs-hail.so%2Fdocs-blue)](https://hail.so/docs)

Your agent needs to call a person to move an appointment. Hail connects to the telephone carrier and runs the voice pipeline — STT, TTS, turn detection. Your agent is the brain: point Hail at any OpenAI-compatible endpoint ([bring your own LLM](docs/public/byo-llm.md)), or let Hail's fallback chain (OpenAI → Gemini → Anthropic) do the talking. SMS and email work the same way — one MCP endpoint, one API key, one invoice.

Self-hostable with `docker compose up`. Open source under AGPLv3.

![Animated terminal demo of hail tail streaming live call events](docs/assets/gifs/hail-tail-live-stream.gif)

## Quick start

```bash
git clone https://github.com/hail-hq/hail
cd hail
cp .env.example .env   # add Twilio, LiveKit Cloud, Deepgram, Cartesia keys
                       # + one of OpenAI / Gemini / Anthropic
docker compose up
```

Then get an API key:

- **Hail Cloud** (managed, at [hail.so](https://hail.so)): run `hail login`. The device flow writes a key to `~/.hail/credentials.json`.
- **Self-host**: seed a key into your database — see [operations.md, "First-run DB seed"](docs/public/operations.md) — then set `HAIL_API_KEY` or pass `--api-key`.

Full setup guides: [Twilio](docs/public/setup/twilio.md) · [LiveKit Cloud](docs/public/setup/livekit-cloud.md) · [AWS SES](docs/public/setup/aws-ses.md) · [Webhooks](docs/public/setup/webhooks.md) · [MCP](docs/public/setup/mcp.md)

## Make your first call

**CLI** ([GitHub Releases](https://github.com/hail-hq/hail/releases)):

```bash
hail call +14155550100 --prompt "be brief"
hail call status <id>             # one call's state
hail call tail <id>               # follow events live

hail sms +15551234567 --body "Hello!" --recipient-consent
hail email send --to a@b.com --subject hi --body "hello"
hail email domain register --kind custom --domain acme.com   # send + receive on your own domain

printf '%s' "$YOUR_API_KEY" | hail providers set llm \
  --provider openai-compatible \
  --base-url https://api.your-agent.dev/v1 \
  --key -                         # standing BYO brain (also: tts, stt)

hail tail                         # cross-channel event stream
```

Run `hail --help` for the full surface: numbers, contacts, suppressions, email stats/events/attachments, provider test/activate, shell completion.

**Python** (`pip install hail-sdk`):

```python
import asyncio
from hail import Client

async def main():
    async with Client() as client:  # reads $HAIL_API_KEY
        call = await client.calls.create(
            to="+15551234567",
            system_prompt="You are calling to confirm a reschedule.",
        )
        async for event in client.events.tail(id=f"call:{call.id}"):
            print(event.kind, event.payload)

asyncio.run(main())
```

**HTTP** ([OpenAPI spec](openapi/openapi.yaml), [API reference](https://hail.so/docs/api)):

```bash
curl -X POST http://localhost:8080/calls \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -d '{"to":"+15551234567","system_prompt":"..."}'
```

**MCP** (Claude.ai, Claude Code, ChatGPT, Cursor, …): add a remote MCP connector pointing at `http://<your-host>:8081` (self-hosted). See [setup/mcp.md](docs/public/setup/mcp.md).

## Bring your own LLM

Hail always runs the telephony and the voice pipeline. The brain is pluggable, at two levels:

- **Per call** — pass an `llm` block to `POST /calls`; different brains for different calls.
- **Standing** — save an endpoint once (`hail providers set llm …`); every call your org places uses it.

Any OpenAI chat-completions-compatible endpoint works. A complete runnable example lives in [docs/public/byo-llm.md](docs/public/byo-llm.md). TTS and STT are pluggable the same way (`hail providers set tts|stt …`).

## Tenets

1. **Clear comms.** Explicit OpenAPI contracts. No hidden behavior.
2. **Simple code.** Boring is best. No abstraction before it has two uses.
3. **Brief docs.** Each page fits on one screen. Setup takes 10 minutes from a fresh clone.
4. **Self-hostable.** `docker compose up` runs everything except LiveKit Cloud.
5. **Pluggable brain.** [BYO LLM endpoint](docs/public/byo-llm.md), or Hail's bundled fallback. The voice pipeline and transport are always Hail's.
6. **Agent-first docs.** AI agents are first-class readers. Runnable examples first; links to canonical sources, not paraphrase.

## Milestones

A checked box is a released feature. Per-artifact changelogs (GitHub Releases for the CLI, PyPI notes for the SDK) record which version shipped it.

### Phone calls

- Outbound
  - [x] Twilio
  - [ ] Telnyx
- Inbound
  - [ ] Twilio

### SMS

- Outbound
  - [x] Twilio
- Inbound
  - [x] Twilio

### Email

- Outbound
  - [x] AWS SES
  - [x] Custom sender domains (own DNS, automatic DKIM + MAIL FROM)
- Inbound
  - [x] AWS SES
  - [x] Custom domains (receive on verified domains)

### Voice pipeline

- Languages
  - [x] 39 call languages with automatic STT routing and per-language turn detection — see [docs/languages.md](docs/languages.md)
- STT
  - [x] Deepgram
  - [x] Speechmatics
  - [ ] Whisper
  - [ ] AssemblyAI
- TTS
  - [x] Cartesia
  - [x] ElevenLabs
  - [ ] Deepgram Aura
- VAD
  - [x] Silero
- Turn detection
  - [x] LiveKit turn-detector
- LLM — system-prompt mode
  - [x] Fallback: OpenAI → Gemini → Anthropic, fast models
- LLM — BYO-endpoint mode
  - [x] OpenAI chat-completions-compatible ([docs](docs/public/byo-llm.md))
- Recording
  - [ ] S3 upload
  - [ ] Diarization

### Distribution

- API
  - [x] OpenAPI spec + [hosted reference](https://hail.so/docs/api)
- CLI
  - [x] `hail` binary via GitHub Releases
- MCP server
  - [x] Remote Streamable HTTP endpoint included with each Hail deployment
  - ~~PyPI stdio package~~ — deliberately not shipped; see [setup/mcp.md](docs/public/setup/mcp.md)
- Python SDK
  - [x] `hail-sdk` on PyPI, imports as `hail`

### Infrastructure

- [x] Docker Compose scaffold
- Self-hosted LiveKit SFU
  - [ ] docker compose integration

## Architecture

The path of an outbound call:

```
AI agent ──► Hail API ──dispatch──► Voicebot ──► LiveKit Cloud ──SIP──► Twilio ──► 📞
```

Full diagram and service breakdown: [docs/public/architecture.md](docs/public/architecture.md). All docs are published at [hail.so/docs](https://hail.so/docs) and live as plain markdown in [docs/public/](docs/public/).

## Contributing

See [docs/public/contributing.md](docs/public/contributing.md). Short version: fork, branch, conventional commits, pull request. Provider adapters go in `core/hailhq/core/providers/`; new env vars update `.env.example` in the same commit.

## License

Code: [AGPL-3.0-or-later](./LICENSE) — run a modified Hail as a service, release your source.
Pricing dataset (`costs/`): [CC-BY-4.0](./costs/LICENSE) — use the JSON with attribution.
