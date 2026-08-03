# Hail documentation

Hail is a universal communication platform for AI agents — outbound phone calls, SMS, and email behind one MCP endpoint, one API key, one invoice. Self-hostable, AGPLv3.

## Start here

- [Architecture](./architecture.md) — the three Python services, the Go CLI, and how LiveKit Cloud fits in.
- [Setup](./setup/README.md) — connect Twilio, LiveKit Cloud, Amazon SES, and your MCP client.
- [CLI reference](./cli.md) — the `hail` binary's email and webhooks surface.

## Running it yourself

- [Operations](./operations.md) — develop, deploy, migrate, release.
- [Self-hosting](./self-host/vm-deploy.md) — the whole stack on one Ubuntu VM with HTTPS and auto-deploy.
- [Contributing](./contributing.md) — dev environment, regenerating OpenAPI, PR flow.

Source lives at [github.com/hail-hq/hail](https://github.com/hail-hq/hail). Every page here is also plain markdown in `docs/public/` — anything in that folder is published, anything outside it is not.
