# Optional call recording

Workspace owners choose recording in **Calls → Settings** or during onboarding.
Existing workspaces keep audio off until an owner chooses otherwise.
Choose **7 days**, **30 days**, or **1 year (365 days)** for recording retention.
The default is one year. A call keeps the choice saved when it starts.
Changes apply to future calls; existing recordings keep their original expiry.
Agents and individual calls can disable recording with `recording_enabled: false`.
They cannot override a workspace that disables recording.

Hail Cloud keeps its base call price unchanged. Enabling recording selects a
combined call rate, disclosed in settings and on the pricing page. One debit
covers the answered call duration. The recording-enabled rate applies even
when best-effort capture fails. There is no later recording debit.

Recording never blocks a call. LiveKit quota limits, timeouts and upload failures
appear as `unavailable` or `failed` recording status, separately from call status.
Calls that never connect retain the existing no-charge behavior.

Audio-only room Egress captures the agent and the other participants on separate
channels in one OGG Opus file. The configured output is 48 kHz at 128 kbps.
Egress encodes audio itself; Hail adds no post-call compression stage.
This captures room audio, not the far-end handset or model-specific inputs.

`GET /calls/{id}/recording` authenticates the workspace and redirects to a
15-minute signed playback URL. It returns 404 when audio is unavailable or expired.
Call details contain `recording_enabled`, `recording_status` and
`recording_duration_ms`. Transcript events include actual speech wall times when
the agent SDK supplies them. Older events have no fabricated seek timestamps.

## Self-hosting

Enable the deployment capability with `HAIL_RECORDING_ENABLED=true`. In shared-key
mode this also enables recording by default. Set `recording_enabled: false` on
individual calls to opt out.

Configure `S3_BUCKET`, `S3_REGION`, `S3_ACCESS_KEY` and `S3_SECRET_KEY`.
Use `S3_ENDPOINT` for a compatible object store. Configure its CORS policy for the
player origin and allow GET, HEAD and Range requests. Hail does not require MinIO.
LiveKit Cloud Egress must be able to reach the endpoint; a localhost-only store
will not work with cloud Egress. A fully private installation needs self-hosted
Egress and a reachable LiveKit media server.

Provision cloud storage using `infra/terraform/s3_assets.tf`. It creates a
dedicated recordings IAM user; use its key outputs as `S3_ACCESS_KEY` and
`S3_SECRET_KEY` and the `assets_bucket` output as `S3_BUCKET`.
Recordings use `recordings/7d/`, `recordings/30d/`, and `recordings/365d/`
prefixes. Terraform sets the matching storage lifecycle rules. Unprefixed legacy
recordings keep their original one-year policy. Playback enforces each call's
saved expiry, including the lifetime of newly signed URLs. Storage lifecycle
removal is asynchronous and can follow the playback deadline.

Self-hosters set `HAIL_RECORDING_RETENTION_DAYS` to 7, 30, or 365 for the service
default. Configure equivalent lifecycle rules on an S3-compatible store.
Honor valid erasure requests and applicable shorter retention requirements.
These presets are product choices, not universal legal retention periods.

Configure a LiveKit webhook to `https://YOUR_API/v1/livekit/recording-events`.
The API verifies LiveKit's signature. Playback also reconciles Egress state when
a webhook is missed. Keep `HAIL_RECORDING_ENABLED=false` until storage is
reachable and your privacy notices cover recording.

## Capacity and cost

LiveKit Cloud Egress is metered per plan: the free Build plan allows 60 composite
minutes per month and two concurrent jobs; this implementation uses one job per
call. Past the allowance, calls continue without audio capture.
See [LiveKit pricing](https://livekit.com/pricing) and
[quotas](https://docs.livekit.io/deploy/admin/quotas-and-limits/).
Provider costs are in `costs/recording.json`.
