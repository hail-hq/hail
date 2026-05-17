# Setup

Connect the third-party services Hail needs — LiveKit Cloud for media, Twilio for phone numbers, and any MCP client for agent access.

Hail v1 wraps three external services. Provision each, drop credentials into `.env`, and `docker compose up` does the rest.

## Order

1. **[LiveKit Cloud](./livekit-cloud.md)** — media (SIP bridge + WebRTC). Required before voicebot can join calls.
2. **[Twilio](./twilio.md)** — phone numbers + SIP trunk. The Origination URI from LiveKit goes into the Twilio trunk config.
3. **[AWS SES](./aws-ses.md)** — outbound email. Independent of voice setup; needed only if you'll send mail.
4. **[MCP clients](./mcp.md)** — connect Claude.ai / ChatGPT / Cursor / Claude Code to your Hail deployment. Independent of voice setup; needed only for agent-driven flows.

For one-click client setup snippets, also see the [client picker on hail.so/mcp](https://hail.so/mcp).
