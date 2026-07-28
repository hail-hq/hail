# Setup

Connect the third-party services that Hail needs — LiveKit Cloud for media, Twilio for phone numbers and SMS, AWS SES for email, and any MCP client for agent access.

Hail v1 wraps three external services. Provision each service. Put the credentials in `.env`. Then `docker compose up` does the rest.

## Order

1. **[LiveKit Cloud](./livekit-cloud.md)** — media (SIP bridge + WebRTC). This is necessary before the voicebot can join calls.
2. **[Twilio](./twilio.md)** — phone numbers, SMS, and the SIP trunk. Put the Origination URI from LiveKit in the Twilio trunk configuration.
3. **[AWS SES](./aws-ses.md)** — outbound and inbound email. This is independent of the voice setup. You need it only if you send or receive mail.
4. **[MCP clients](./mcp.md)** — connect Claude.ai / ChatGPT / Cursor / Claude Code to your Hail deployment. This is independent of the voice setup. You need it only for agent-driven flows.
5. **[VM deployment](./vm-deploy.md)** — run the full stack on one Ubuntu VM behind HTTPS. GitHub Actions deploys each commit on `main` automatically.

For one-click client setup snippets, also refer to the [client picker on hail.so/mcp](https://hail.so/mcp).
