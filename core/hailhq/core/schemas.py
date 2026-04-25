import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

E164 = re.compile(r"^\+[1-9]\d{1,14}$")


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: str
    model: str


class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stt: Literal["deepgram"] = "deepgram"
    tts: Literal["elevenlabs"] = "elevenlabs"
    vad: Literal["silero"] = "silero"
    turn_detection: Literal["livekit"] = "livekit"


class CallCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str
    from_: str | None = Field(default=None, alias="from")
    system_prompt: str | None = None
    llm: LLMConfig | None = None
    first_message: str | None = None
    voice_config: VoiceConfig = Field(default_factory=VoiceConfig)
    conversation_id: UUID | None = None
    metadata: dict = Field(default_factory=dict)

    @field_validator("to", "from_")
    @classmethod
    def _validate_e164(cls, v: str | None) -> str | None:
        if v is not None and not E164.match(v):
            raise ValueError("must be E.164 (e.g. +14155551234)")
        return v

    @model_validator(mode="after")
    def _prompt_or_llm(self):
        if self.system_prompt is None and self.llm is None:
            raise ValueError("either system_prompt or llm must be provided")
        return self


CallStatus = Literal[
    "queued",
    "dialing",
    "ringing",
    "in_progress",
    "completed",
    "failed",
    "busy",
    "no_answer",
    "canceled",
]


class CallResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    conversation_id: UUID | None
    from_e164: str
    to_e164: str
    direction: Literal["outbound", "inbound"]
    status: CallStatus
    end_reason: str | None
    provider_call_sid: str | None
    livekit_room: str | None
    initial_prompt: str | None
    recording_s3_key: str | None
    requested_at: datetime
    started_at: datetime | None
    answered_at: datetime | None
    ended_at: datetime | None


class CallListResponse(BaseModel):
    items: list[CallResponse]
    next_cursor: str | None = None
