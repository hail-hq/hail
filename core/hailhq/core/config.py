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
    eleven_api_key: str = ""

    # STT/TTS — model names set via .env / .env.local.
    deepgram_model: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = ""

    # Carriers
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""

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
    hail_api_key: str = ""
    hail_api_url: str = "http://localhost:8080"


settings = Settings()
