# Optional call recording

Workspace owners choose recording in **Calls → Settings** or during onboarding.
Existing workspaces keep audio off until an owner chooses otherwise.
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
individual calls to opt out. Self-hosted deployments do not run Hail Cloud's rater.

Configure `S3_BUCKET`, `S3_REGION`, `S3_ACCESS_KEY` and `S3_SECRET_KEY`.
Use `S3_ENDPOINT` for a compatible object store. Configure its CORS policy for the
player origin and allow GET, HEAD and Range requests. Hail does not require MinIO.
LiveKit Cloud Egress must be able to reach the endpoint; a localhost-only store
will not work with cloud Egress. A fully private installation needs self-hosted
Egress and a reachable LiveKit media server.

Provision cloud storage using `infra/terraform/s3_assets.tf`. Attach the exported
recordings policy to the recording credentials and pass the assets bucket name.
The recording prefix expires after 30 days by default. Set
`HAIL_RECORDING_RETENTION_DAYS` and the bucket lifecycle to the same value.
Custom stores need equivalent expiry rules. Signed playback enforces expiry too.

Configure a LiveKit webhook to `https://YOUR_API/v1/livekit/recording-events`.
The API verifies LiveKit's signature. Playback also reconciles Egress state when
a webhook is missed. Keep `HAIL_RECORDING_ENABLED=false` until storage, migration
0060, legal notices and organization controls are deployed together.

## Capacity and cost

The free Build plan includes 60 composite minutes per month across the project
and two concurrent Egress jobs. This implementation uses one job per call.
Exhausting either allowance leaves the call running without audio capture.
The allowance is a hard cap, not usage billing.

Ship starts at $50/month, includes 600 composite minutes, then charges $0.005
per audio-only minute. For recording alone, 1,000 monthly minutes would cost
approximately $52; 10,000 would cost $97, before storage and other LiveKit usage.
See [LiveKit pricing](https://livekit.com/pricing) and
[quotas](https://docs.livekit.io/deploy/admin/quotas-and-limits/).
The public `costs/recording.json` dataset holds provider costs; Hail Cloud's selling
rate remains in the private website repository.
