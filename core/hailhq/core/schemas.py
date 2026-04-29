import base64
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

E164 = re.compile(r"^\+[1-9]\d{1,14}$")


# --------------------------------------------------------------------------- #
# Cursor codec.
#
# Wire format: base64(urlsafe-no-pad) of "<isoformat>|<uuid>". Used by every
# cursor-paginated route (calls list, events stream). The CLI's
# ``encodeEventCursor`` mirrors this byte-for-byte. ``decode_cursor`` raises
# ``ValueError``; routes wrap it as a 400.
# --------------------------------------------------------------------------- #


def encode_cursor(ts: datetime, id_: UUID) -> str:
    raw = f"{ts.isoformat()}|{id_}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), UUID(id_str)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid cursor: {exc}") from exc


# --------------------------------------------------------------------------- #
# Resource id parsing.
#
# Wire format on the events stream filter is ``<type>:<uuid>`` — e.g.
# ``call:abc-def-...``. ``audit_log`` already uses the ``resource_type`` /
# ``resource_id`` vocabulary; the events query surface adopts the same
# convention so SMS / email / conversation can join later without a second
# rename.
#
# v1 only resolves ``call``; unknown types fail closed with a 422 so a client
# never silently gets back zero rows for a typo. The list lives here so the
# helper, the route, and (eventually) the SDK share one source.
# --------------------------------------------------------------------------- #

SUPPORTED_RESOURCE_TYPES: tuple[str, ...] = ("call",)


def parse_resource_id(value: str) -> tuple[str, UUID]:
    """Parse a ``<type>:<uuid>`` resource id.

    Raises ``ValueError`` (with a specific message) on:
      * missing colon
      * empty type or empty id
      * unknown type (not in :data:`SUPPORTED_RESOURCE_TYPES`)
      * id that is not a valid UUID
    """
    if ":" not in value:
        raise ValueError("must be '<type>:<uuid>' (e.g. 'call:abc-...'); missing ':'")
    type_str, _, id_str = value.partition(":")
    if not type_str:
        raise ValueError("missing resource type before ':'")
    if not id_str:
        raise ValueError("missing resource id after ':'")
    if type_str not in SUPPORTED_RESOURCE_TYPES:
        supported = ", ".join(SUPPORTED_RESOURCE_TYPES)
        raise ValueError(
            f"unsupported resource type '{type_str}'; supported: [{supported}]"
        )
    try:
        return type_str, UUID(id_str)
    except ValueError as exc:
        raise ValueError(f"invalid uuid '{id_str}': {exc}") from exc


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
        has_prompt = self.system_prompt is not None and self.system_prompt != ""
        has_llm = self.llm is not None
        if has_prompt and has_llm:
            raise ValueError(
                "system_prompt and llm are mutually exclusive (use one mode)"
            )
        if not has_prompt and not has_llm:
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


NumberType = Literal["local", "mobile", "toll_free"]


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


class CallEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    call_id: UUID
    kind: str
    payload: dict[str, Any]
    occurred_at: datetime


class EventStreamResponse(BaseModel):
    items: list[CallEventResponse]
    next_cursor: str | None = None
    # Only populated when the ``id`` query filter resolves to a call (e.g.
    # ``id=call:<uuid>``). Org-wide tails and non-call resource types leave
    # this null — there's no single "the" status to report.
    call_status: CallStatus | None = None
