# Call recording rollout

Merge first. The deploy workflow runs migrations. Until the deployment `.env`
has the recording keys, the API defaults to `HAIL_RECORDING_ENABLED=false`, so
nothing records yet.

1. Merge and wait for the deploy workflow.

2. Create the bucket and the recordings-only IAM user. CI does not run Terraform.

   ```bash
   cd infra
   terragrunt apply
   terragrunt output -raw assets_bucket
   terragrunt output -raw recordings_access_key_id
   terragrunt output -raw recordings_secret_access_key
   ```

   The recordings key goes to LiveKit Cloud Egress, so it is a dedicated user
   with the recordings policy only, never the service user's key.

3. In `.env`: `HAIL_RECORDING_ENABLED=true`, `HAIL_RECORDING_RETENTION_DAYS`
   (7, 30 or 365), `S3_ENDPOINT=` empty for AWS, `S3_BUCKET`, `S3_ACCESS_KEY`,
   `S3_SECRET_KEY` from the outputs above, `S3_REGION` = the Terraform region.

4. LiveKit Cloud console: Settings > Webhooks > Add endpoint
   `https://<api host>/v1/livekit/recording-events` with the project API key.
   Playback reconciles missed webhooks, so this is not blocking.

5. Copy `.env` to the deployment and restart the stack (re-run the deploy
   workflow).

6. Check: place a call from a workspace with recording on, then
   `GET /v1/calls/{id}/recording` redirects to a signed URL.
