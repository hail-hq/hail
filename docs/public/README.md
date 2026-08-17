# Hail documentation

Hail is a universal communication platform for AI agents — outbound phone calls, SMS, and email behind one MCP endpoint, one API key, one invoice. Most people use [Hail Cloud](https://hail.so); Hail's services are also self-hostable under AGPLv3, with LiveKit Cloud and channel providers remaining external.

## Using Hail Cloud

- [MCP clients](./mcp.md) — connect Claude.ai, ChatGPT, Cursor, or any MCP client. Paste a URL, click Allow, done.
- [Webhooks](./webhooks.md) — signed JSON events for inbound mail, SMS, delivery reports, and call outcomes.
- [Bring your own LLM](./byo-llm.md) — point voice calls at your own OpenAI-compatible endpoint.
- [CLI reference](./cli.md) — the `hail` binary's email and webhooks surface.
- [API reference](https://hail.so/docs/api) — every REST endpoint, generated from the OpenAPI spec.

## Understanding Hail

- [Architecture](./architecture.md) — the three Python services, the Go CLI, and how LiveKit Cloud fits in.

## Running it yourself

- [Self-hosting](./self-host/README.md) — the umbrella: one-VM deploy, provider setup (LiveKit, Twilio, SES), SMTP inbound, and the operations runbook.
- [Contributing](./contributing.md) — dev environment, regenerating OpenAPI, PR flow.

Source lives at [github.com/hail-hq/hail](https://github.com/hail-hq/hail). Every page here is also plain markdown in `docs/public/` — anything in that folder is published, anything outside it is not.
