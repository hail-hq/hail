# Call recording rollout (Hail Cloud)

Merge first. Merge runs migration 0060 and the deploy
(`.github/workflows/deploy.yml`). Until the VM `.env` has the new keys, the
API defaults to `HAIL_RECORDING_ENABLED=false`, so nothing records yet.

1. Merge hail PR #200. Wait for the deploy workflow to finish.

2. Create the bucket and the recordings-only IAM user. CI does not run Terraform.
   Run from the main checkout (it now has `infra/terraform/s3_assets.tf`).

   ```bash
   cd /Users/r/playground/hail/infra
   AWS_PROFILE=myaws terragrunt apply
   ```

   Creates `hail-mail-assets` (eu-west-1) with lifecycle and CORS rules, the
   `hail-mail-recordings` IAM user with only the recordings policy, and its key.
   That key goes to LiveKit Cloud Egress, so it is not the service user's key.
   It also redeploys the SES ingest Lambda (lint-only source change).

3. Write the key into the repo `.env`:

   ```bash
   cd /Users/r/playground/hail/infra
   AK=$(AWS_PROFILE=myaws terragrunt output -raw recordings_access_key_id 2>/dev/null)
   SK=$(AWS_PROFILE=myaws terragrunt output -raw recordings_secret_access_key 2>/dev/null)
   sed -i '' "s|^S3_ACCESS_KEY=.*|S3_ACCESS_KEY=$AK|; s|^S3_SECRET_KEY=.*|S3_SECRET_KEY=$SK|" \
     /Users/r/playground/hail/.env
   grep -n "^S3_\|^HAIL_RECORDING" /Users/r/playground/hail/.env
   ```

   Expected: `HAIL_RECORDING_ENABLED=true`, `HAIL_RECORDING_RETENTION_DAYS=365`,
   `S3_ENDPOINT=` (empty), `S3_BUCKET=hail-mail-assets`, both keys filled,
   `S3_REGION=eu-west-1`.

4. LiveKit Cloud console: Settings > Webhooks > Add endpoint.
   URL: `https://api.hail.so/v1/livekit/recording-events`. API key: the one in
   `LIVEKIT_API_KEY`. Playback reconciles missed webhooks, so this is not blocking.

5. Copy `/Users/r/playground/hail/.env` to the VM at `/opt/hail/.env`, then
   restart the stack with a deploy re-run:

   ```bash
   gh workflow run deploy.yml -R hail-hq/hail
   ```

6. Merge hail-website PR #155 (needs migration 0060 live, done by step 1).

7. Check: place a call from a workspace with recording on, then
   `GET /v1/calls/{id}/recording` redirects to a signed URL.

LiveKit Build plan: 60 recording minutes per month, 2 at once. Past that, calls
continue without audio. Ship plan: $50/month for 600 minutes.
