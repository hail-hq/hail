# Hail

**Give your agent a real phone number and an email inbox — in minutes, not days.**

[![License: AGPL v3](https://img.shields.io/github/license/hail-hq/hail)](./LICENSE)
[![PyPI — hail-sdk](https://img.shields.io/pypi/v/hail-sdk?label=hail-sdk)](https://pypi.org/project/hail-sdk/)
[![CLI release](https://img.shields.io/github/v/release/hail-hq/hail?label=hail%20CLI)](https://github.com/hail-hq/hail/releases)
[![Docs](https://img.shields.io/badge/docs-hail.so%2Fdocs-blue)](https://hail.so/docs)

Your agent needs to call a person to move an appointment. Hail connects to the telephone carrier and runs the voice pipeline — STT, TTS, turn detection. Your agent is the brain: point Hail at any OpenAI-compatible endpoint ([bring your own LLM](docs/public/byo-llm.md)), or let Hail's fallback chain (OpenAI → Gemini → Anthropic) do the talking. SMS and email work the same way — one MCP endpoint, one API key, one invoice.

Self-hostable with Docker Compose; LiveKit Cloud and the communication
providers remain external. Open source under AGPLv3.

![Animated terminal demo of hail tail streaming live call events](docs/assets/gifs/hail-tail-live-stream.gif)

## Self-host quick start

Prerequisites: Git, Docker Engine, and Docker Compose v2. For a public
production deployment you also need a domain, HTTPS, and a managed Postgres;
start with the [VM deployment guide](docs/public/self-host/vm-deploy.md).

The commands below run a local evaluation stack with bundled Postgres and
MinIO. Voice calls additionally require LiveKit Cloud, Twilio, Deepgram,
Cartesia, and at least one LLM provider. Email is optional and requires AWS SES.

```bash
git clone https://github.com/hail-hq/hail
cd hail
cp .env.example .env

# Generate a shared self-host key, then put it in .env as HAIL_API_KEY.
printf 'hk_%s\n' "$(openssl rand -base64 32 | tr -d '/+=' | head -c 40)"

# Edit .env and add the providers required for the channels you will use.
docker compose -f docker-compose.yml -f docker-compose.local.yml \
  run --rm api alembic upgrade head
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
docker compose -f docker-compose.yml -f docker-compose.local.yml ps
curl --fail http://localhost:8080/healthz
```

Self-host authentication uses the `HAIL_API_KEY` value from `.env`; it does not
need an API-key row in Postgres. Export the same key and API URL in the shell
where you use the CLI or SDK (Compose does not export `.env` into your shell):

```bash
export HAIL_API_URL=http://localhost:8080
export HAIL_API_KEY='<same value as .env>'
```

Next, follow [LiveKit Cloud](docs/public/self-host/livekit-cloud.md) and
[Twilio](docs/public/self-host/twilio.md), then bind a phone number to the
self-host organization using the
[first-run setup](docs/public/self-host/operations.md#self-host-first-run-setup).
To enable email, follow [AWS SES](docs/public/self-host/aws-ses.md).

Authentication differs by deployment:

- **Hail Cloud** (managed, at [hail.so](https://hail.so)): run `hail login`. The device flow writes a key to `~/.hail/credentials.json`.
- **Self-host**: do not run `hail login`; set `HAIL_API_URL` and `HAIL_API_KEY` as shown above, or pass `--api-url` and `--api-key`.

Full setup guides: [self-hosting](docs/public/self-host/README.md) · [Webhooks](docs/public/webhooks.md) · [MCP](docs/public/mcp.md) · [operations](docs/public/self-host/operations.md)

## Make your first call

**CLI** ([install a binary from GitHub Releases](https://github.com/hail-hq/hail/releases)):

```bash
hail login                        # Hail Cloud only (device flow)
hail auth logout                  # remove local credentials
hail auth token                   # print bare API key for scripting

hail call +14155550100 --prompt "be brief" --recipient-consent
hail call list
hail call status <id>             # one call's state
hail call tail <id>               # follow events for one call

hail sms +15551234567 --body "Hello!" --recipient-consent
hail sms list
hail sms status <id>
hail sms suppressions list        # opt-out list
hail sms sender-id get            # custom sender ID

hail numbers acquire              # dedicated phone number (voice + SMS)
hail numbers list
hail contacts list                # org contact directory

hail email send --to a@b.com --subject hi --body "hello" --recipient-consent
hail email list
hail email get <id>
hail email tail <id>              # follow events for one email
hail email raw <id>               # RFC 5322 source
hail email attachment <id> <att-id> --output file.pdf
hail email domain register --kind hail_mail
hail email domain register --kind custom --domain acme.com  # send + receive on your own domain
hail email domain list

printf '%s' "$YOUR_API_KEY" | hail providers set llm \
  --provider openai-compatible \
  --base-url https://api.your-agent.dev/v1 \
  --model your-model \
  --key -                         # standing BYO brain (also: tts, stt)

hail tail                         # cross-channel event stream
hail tail call:<id>               # narrow by resource type

hail mcp endpoint                 # Streamable HTTP URL for the MCP server
hail completion zsh               # source <(hail completion zsh)
hail version
```

**Python** (`pip install hail-sdk`):

```python
import asyncio
from hail import Client

async def main():
    async with Client() as client:  # reads $HAIL_API_KEY
        call = await client.calls.create(
            to="+15551234567",
            recipient_consent=True,
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
  -H "Content-Type: application/json" \
  -d '{"to":"+15551234567","recipient_consent":true,"system_prompt":"..."}'
```

**MCP** (Claude.ai, Claude Code, ChatGPT, Cursor, …): local clients can use
`http://localhost:8081`. Web-based clients require a publicly reachable HTTPS
endpoint. See the [MCP setup guide](docs/public/mcp.md).

## Bring your own LLM

Hail always runs the telephony and the voice pipeline. The brain is pluggable, at two levels:

- **Per call** — pass an `llm` block to `POST /calls`; different brains for different calls.
- **Standing** — save an endpoint once (`hail providers set llm …`); every call your org places uses it.

Any OpenAI chat-completions-compatible endpoint works. A complete runnable example lives in [docs/public/byo-llm.md](docs/public/byo-llm.md). TTS and STT are pluggable the same way (`hail providers set tts|stt …`).

## Tenets

1. **Clear comms.** Explicit OpenAPI contracts. No hidden behavior.
2. **Simple code.** Boring is best. No abstraction before it has two uses.
3. **Brief docs.** Each page fits on one screen. Setup takes 10 minutes from a fresh clone.
4. **Self-hostable.** Docker Compose runs Hail's API, voicebot, MCP server,
   Postgres, and MinIO; LiveKit Cloud and channel providers remain external.
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
  - ~~PyPI stdio package~~ — deliberately not shipped; see [MCP setup](docs/public/mcp.md)
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
