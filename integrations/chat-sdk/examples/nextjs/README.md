# Hail SMS echo bot

From `integrations/chat-sdk`, install and build the adapter:

```bash
pnpm install --frozen-lockfile
pnpm build
cp examples/nextjs/.env.example examples/nextjs/.env.local
```

Fill in the Hail credentials and number. Set `HAIL_RECIPIENT_CONSENT=true` only after confirming consent for recipients.
Run `pnpm --filter example-hail-chat-sdk dev` and expose port 3000 through your HTTPS tunnel.
Subscribe to `sms.received` at `https://your-tunnel.example/api/webhooks/hail`, then copy its signing secret into `.env.local`.
Send an SMS to the configured Hail number. The bot replies with `You said: …`.

Before a production build, set `REDIS_URL` and the Hail variables in the build and runtime environments.
Run `pnpm --filter example-hail-chat-sdk build` followed by `pnpm --filter example-hail-chat-sdk start`.
The bot requires persistent Redis state in production. Refer to the [adapter README](../../README.md) for Hail setup and limitations.
