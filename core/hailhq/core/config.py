from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM providers
    openai_api_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    google_api_key: str = ""
    google_application_credentials: str = ""
    google_service_account_json_b64: str = ""
    google_cloud_project: str = ""
    google_genai_use_vertexai: bool = True
    anthropic_api_key: str = ""

    # LLM models for the FallbackAdapter chain (mode A — system_prompt only).
    # Set via .env / .env.local — see .env.example for current values.
    openai_model: str = ""
    google_model: str = ""
    anthropic_model: str = ""

    # Voice pipeline
    deepgram_api_key: str = ""
    cartesia_api_key: str = ""
    eleven_api_key: str = ""
    speechmatics_api_key: str = ""

    # STT/TTS — model names set via .env / .env.local.
    deepgram_model: str = ""
    cartesia_voice_id: str = ""
    cartesia_model: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = ""

    # Carriers
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""

    # AWS — used today for SES (outbound email). boto3 falls back to its
    # default credential chain (env / config file / IAM role) when these
    # are empty, so workloads running on EC2/ECS pick up IAM-role creds
    # automatically.
    aws_region: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""

    # Email — operator-managed parent domain used to mint hail-mail
    # addresses of the form ``<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>``
    # (e.g. ``alice+acme@mail.hail.so``). The operator pre-verifies this
    # one SES identity out-of-band; per-org rows land already-verified.
    # Leave empty to disable hail-mail mode (only custom domains accepted).
    hail_mail_base_domain: str = ""

    # Shortcut for self-hosters running a SINGLE org: one full address the
    # server splits into the user/org prefix pair internally. Wins over the
    # per-org derived org prefix when set. Example:
    #   HAIL_MAIL_FROM=admin+selfhost@mail.hail.so
    # The domain portion must match ``HAIL_MAIL_BASE_DOMAIN`` (the
    # SES-verified parent); each side of the ``+`` must match the
    # ``^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$`` prefix regex.
    # Do NOT set this on a multi-tenant deployment — a fixed org prefix is
    # shared across orgs and only the first org can claim it.
    hail_mail_from: str = ""

    # Default USER prefix for ``POST /email-domains`` (``kind='hail_mail'``)
    # when the request omits ``local_prefix_user``. The ORG prefix is never a
    # deploy-wide constant — it is derived per-org from the organization id
    # (see ``hailhq.core.hail_mail.org_prefix_from_id``) so two orgs can never
    # share an address. Must match
    # ``^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$``.
    hail_mail_default_user_prefix: str = ""

    # SES configuration set attached to every outbound send. Provisioned by
    # infra/terraform (event destinations → SNS → ingest Lambda → API); the
    # name must match the Terraform variable. Empty = sends carry no config
    # set and no delivery/engagement events are published.
    hail_ses_configuration_set: str = ""

    # Inbound email (SES). Off by default so a misconfigured Lambda can't
    # write rows into a deployment that hasn't opted into inbound. The
    # Terraform module under ``infra/terraform/`` provisions S3 bucket,
    # SES Receipt Rule, and Lambda; HAIL_INBOUND_HMAC_SECRET is shared
    # between the Lambda env and the API.
    # Background re-poll cadence (seconds) for pending custom sender domains.
    # The worker flips them to verified once DKIM lands and fails them past a
    # 72h TTL. Set 0 to disable (rely on POST /email-domains/{id}/verify only).
    hail_domain_verify_poll_seconds: int = 120

    hail_inbound_enabled: bool = False
    # Single source of truth for both the Terraform module and the API.
    # The mail bucket name is derived as ``{prefix}-mail``; SES Lambda
    # writes inbound raw MIME there, outbound sends write uploaded
    # attachments there, and the API reads both back. Set in .env /
    # .env.example.
    hail_mail_name_prefix: str = ""
    hail_inbound_hmac_secret: str = ""
    # Forwarding controls — see docs spec §6.2.
    hail_forward_max_hops: int = 3
    hail_forward_rate_per_hour: int = 200
    # Per-org soft cap on inbound. Beyond it we persist but skip fan-out.
    hail_inbound_org_rate_per_hour: int = 1000
    # Self-host convenience — when true, webhook targets pointing at
    # localhost / RFC-1918 / link-local are accepted. Leave false in prod.
    hail_webhook_allow_private_networks: bool = False
    # Fernet key for encrypting webhook secrets at rest. Generate with
    # `python -c "from hailhq.core.secret_cipher import generate_key; print(generate_key())"`.
    hail_webhook_secret_key: str = ""
    # Fernet key for org provider API keys at rest (BYO keys). Dedicated —
    # do NOT reuse hail_webhook_secret_key; rotating one must not break the
    # other. Generate like the webhook key (see secret_cipher.generate_key).
    hail_provider_secret_key: str = ""

    # Media
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    # LiveKit SIP trunks are direction-specific. Outbound is used today by
    # POST /calls (CreateSIPParticipantRequest.sip_trunk_id). Inbound is for
    # the v1.1 inbound-calls milestone — kept here so the config schema is
    # ready and operators only set both up once.
    livekit_sip_outbound_trunk_id: str = ""
    livekit_sip_inbound_trunk_id: str = ""

    # Storage
    database_url: str = "postgresql://hail:hail@postgres:5432/hail"
    s3_endpoint: str = "http://minio:9000"
    s3_bucket: str = "hail-recordings"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"

    # Hail
    # Shared-key mode: when set, requests carrying this bearer authenticate
    # to the sentinel "Self-hosted" organization. The balance gate at
    # POST /v1/calls is skipped for this path — billing is cloud-only. Leave
    # empty in managed cloud; per-user keys are the only auth path there.
    hail_api_key: str = ""
    hail_api_url: str = "http://localhost:8080"

    # Soft cap on voice-call duration. When a call reaches this many seconds
    # the voicebot says a polite "we've reached the time limit" line, waits
    # for playout, then triggers ctx.shutdown(). Set to 0 to disable the cap.
    # Worst-case overrun ≈ this × per-minute rate, bounding abuse from a
    # single long call.
    hail_voice_max_duration_seconds: int = 300

    # Pool-number sweeper backstop. Force-release a pool reservation when
    # now() > calls.requested_at + max_duration_seconds + this grace, even
    # if neither the API nor the voicebot called release_pool_reservation.
    # Set wide enough to absorb LiveKit/Twilio teardown + clock skew; small
    # enough that a stuck reservation doesn't starve the pool for long.
    hail_pool_release_grace_seconds: int = 120

    # Base URL of the hail-website deployment. Used by voicebot / api to
    # trigger internal endpoints (e.g., the usage-events rater). Leave
    # empty in self-host — the corresponding internal calls become no-ops.
    hail_base_url: str = ""
    # Shared HMAC secret for internal API↔website calls (rater webhook
    # today; other internal endpoints later). Generate with
    #   openssl rand -base64 32
    # Must match the value set in hail-website's HAIL_INTERNAL_SECRET.
    hail_internal_secret: str = ""

    # Hail auth backend (cloud) — OAuth/JWT verification alongside the
    # existing API-key path. ``hail_auth_url`` is the issuer URL the auth
    # backend stamps on JWTs (Better Auth's ``ctx.baseURL``, e.g.
    # "https://hail.so/api/auth"); the JWKS endpoint is derived as
    # ``${hail_auth_url}/jwks``. ``hail_auth_audiences`` is a CSV of
    # accepted ``aud`` claims (e.g. "https://api.hail.so,https://mcp.hail.so").
    # Leave both empty in self-host: the JWT path stays disabled and only
    # shared-key + API-key paths are tried.
    hail_auth_url: str = ""
    hail_auth_audiences: str = ""

    # MCP service resource identity (cloud) — the public URL the MCP
    # serves under (e.g. "https://mcp.hail.so"). Used by the MCP server's
    # FastMCP AuthSettings as ``resource_server_url`` so the 401's
    # WWW-Authenticate header points clients at this MCP's own
    # ``.well-known/oauth-protected-resource``. Empty in self-host.
    mcp_resource_url: str = ""

    # --- Pre-send compliance gate (hailhq.core.compliance_gate) --------- #

    # Signs GET /unsubscribe tokens (email + organization_id + expiry). A
    # dedicated secret — deliberately NOT hail_internal_secret, which is a
    # different concern (internal API<->website HMAC signing). Generate
    # with: openssl rand -base64 32
    hail_unsubscribe_secret: str = ""

    # National Do Not Call registry (donotcall.gov) scrub. Off by default:
    # there is no live vendor integration today (a real check requires a
    # paid subscription — a Bucket-1/founder task). See
    # ``compliance_gate.check_national_dnc`` for the stub this gates.
    hail_national_dnc_enabled: bool = False

    # CSV of E.164 prefixes to block outright (premium-rate / high-risk
    # destinations), e.g. "+1900,+1976". Parsed at the use site — same
    # convention as ``hail_auth_audiences`` (see api/hailhq/api/deps.py).
    # Empty = no prefix blocking. Illustrative examples in .env.example;
    # the complete list is an ops decision, not hardcoded here.
    hail_blocked_e164_prefixes: str = ""

    # Velocity / new-account caps — flat defaults for now; a per-tier
    # scheme is out of scope. Counted against hailhq.core.models.UsageEvent
    # rows in a rolling window (see compliance_gate for the voice-channel
    # lag caveat: voice usage_events are written by the voicebot at call
    # completion, not at dial time).
    hail_velocity_call_per_hour: int = 20
    hail_velocity_call_per_day: int = 200
    hail_velocity_email_per_hour: int = 50
    hail_velocity_email_per_day: int = 500
    hail_velocity_sms_per_hour: int = 100
    hail_velocity_sms_per_day: int = 1000

    # Abuse-monitoring guardrail (SMS opt-out rate -> ChannelSuspension).
    # Conservative starting thresholds per the design spec's own caution
    # that these are unvalidated pending real traffic data — tune post-launch.
    hail_sms_abuse_window_hours: int = 24
    hail_sms_abuse_min_sends: int = 20  # floor: don't flag low-volume orgs
    hail_sms_abuse_max_opt_out_rate: float = 0.05  # 5% opt-out rate trips it
    # Poll cadence (seconds) for the AbuseMonitorWorker's coarse-grained batch
    # check — hourly by default, not a per-send check. Set 0 to disable.
    hail_abuse_monitor_poll_seconds: int = 3600

    # Agent self-signup velocity caps (spec: 2026-07-14-agent-self-signup-design).
    # Per agent-origin org:
    agent_email_per_hour: int = 20
    agent_email_per_day: int = 50
    agent_sms_per_hour: int = 5
    agent_sms_per_day: int = 15
    agent_voice_per_hour: int = 3
    agent_voice_per_day: int = 10
    # Distinct recipients per org, per channel, per day:
    agent_email_recipients_per_day: int = 10
    agent_sms_recipients_per_day: int = 5
    agent_voice_recipients_per_day: int = 5
    # Global ceilings across ALL agent-origin orgs, per channel, per hour:
    agent_global_email_per_hour: int = 200
    agent_global_sms_per_hour: int = 30
    agent_global_voice_per_hour: int = 15

    # General per-caller HTTP request-rate ceiling (api/hailhq/api/ratelimit.py).
    # Distinct from the agent_* velocity caps above: those are a DB-backed,
    # per-org, per-channel *send* cap for agent-origin orgs only; this is an
    # in-memory request-rate limiter across every customer-facing route, for
    # every caller. 300/min = 5 req/sec sustained per caller — generous for
    # legitimate agent/automation traffic, still bounds a runaway loop. This
    # is a starting point, not a researched-and-final threshold — tune
    # post-launch same as the velocity caps above.
    api_rate_limit_per_minute: int = 300

    # SMS compliance auto-replies (HELP/STOP/START). OFF by default: Twilio's
    # own opt-out handling already auto-replies to these keywords, so enabling
    # Hail replies on top would double-text. Enable only when Twilio's default
    # filtering is disabled (account-wide Support ticket) or on a non-Twilio
    # provider. The suppression record is written regardless of this flag.
    hail_sms_compliance_replies_enabled: bool = False
    hail_sms_stop_reply: str = (
        "You replied STOP and are unsubscribed from Hail messages and will receive no more. "
        "Reply START to resubscribe. Help: hi@hail.so"
    )
    hail_sms_help_reply: str = (
        "Hail: for help contact hi@hail.so. Msg&data rates may apply. "
        "Reply STOP to unsubscribe."
    )
    hail_sms_start_reply: str = (
        "You are resubscribed to Hail messages. Reply STOP to unsubscribe, "
        "HELP for help."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hail_mail_bucket(self) -> str:
        """Shared mail bucket name. Derived to match Terraform's `${prefix}-mail`."""
        if not self.hail_mail_name_prefix:
            return ""
        return f"{self.hail_mail_name_prefix}-mail"


settings = Settings()
