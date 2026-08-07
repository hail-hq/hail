# Hail

> Hail is a communication platform for AI agents.
> Agents can make telephone calls. Agents can send and receive SMS messages and email. Inbound telephone calls will follow. You can host Hail on your own servers. The source code is open (AGPLv3).

An example: your agent must call a person to change an appointment. Hail connects to the telephone carrier and operates the voice pipeline. The agent can supply its own LLM. If the agent does not supply an LLM, Hail uses a fallback sequence: OpenAI, then Gemini, then Claude.

## Quick start

Do these steps:

1. Get the source code and go into the directory:

   ```bash
   git clone https://github.com/hail-hq/hail
   cd hail
   ```

2. Copy the example configuration file:

   ```bash
   cp .env.example .env
   ```

3. Add your credentials to `.env` for Twilio, LiveKit Cloud, Deepgram, and Cartesia. Add credentials for one of OpenAI, Gemini, or Anthropic.

4. Start the stack:

   ```bash
   docker compose up
   ```

Then get an API key. There are two procedures:

- **Hail Cloud** (managed at hail.so): Run `hail login`. The command starts the device flow. It writes an API key to `~/.hail/credentials.json`.
- **Self-host**: Put an API key into your local database. Refer to [docs/public/self-host/operations.md](docs/public/self-host/operations.md), section "First-run DB seed". Then set the environment variable `HAIL_API_KEY`, or give the `--api-key` option.

Use the CLI (for persons who write scripts for Hail):

```bash
hail login                        # authenticate (device flow)
hail auth logout                  # remove local credentials
hail auth token                   # print bare API key for scripting

hail call +14155550100 --prompt "be brief"
hail call list
hail call tail <id>               # follow events for one call

hail sms +15551234567 --body "Hello!" --recipient-consent
hail sms list
hail sms status <id>
hail sms suppressions list        # opt-out list
hail sms sender-id get            # custom sender ID

hail numbers acquire              # dedicated phone number (voice + SMS)
hail numbers list
hail contacts list                # org contact directory

hail email send --to a@b.com --subject hi --body "hello"
hail email list
hail email get <id>
hail email tail <id>              # follow events for one email
hail email raw <id>               # RFC 5322 source
hail email attachment <id> <att-id> --output file.pdf
hail email domain register --kind hail_mail
hail email domain register --kind custom --domain acme.com  # send + receive on your own domain
hail email domain list

hail tail                         # cross-channel event stream
hail tail call:<id>               # narrow by resource type

hail mcp endpoint                 # Streamable HTTP URL for the MCP server
hail completion zsh               # source <(hail completion zsh)
hail version
```

Or use HTTP or MCP:

```bash
# HTTP
curl -X POST http://localhost:8080/calls \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -d '{"to":"+15551234567","system_prompt":"..."}'

curl -X POST http://localhost:8080/sms \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -d '{"to":"+15551234567","body":"hello","recipient_consent":true}'

curl -X POST http://localhost:8080/emails \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -d '{"to":["alice@example.com"],"subject":"hi","body_text":"hello"}'

# MCP (for AI agents — Claude.ai, ChatGPT, Claude Code, Cursor, …)
# Add a remote MCP connector in your client pointing at:
#   http://<your-host>:8081    (self-hosted)
#   https://mcp.hail.so        (Hail Cloud, later)
```

This is `hail tail` in operation:

![Animated terminal demo of hail tail streaming live call events](docs/assets/gifs/hail-tail-live-stream.gif)

For the full setup, refer to [docs/public/self-host/twilio.md](docs/public/self-host/twilio.md), [docs/public/self-host/livekit-cloud.md](docs/public/self-host/livekit-cloud.md), [docs/public/self-host/aws-ses.md](docs/public/self-host/aws-ses.md), and [docs/public/mcp.md](docs/public/mcp.md).

## Tenets

1. **Clear communication.** The API has explicit OpenAPI contracts. There is no hidden behavior.
2. **Simple code.** Simple solutions are best. We do not add an abstraction before it has two uses.
3. **Short documents.** Each page is one screen. Setup from a new clone takes 10 minutes or less.
4. **Self-hostable.** The command `docker compose up` starts all the services. Only LiveKit Cloud is external.
5. **Pluggable brain.** Connect your own LLM endpoint that is compatible with the OpenAI completions API. As an alternative, use the fallback sequence from Hail: OpenAI, then Gemini, then Anthropic. Hail always supplies the voice pipeline and the transport.
6. **Agent-first documents.** AI agents are primary readers. Each page starts with an example that you can run. Pages give links to canonical sources (OpenAPI spec, MCP tool schemas, code paths). They do not give the same data again in different words. Each page lets a reader — a person or an agent — do the subsequent action.

## Milestones

A checked box shows a feature that we released. The changelog for each artifact (GitHub Releases for the CLI, PyPI release notes for the SDK) shows the version that contains each feature.

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
  - [x] OpenAI chat-completions-compatible
- Recording
  - [ ] S3 upload
  - [ ] Diarization

### Distribution

- API
  - [x] OpenAPI spec
- CLI
  - [x] `hail` binary via GitHub Releases
- MCP server
  - [x] Remote Streamable HTTP endpoint included with each Hail deployment
  - ~~PyPI stdio package~~ — we do not supply this package; refer to [docs/public/mcp.md](docs/public/mcp.md)
- Python SDK
  - [x] `hail-sdk` on PyPI, imports as `hail`

### Infrastructure

- [x] Docker Compose scaffold
- Self-hosted LiveKit SFU
  - [ ] docker compose integration

## Architecture

This diagram shows the path of an outbound call:

```
AI agent ──► Hail API ──dispatch──► Voicebot ──► LiveKit Cloud ──SIP──► Twilio ──► 📞
```

For the full diagram, refer to [docs/public/architecture.md](docs/public/architecture.md).

## Contributing

Refer to [docs/public/contributing.md](docs/public/contributing.md). The procedure is short: make a fork, make a branch, write conventional commits, and open a pull request. Put provider adapters in `core/hailhq/core/providers/`. If you add a new environment variable, update `.env.example` in the same commit.

## License

The source code has the [AGPL-3.0-or-later](./LICENSE) license. If you operate a modified Hail as a service, you must release your source code.
The pricing dataset (`costs/`) has the [CC-BY-4.0](./costs/LICENSE) license. You can use the pricing JSON if you give attribution.
