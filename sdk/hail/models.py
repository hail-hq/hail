"""Pydantic models for the Hail v1 API.

Mirror of ``core/hailhq/core/schemas.py`` — duplicated by hand because
the SDK ships standalone (``pip install hail-sdk`` must not pull any
``hailhq.*`` packages). Keep this file in lockstep with core's schemas
when fields change. A future task will codegen these from
``openapi/openapi.yaml``; until then the duplication is intentional and
audited by hand.

Two intentional deviations from core:

1. ``CallCreate`` enforces the CLI's mode-A/B rules (mirroring
   ``cli/internal/cmd/call.go::validateMode``): exactly one of
   ``system_prompt`` / a fully-populated ``llm`` block must be
   provided. Core's validator is looser (it allows both); the SDK
   matches the CLI so the public surfaces agree.
2. ``CallCreate`` is configured with ``populate_by_name=True`` so
   ``CallCreate(from_="+1...")`` works at the Python boundary while
   ``model_dump(by_alias=True)`` still emits ``"from"`` on the wire.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
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


TERMINAL_CALL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "busy", "no_answer", "canceled"}
)


NumberType = Literal["local", "mobile", "toll_free"]


class CallCreate(BaseModel):
    """Body shape for ``POST /calls``.

    Mode A: pass ``system_prompt``. Mode B: pass a full ``llm`` block. Exactly
    one is required (mirrors the CLI's ``--prompt`` vs. ``--llm-*`` rule).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    to: str
    from_: str | None = Field(default=None, alias="from")
    system_prompt: str | None = None
    llm: LLMConfig | None = None
    first_message: str | None = None
    voice_config: VoiceConfig = Field(default_factory=VoiceConfig)
    conversation_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("to", "from_")
    @classmethod
    def _validate_e164(cls, v: str | None) -> str | None:
        if v is not None and not E164.match(v):
            raise ValueError("must be E.164 (e.g. +14155551234)")
        return v

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "CallCreate":
        has_prompt = self.system_prompt is not None and self.system_prompt != ""
        has_llm = self.llm is not None
        if has_prompt and has_llm:
            raise ValueError(
                "system_prompt and llm are mutually exclusive (use one mode)"
            )
        if not has_prompt and not has_llm:
            raise ValueError("must provide either system_prompt or a full llm block")
        return self


class CallResponse(BaseModel):
    """Shape returned by ``POST /calls`` and ``GET /calls/{id}``."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    conversation_id: UUID | None = None
    from_e164: str
    to_e164: str
    direction: Literal["outbound", "inbound"]
    status: CallStatus
    end_reason: str | None = None
    provider_call_sid: str | None = None
    livekit_room: str | None = None
    initial_prompt: str | None = None
    recording_s3_key: str | None = None
    requested_at: datetime
    started_at: datetime | None = None
    answered_at: datetime | None = None
    ended_at: datetime | None = None


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
    # Only populated when the ``id`` filter resolves to a single call. Org-wide
    # tails leave this null — there's no single "the" status.
    call_status: CallStatus | None = None


__all__ = [
    "E164",
    "CallStatus",
    "TERMINAL_CALL_STATUSES",
    "NumberType",
    "LLMConfig",
    "VoiceConfig",
    "CallCreate",
    "CallResponse",
    "CallListResponse",
    "CallEventResponse",
    "EventStreamResponse",
]
