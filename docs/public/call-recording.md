# Optional call recording

Workspace owners choose recording in **Calls → Settings** or during onboarding.
Recording is off until an owner turns it on. Retention is **7 days**, **30 days**
or **1 year**; the default is one year. A call keeps the retention saved when it
starts, so later changes apply to future calls only.

Agents and individual calls can opt out with `recording_enabled: false`. They
cannot turn recording on for a workspace that has it off.

Recording never blocks a call. If capture fails, the call continues and the
recording status is `unavailable` or `failed`.

`GET /calls/{id}/recording` redirects to a short-lived signed playback URL and
returns 404 when no audio is available or it has expired. Call details carry
`recording_enabled`, `recording_status` and `recording_duration_ms`.

## Self-hosting

Set `HAIL_RECORDING_ENABLED=true` and `HAIL_RECORDING_RETENTION_DAYS` (7, 30 or
365). Configure `S3_BUCKET`, `S3_REGION`, `S3_ACCESS_KEY` and `S3_SECRET_KEY`;
`S3_ENDPOINT` for an S3-compatible store. `infra/terraform/s3_assets.tf` creates
the bucket with matching lifecycle rules and a recordings-only IAM user.
Point a LiveKit webhook at `https://<api host>/v1/livekit/recording-events`.
