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

    # Voice pipeline
    deepgram_api_key: str = ""
    eleven_api_key: str = ""

    # Carriers
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_sip_trunk_domain: str = ""
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    ses_from_address: str = ""

    # Media
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_sip_trunk_id: str = ""

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
