# Hail documentation

Help a caller find out what to bring to an appointment. [Create an agent](https://hail.so/create-ai-agent) and put a short list in **Instructions**. Turn on **Calls**, assign a number, and call it to check the answer.

Use Hail for calls, texts, and email. Connect your assistant through MCP or build with the API. Use [Hail Cloud](https://hail.so) or self-host under AGPLv3 with LiveKit Cloud and your channel providers.

## Using Hail Cloud

- [MCP clients](./mcp.md) — connect Claude.ai, ChatGPT, Cursor, or any MCP client. Paste a URL, click Allow, done.
- [Agents](./agents.md) — save an agent once; numbers answer calls and texts with it, and `POST /v1/calls` can place calls with it.
- [Webhooks](./webhooks.md) — receive signed events for inbound mail, SMS, delivery reports, and call outcomes. Use call transcripts and any summaries to create tickets in your help desk.
- [Email delivery](./email.md): what Hail does so mail reaches the email inbox: signed domains, one-click unsubscribe, bounce and complaint suppression, events and stats.
- [react-email](./react-email.md) — render a react-email template and send it through `POST /v1/emails`.
- [Bring your own LLM](./byo-llm.md) — point voice calls at your own OpenAI-compatible endpoint.
- [CLI reference](./cli.md) — the `hail` binary's email and webhooks surface.
- [API reference](https://hail.so/docs/api) — every REST endpoint, generated from the OpenAPI spec.

## Understanding Hail

- [Architecture](./architecture.md) — the three Python services, the Go CLI, and how LiveKit Cloud fits in.

## Running it yourself

- [Self-hosting](./self-host/README.md) — the umbrella: one-VM deploy, provider setup (LiveKit, Twilio, SES), SMTP inbound, and the operations runbook.
- [Contributing](./contributing.md) — dev environment, regenerating OpenAPI, PR flow.

Source lives at [github.com/hail-hq/hail](https://github.com/hail-hq/hail). Every page here is also plain markdown in `docs/public/` — anything in that folder is published, anything outside it is not.

- [Optional call recording](call-recording.md): settings, playback, billing and self-hosted storage.
