# Self-hosting

Run the whole Hail stack yourself. The code is AGPLv3; the stack is three Python services, a Go CLI, and Postgres. Provision the three external providers below, put the credentials in `.env`, and `docker compose up` does the rest.

## Order

1. **[VM deployment](./vm-deploy.md)** — run the full stack on one Ubuntu VM behind HTTPS. GitHub Actions deploys each commit on `main` automatically.
2. **[LiveKit Cloud](./livekit-cloud.md)** — media (SIP bridge + WebRTC). This is necessary before the voicebot can join calls.
3. **[Twilio](./twilio.md)** — phone numbers, SMS, and the SIP trunk. Put the Origination URI from LiveKit in the Twilio trunk configuration.
4. **[AWS SES](./aws-ses.md)** — outbound and inbound email. This is independent of the voice setup. You need it only if you send or receive mail. To receive without AWS, refer to [SMTP inbound](./smtp-inbound.md).

Then connect your agent the same way cloud users do: [MCP clients](../mcp.md) — the self-host URL is `http://<your-host>:8081`.

## Day 2

- **[Operations runbook](./operations.md)** — develop, deploy, migrate, release. The single source of truth for operating a Hail deployment.
