# Call recording rollout (Hail Cloud)

Order matters. Steps 1 to 3 happen before the merge. Merge runs migration 0060
and the deploy by itself (`.github/workflows/deploy.yml`).

1. Create the bucket and attach the recordings policy to the service IAM user.
   Terraform is not run by CI.

   ```bash
   cd infra
   AWS_PROFILE=myaws terragrunt apply
   ```

   Creates `hail-mail-assets` (eu-west-1), its lifecycle and CORS rules, the
   `hail-mail-recordings` policy, and attaches it to `hail-production`.
   It also redeploys the SES ingest Lambda (lint-only source change).

2. LiveKit Cloud console: Settings > Webhooks > Add endpoint.
   URL: `https://api.hail.so/v1/livekit/recording-events`. API key: the one in
   `LIVEKIT_API_KEY`. Playback reconciles missed webhooks, so this is not blocking.

3. Copy the repo `.env` to the VM (path from `DEPLOY_PATH`, default `/opt/hail`).
   New keys: `HAIL_RECORDING_ENABLED=true`, `HAIL_RECORDING_RETENTION_DAYS=365`,
   `S3_ENDPOINT=` (empty), `S3_BUCKET=hail-mail-assets`, `S3_ACCESS_KEY` and
   `S3_SECRET_KEY` (the `hail-production` keys, same as `AWS_*`), `S3_REGION=eu-west-1`.

4. Merge hail PR #200. Wait for the deploy workflow to finish.

5. Merge hail-website PR #155 (needs migration 0060 live first).

6. Check: place a call from a workspace with recording on, then
   `GET /v1/calls/{id}/recording` redirects to a signed URL.
