# Self-hosting

Run Hail's API, voicebot, MCP server, Postgres, and object storage with Docker
Compose. LiveKit Cloud and the providers for the channels you enable remain
external. The code is AGPLv3.

## Local evaluation

Prerequisites: Git, Docker Engine, and Docker Compose v2.

```bash
git clone https://github.com/hail-hq/hail
cd hail
cp .env.example .env

# Generate a key and put it in .env as HAIL_API_KEY.
printf 'hk_%s\n' "$(openssl rand -base64 32 | tr -d '/+=' | head -c 40)"

docker compose -f docker-compose.yml -f docker-compose.local.yml \
  run --rm api alembic upgrade head
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
curl --fail http://localhost:8080/healthz
```

The local overlay supplies Postgres. Plain `docker compose up` is only for a
managed database after `DATABASE_URL` has been changed from its bundled
`postgres` default. Compose reads `.env` for containers but does not export it
to your shell; set `HAIL_API_URL` and `HAIL_API_KEY` before using the CLI or SDK.

## Order

1. **[Operations](./operations.md#deployment-self-host)** — required credentials, migrations, authentication, phone-number binding, and troubleshooting.
2. **[LiveKit Cloud](./livekit-cloud.md)** — required media and SIP bridge for voice calls.
3. **[Twilio](./twilio.md)** — required phone numbers and SIP trunk for voice/SMS.
4. **[AWS SES](./aws-ses.md)** — optional outbound/inbound email. To receive without AWS, see [SMTP inbound](./smtp-inbound.md).
5. **[VM deployment](./vm-deploy.md)** — production deployment on Ubuntu with managed Postgres and HTTPS.

Local MCP clients use `http://localhost:8081`. Web-based clients need a public
HTTPS endpoint; see [MCP clients](../mcp.md#self-host).

## Day 2

- **[Operations runbook](./operations.md)** — develop, deploy, migrate, release. The single source of truth for operating a Hail deployment.
