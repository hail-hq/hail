import base64
import json
import re
from datetime import datetime
from functools import cache
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import phonenumbers
import regex
from hailhq.core.contact_ids import normalize_contact_id
from hailhq.core.file_uploads import (
    VERIFICATION_FILE_TYPES,
    Purpose,
    valid_content_type,
)
from hailhq.core.hail_mail import ROLE_MAILBOXES
from hailhq.core.languages import Language
from hailhq.core.regions import REGIONS
from hailhq.core.sender_id import PLATFORM_DEFAULT_SENDER_ID
from hailhq.core.webhook_events import WebhookEventType
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationInfo,
    computed_field,
    field_validator,
    model_validator,
)

# ``[0-9]``, not ``\d``: ``\d`` also matches non-ASCII digits, and this
# pattern is reused where Python flags do not carry (pydantic ``pattern=``).
E164 = re.compile(r"^\+[1-9][0-9]{1,14}$", re.ASCII)


def _e164_or_error(v: str | None) -> str | None:
    """Shared to/from validator for the phone-channel create schemas
    (``CallCreate``, ``SmsCreate``) — one place to tighten the rule or
    reword the error."""
    # fullmatch: ``$`` with ``match`` also accepts a trailing newline.
    if v is not None and not E164.fullmatch(v):
        raise ValueError("must be E.164 (e.g. +14155551234)")
    return v


# --------------------------------------------------------------------------- #
# Cursor codec.
#
# Wire format: base64(urlsafe-no-pad) of "<isoformat>|<uuid>". Used by every
# cursor-paginated route (calls list, events stream). The CLI's
# ``encodeEventCursor`` mirrors this byte-for-byte. ``decode_cursor`` raises
# ``ValueError``; routes wrap it as a 400.
# --------------------------------------------------------------------------- #


def encode_cursor(ts: datetime, id_: UUID) -> str:
    raw = f"{ts.isoformat()}|{id_}".encode()
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
# convention.
#
# Unknown types fail closed with a 422 so a client never silently gets back
# zero rows for a typo. The list lives here so the helper, the route, and
# (eventually) the SDK share one source.
# --------------------------------------------------------------------------- #

SUPPORTED_RESOURCE_TYPES: tuple[str, ...] = ("call", "email", "sms")


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

    base_url: str = Field(
        description=(
            "Public HTTPS base URL of the BYO LLM endpoint the call runs "
            "on. Must resolve to a public address — private/internal "
            "hosts are rejected."
        )
    )
    api_key: str = Field(
        description="API key sent to the BYO LLM endpoint. Write-only — never echoed back."
    )
    model: str = Field(description="Model name to request from the BYO LLM endpoint.")

    @field_validator("base_url")
    @classmethod
    def _base_url_is_public_https(cls, v: str) -> str:
        """Cheap syntactic check only — no name resolution.

        This runs synchronously inside pydantic body validation on the API
        worker's event loop. Resolving the host (``assert_public_https_url``)
        does blocking DNS and must never run here — a slow-resolving
        attacker-controlled domain would stall the whole worker. The full
        resolving SSRF guard runs later, off the loop, in the route (see
        ``api/routes/calls.py``) and again at call time in the voicebot.
        """
        parts = urlsplit(v)
        if parts.scheme != "https":
            raise ValueError(f"base_url must use https, got '{parts.scheme or v}'")
        if not parts.hostname:
            raise ValueError("base_url has no host")
        return v


class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tts: Literal["cartesia"] = Field(
        default="cartesia",
        description="Text-to-speech provider. Currently only 'cartesia'.",
    )
    vad: Literal["silero"] = Field(
        default="silero",
        description="Voice-activity-detection engine. Currently only 'silero'.",
    )
    turn_detection: Literal["livekit"] = Field(
        default="livekit",
        description="Turn-detection engine. Currently only 'livekit'.",
    )
    # Per-call TTS voice override. Applies to whichever TTS provider serves
    # the call (org BYO config or Hail default). None → org/env default.
    voice_id: str | None = Field(
        default=None,
        description=(
            "Per-call TTS voice override, applied to whichever TTS "
            "provider serves the call. Omitted: the organization's or "
            "environment's default voice."
        ),
    )
    language: Language | None = Field(
        default=None,
        description=(
            "Spoken language for the call as a lowercase ISO 639-1 code "
            "(e.g. 'da'). One of the 39 supported codes — see "
            "docs/languages.md. Applied to STT, TTS, and turn detection. "
            "Omitted: the providers' defaults (English)."
        ),
    )


class ConsentAttestationMixin(BaseModel):
    """Shared consent-attestation fields for every outbound-send schema
    (Call, Email, Sms). Extracted here once a third channel needed the
    identical block — the repo's "no abstraction without two concrete
    uses" tenet: two existing copies (Call, Email) plus this one crossed
    that bar.
    """

    recipient_consent: bool = Field(
        description=(
            "Attestation that you have obtained the lawful consent required "
            "to contact this recipient. Hail does not verify consent itself "
            "— you are responsible for a lawful basis under TCPA/ePrivacy/"
            "PECR/CAN-SPAM/GDPR as applicable. Rejected (422) if not true."
        )
    )
    consent_source: str | None = Field(
        default=None,
        description=(
            "Where/how consent was obtained (e.g. 'signup form', "
            "'prior customer relationship'). Required (non-empty) when "
            "message_type is 'marketing'."
        ),
    )
    consent_obtained_at: datetime | None = Field(
        default=None, description="When consent was obtained, if known."
    )
    message_type: Literal["marketing", "informational"] = Field(
        default="informational",
        description=(
            "'marketing' additionally requires a non-empty consent_source. "
            "Use 'informational' for transactional/service communications."
        ),
    )


# Call.metadata keys Hail writes for recording state; POST /calls rejects them.
RESERVED_CALL_METADATA_PREFIX = "recording_"
# Call.metadata keys of an ownership check call (hailhq.core.sip_connections);
# POST /calls rejects them too.
PBX_CHECK_METADATA_PREFIX = "pbx_check_"


class CallCreate(ConsentAttestationMixin):
    model_config = ConfigDict(extra="forbid")

    to: str = Field(
        description="Recipient phone number, E.164 format (e.g. +14155551234)."
    )
    from_: str | None = Field(
        default=None,
        alias="from",
        description=(
            "Caller-id phone number, E.164 format. Must be a number owned "
            "by the organization with the voice capability. Omitted: an "
            "active org-owned number is used if one exists, else a number "
            "is claimed from the shared pool."
        ),
    )
    system_prompt: str | None = Field(
        default=None,
        description=(
            "Task instructions for the agent, sent as its leading system "
            "message. At least one of system_prompt or llm is required; "
            "both together is also valid."
        ),
    )
    llm: LLMConfig | None = Field(
        default=None,
        description=(
            "BYO LLM endpoint the call runs on instead of Hail's default "
            "model. At least one of system_prompt or llm is required; both "
            "together is also valid."
        ),
    )
    first_message: str | None = Field(
        default=None,
        description=(
            "Opening line the agent speaks first. Omitted: the agent waits "
            "for the callee to speak first."
        ),
    )
    recording_enabled: bool | None = Field(
        default=None,
        description="Best-effort audio recording for this call. true records and false does not, whatever the workspace setting. Null inherits the agent, then the workspace. Recording failure never prevents the call. The recording-enabled call rate applies regardless of capture outcome.",
    )
    ai_disclosure: bool = Field(
        default=True,
        description=(
            "Speak the AI self-disclosure line ('Hi, this is an AI "
            "assistant calling on behalf of ...') as the first thing on "
            "the call. Enabled by default. Disable only if you have "
            "verified the disclosure is not required for this call — 47 "
            "CFR 64.1200(b)(1) requires identifying the initiating "
            "business at the start of artificial-voice calls in the US, "
            "and several jurisdictions have AI bot-disclosure laws. Hail "
            "does not verify this for you. The agent still identifies "
            "itself as an AI if asked."
        ),
    )
    voice_config: VoiceConfig = Field(
        default_factory=VoiceConfig,
        description=(
            "TTS voice, VAD, turn-detection, and spoken-language settings "
            "for this call."
        ),
    )
    metadata: dict = Field(
        default_factory=dict,
        description=(
            "Free-form JSON object attached to the call and echoed back on "
            "reads. Not interpreted by Hail. Keys starting with "
            "'recording_' and the key 'failure_code' are reserved and rejected."
        ),
    )

    @field_validator("metadata")
    @classmethod
    def _metadata_has_no_reserved_keys(cls, v: dict) -> dict:
        # Recording state (egress id, object key, status) lives under these
        # keys and is read back as Hail-owned by the voicebot, DSAR erasure
        # and retention. Ownership check calls are found by theirs. A caller
        # must not be able to plant them.
        bad = sorted(
            k
            for k in v
            if isinstance(k, str)
            and (
                k.startswith((RESERVED_CALL_METADATA_PREFIX, PBX_CHECK_METADATA_PREFIX))
                or k == "failure_code"
            )
        )
        if bad:
            raise ValueError(f"metadata keys are reserved: {', '.join(bad)}")
        return v

    tools: list[str] | None = Field(
        default=None,
        description=(
            "Agent tools to allow on this call. Omitted: every tool the "
            "organization's configured channels support (new channels appear "
            "automatically). Empty list: only the tools every call keeps "
            "(end_call, send_dtmf and Hail's settings guide); a saved agent's "
            "HTTP tools stay on. "
            "Tool names are validated "
            "against the server's registry."
        ),
    )
    agent_id: UUID | None = Field(
        default=None,
        description=(
            "Place the call with a saved agent (POST /agents). The agent "
            "supplies system_prompt, first_message, ai_disclosure, recording_enabled, "
            "voice_config, tools and max duration; any of those given "
            "explicitly on this request wins. Satisfies the system_prompt "
            "or llm requirement on its own. A paused agent is refused "
            "with 409."
        ),
    )

    _validate_e164 = field_validator("to", "from_")(_e164_or_error)

    @model_validator(mode="after")
    def _prompt_or_llm(self):
        """At least one of ``system_prompt`` / ``llm`` — but not exclusive.

        Both together is a legitimate combination: the call runs on the
        caller's BYO endpoint (mode B) *and* carries their task prompt.
        ``build_instructions()`` in the voicebot is mode-agnostic and
        composes ``VOICE_PREAMBLE`` + the caller prompt whichever LLM
        serves the call, so the endpoint receives the prompt as its
        leading system message.
        """
        has_prompt = self.system_prompt is not None and self.system_prompt != ""
        has_llm = self.llm is not None
        if not has_prompt and not has_llm and self.agent_id is None:
            raise ValueError("either system_prompt, llm or agent_id must be provided")
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

TERMINAL_CALL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "busy", "no_answer", "canceled"}
)


NumberType = Literal["local", "mobile", "toll_free", "national"]
Capability = Literal["voice", "sms"]


class CallTranscriptTurn(BaseModel):
    role: Literal["user", "assistant"] = Field(
        description=(
            "'assistant' is the Hail agent. 'user' is the person (or "
            "answering machine) on the other end of the line."
        )
    )
    text: str = Field(description="What was said in this turn.")
    at: datetime = Field(
        description=(
            "When the speaker started this turn, ISO 8601. When that is not "
            "known, when Hail recorded the turn."
        )
    )


class CallResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Unique identifier for this call.")
    organization_id: UUID = Field(
        description="Organization that placed or received this call."
    )
    from_e164: str = Field(description="Caller-id phone number used, E.164 format.")
    to_e164: str = Field(description="Recipient phone number, E.164 format.")
    direction: Literal["outbound", "inbound"] = Field(
        description="'outbound' for calls Hail placed, 'inbound' for calls received."
    )
    agent_id: UUID | None = Field(
        default=None,
        description=(
            "Agent that answered (inbound) or placed (outbound) this call. "
            "Null when the call was placed with an inline system_prompt."
        ),
    )
    status: CallStatus = Field(
        description=(
            "Current call-progress state: 'queued', 'dialing', 'ringing', "
            "'in_progress', or one of the terminal states 'completed', "
            "'failed', 'busy', 'no_answer', 'canceled'."
        )
    )
    end_reason: str | None = Field(
        description=(
            "Machine-readable reason the call reached a terminal status "
            "(e.g. 'normal_hangup', 'user_rejected', 'sip_trunk_failure'). "
            "Null while the call is still in progress."
        )
    )
    provider_call_sid: str | None = Field(
        description="The telephony provider's identifier for this call leg, if assigned."
    )
    livekit_room: str | None = Field(
        description="Name of the LiveKit room hosting this call's media session, if one was created."
    )
    initial_prompt: str | None = Field(
        description="The system_prompt this call was created with, if any."
    )
    recording_enabled: bool = Field(
        default=False,
        description="Whether the recording-enabled call rate applies, regardless of capture outcome.",
    )
    recording_status: str = Field(
        default="disabled",
        description="disabled, requested, starting, recording, processing, ready, unavailable, failed, or erased.",
    )
    recording_duration_ms: int | None = Field(
        default=None,
        description="Duration of captured audio, independent of billed call duration.",
    )
    recording_s3_key: str | None = Field(
        description="Internal storage key for the call recording. Not a directly fetchable URL."
    )
    requested_at: datetime = Field(
        description="When the call was requested, ISO 8601 timestamp."
    )
    started_at: datetime | None = Field(
        description="When dialing began, ISO 8601 timestamp. Null until the call starts."
    )
    answered_at: datetime | None = Field(
        description="When the callee answered, ISO 8601 timestamp. Null if never answered."
    )
    ended_at: datetime | None = Field(
        description="When the call ended, ISO 8601 timestamp. Null while still in progress."
    )
    failure_message: str | None = Field(
        default=None,
        description="Human-readable explanation for a failed call. Null for other statuses.",
    )
    transcript: list[CallTranscriptTurn] | None = Field(
        default=None,
        description=(
            "What was said on the call, in the order it was said. Set only by "
            "GET /v1/calls/{call_id}: an empty list until someone speaks. "
            "Null in POST /v1/calls and GET /v1/calls responses."
        ),
    )


class CallListResponse(BaseModel):
    items: list[CallResponse] = Field(description="Calls in this page, newest first.")
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Null when there are no more results.",
    )


class SmsCreate(ConsentAttestationMixin):
    model_config = ConfigDict(extra="forbid")

    to: str = Field(
        description="Recipient phone number, E.164 format (e.g. +14155551234)."
    )
    from_: str | None = Field(
        default=None,
        alias="from",
        description=(
            "Sender phone number, E.164 format. Must be an active number owned by "
            "the organization with the SMS capability. Omitted: destinations "
            "where the sender-ID carrier delivers a dynamic alphanumeric sender "
            "ID unchanged (about 110 countries: most of Europe, Africa, the Middle "
            "East, Asia-Pacific and the Caribbean; one table per carrier in "
            "core/hailhq/core/sender_id_corridors.py) use the organization's sender ID, or "
            "the platform default 'HAIL' when none is set; Australia (+61) may "
            "always use 'HAIL', depending on the carrier. Every other destination, "
            "including the US and Canada, "
            "uses the organization's oldest active SMS-capable number; if none "
            "exists the request fails with 422. SMS never uses the shared pool. "
            "Trial texts (only to the workspace's verified trial phone): "
            "with no from, Hail uses one when the workspace's own "
            "number cannot text that US phone; set from to the trial number "
            "to use one directly."
        ),
    )
    body: str = Field(
        min_length=1,
        max_length=1600,
        description="Message text. Long bodies are split into multiple carrier segments.",
    )
    metadata: dict = Field(
        default_factory=dict,
        description=(
            "Free-form JSON object attached to the message and echoed back "
            "on reads. Not interpreted by Hail. The key 'sms_trial' is "
            "reserved and rejected."
        ),
    )

    _validate_e164 = field_validator("to", "from_")(_e164_or_error)

    @field_validator("metadata")
    @classmethod
    def _metadata_has_no_reserved_keys(cls, v: dict) -> dict:
        # Trial texts are counted by this key (sms_trial.SMS_TRIAL_META_KEY;
        # a literal here because models imports this module). A caller must
        # not be able to plant it.
        if "sms_trial" in v:
            raise ValueError("metadata keys are reserved: sms_trial")
        return v


SmsStatus = Literal["queued", "sent", "delivered", "failed", "undelivered", "received"]


class SmsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Unique identifier for this message.")
    organization_id: UUID = Field(
        description="Organization that sent or received this message."
    )
    from_e164: str = Field(description="Sender phone number, E.164 format.")
    to_e164: str = Field(description="Recipient phone number, E.164 format.")
    direction: Literal["outbound", "inbound"] = Field(
        description="'outbound' for messages Hail sent, 'inbound' for messages received."
    )
    agent_id: UUID | None = Field(
        default=None,
        description=(
            "The agent this text belongs to: the number's text agent on inbound "
            "texts, the agent that wrote it on outbound texts. Null when no "
            "agent is involved."
        ),
    )
    status: SmsStatus = Field(
        description=(
            "Delivery status: 'queued', 'sent', 'delivered', 'failed', "
            "'undelivered', or 'received' (inbound messages)."
        )
    )
    body: str = Field(description="Message text.")
    provider_message_sid: str | None = Field(
        description="The carrier/provider's identifier for this message, if assigned."
    )
    segment_count: int = Field(
        description="Number of carrier SMS segments the body was split into."
    )
    error_code: str | None = Field(
        description="Carrier error code if delivery failed. Null on success or while pending."
    )
    requested_at: datetime = Field(
        description="When the send was requested, ISO 8601 timestamp."
    )
    sent_at: datetime | None = Field(
        description="When the message was handed to the carrier, ISO 8601 timestamp. Null until sent."
    )


class SmsListResponse(BaseModel):
    items: list[SmsResponse] = Field(description="Messages in this page, newest first.")
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Null when there are no more results.",
    )


# ``\Z`` (not ``$``) so a trailing newline is rejected: in Python ``$`` also
# matches just before a final ``\n``, which would let "ACME\n" through and be
# stored as a malformed Sender ID.
SENDER_ID_RE = re.compile(r"^[A-Za-z0-9]{2,11}\Z")


class SenderIdPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    custom_sender_id: str | None = Field(
        default=None,
        description=(
            "Alphanumeric sender id (2-11 characters, letters/digits only) "
            "to use on alphanumeric-eligible corridors instead of a phone "
            "number. Explicit null clears it, reverting to the platform "
            "default."
        ),
    )

    @field_validator("custom_sender_id")
    @classmethod
    def _validate_sender_id(cls, v: str | None) -> str | None:
        if v is not None and not SENDER_ID_RE.match(v):
            raise ValueError(
                "must be 2-11 alphanumeric characters, no spaces or symbols"
            )
        return v


class SenderIdResponse(BaseModel):
    custom_sender_id: str | None = Field(
        description="The organization's configured alphanumeric sender id. Null if none is set."
    )
    effective_default: str = Field(
        default=PLATFORM_DEFAULT_SENDER_ID,
        description=(
            "The platform's alphanumeric sender id, used on eligible "
            "corridors when custom_sender_id is null."
        ),
    )


class SmsTrialResponse(BaseModel):
    configured: bool = Field(
        description="True when this Hail deployment has a trial number set up."
    )
    available: bool = Field(
        description=(
            "True when trial texts can be sent now. False while the trial "
            "number waits for carrier approval."
        )
    )
    status: Literal["none", "pending", "verified"] = Field(
        description=(
            "The workspace's trial phone: 'none' (no phone yet), 'pending' "
            "(code sent, not confirmed) or 'verified' (receives trial texts)."
        )
    )
    phone: str | None = Field(
        description="The trial phone in E.164. Null when status is 'none'."
    )
    pending_phone: str | None = Field(
        description=(
            "A new phone in E.164 that got a code and is not confirmed yet. "
            "Until it is, the verified phone keeps getting trial texts. Null "
            "when no new phone waits."
        )
    )
    code_expires_at: datetime | None = Field(
        description=(
            "When the open code expires, for pending_phone when set, else for "
            "phone. Null when no code is open."
        )
    )
    texts_used: int = Field(description="Trial texts this workspace has sent.")
    texts_limit: int = Field(description="All the trial texts the workspace gets.")
    texts_left: int = Field(description="Trial texts this workspace can still send.")


class SmsTrialPhoneStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: str = Field(
        description=(
            "Your own US mobile in E.164 (e.g. +14155551234). Hail texts a "
            "6-digit code to it."
        )
    )
    recipient_consent: Literal[True] = Field(
        description=(
            "Must be true: the phone's owner agreed to get the code and the "
            "trial texts."
        )
    )
    consent_version: str = Field(
        min_length=1,
        max_length=40,
        description="Version of the consent wording the person agreed to.",
    )

    _validate_e164 = field_validator("phone")(_e164_or_error)


class SmsTrialPhoneConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        # ``[0-9]``: ``\d`` also matches non-ASCII digits.
        pattern=r"^[0-9]{6}$",
        description="The 6-digit code Hail texted to the trial phone.",
    )


class NumberAcquireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: UUID = Field(
        description="Unexpired organization-bound quote from POST /numbers/quotes. Required: without it the request is a 422; get a quote first.",
    )
    provider: str | None = Field(
        default=None,
        deprecated=True,
        description="Deprecated and ignored. Hail buys the quoted number from the carrier it picked.",
    )

    country_code: str = Field(
        min_length=2,
        max_length=2,
        description="ISO alpha-2 country code to acquire a number in (e.g. 'US'). Case-insensitive.",
    )
    number_type: NumberType = Field(
        default="local",
        description="Kind of number to acquire: 'local', 'mobile', 'toll_free', or 'national'.",
    )
    expected_total_cents: int | None = Field(
        default=None,
        ge=0,
        description=(
            "The total the caller expects to pay now: the quoted monthly price "
            "plus the setup price, in cents. When set, the order is refused "
            "with 409 if the quoted total differs."
        ),
    )
    subject_type: Literal["person", "business"] | None = Field(
        default=None,
        description=(
            "Buy with an approved verification of this kind only: 'person' "
            "(you) or 'business' (your company). With `verification_id` it "
            "must match that verification. Ignored when the number needs no "
            "verification."
        ),
    )
    verification_id: UUID | None = Field(
        default=None,
        description=(
            "Hail id of the approved verification to buy with, from the "
            "quote's `verifications`. Needed when you have more than one "
            "approved verification for this number (422 without it); with one "
            "it is used. 422 when it is not one of your approved "
            "verifications for this number. Ignored when the number needs no "
            "verification."
        ),
    )

    @field_validator("country_code")
    @classmethod
    def _uppercase_country_code(cls, v: str) -> str:
        # The telephony catalog, the provider, and the stored row all key on
        # uppercase ISO alpha-2; normalize here so "us" and "US" behave alike.
        return v.upper()


@cache
def _landline_pattern(country: str) -> "regex.Pattern[str] | None":
    """The national landline number pattern of ``country``, or None when
    libphonenumber has no landline data for it."""
    metadata = phonenumbers.PhoneMetadata.metadata_for_region(country)
    if metadata is None or metadata.fixed_line is None:
        return None
    pattern = metadata.fixed_line.national_number_pattern
    return regex.compile(pattern) if pattern else None


def _starts_a_landline(country: str, digits: str) -> bool:
    """True when some landline number in ``country`` starts with ``digits``
    (after the country code). ``partial`` accepts a prefix of a full match."""
    pattern = _landline_pattern(country)
    return pattern is not None and pattern.fullmatch(digits, partial=True) is not None


class NumberQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country_code: str = Field(
        min_length=2,
        max_length=2,
        description="ISO alpha-2 country code to search. Case-insensitive.",
    )
    number_type: NumberType | None = Field(
        default=None,
        description="Restrict number type; omit to compare all supported types.",
    )
    capabilities: list[Capability] = Field(
        min_length=1,
        max_length=2,
        description="Required channels; every returned offer must support all requested capabilities.",
    )
    provider: str | None = Field(
        default=None,
        deprecated=True,
        description="Deprecated and ignored. Hail compares every carrier by readiness, remaining verification effort, and rental and setup costs.",
    )
    area_code: str | None = Field(
        default=None,
        pattern=r"^[0-9]{1,6}$",
        description="Area code or first digits of the number, as dialed inside the country: 020 or 20 (London), 480 (Arizona). Any country; every returned offer starts with these digits.",
    )
    city: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="City name as the carriers list it, such as London or Phoenix. Each offer's locality and region tell same-named cities apart.",
    )
    region: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z]{2}$",
        description="Optional, with city: a US state or Canadian province code, such as AZ. GET /numbers/regions lists the codes.",
    )
    subject_type: Literal["person", "business"] | None = Field(
        default=None,
        description="Who the number is for: 'person' (you) or 'business' (your company). Offers that do not accept that subject are left out, the rest are ranked for it, and each offer lists its `paperwork`. Omit to get every offer as before.",
    )

    @field_validator("country_code")
    @classmethod
    def _uppercase_country_code(cls, v: str) -> str:
        v = v.upper()
        if not re.fullmatch(r"[A-Z]{2}", v):
            raise ValueError("country_code must be two letters")
        return v

    @field_validator("area_code")
    @classmethod
    def _national_digits(cls, v: str | None, info: ValidationInfo) -> str | None:
        # People type the digits as dialed at home: 020 in the UK. Carriers
        # match the digits after the country code (20), so drop the country's
        # trunk prefix. Some area codes start with the trunk digit (Russia
        # dials 8, and 812 is St Petersburg): keep the digits as typed when
        # they start a landline number and the stripped form does not. When
        # both forms do (8342 Saransk, 342 Perm), the trunk digit wins,
        # because people dial it first. Landline data only: no RU/KZ mobile
        # range starts with 8. ``country_code`` is declared first, so its
        # validated value is in ``info.data`` unless it failed on its own.
        if v is None:
            return None
        country = info.data.get("country_code") or ""
        metadata = phonenumbers.PhoneMetadata.metadata_for_region(country)
        trunk = metadata.national_prefix if metadata else None
        if trunk and v.startswith(trunk) and len(v) > len(trunk):
            stripped = v[len(trunk) :]
            if _starts_a_landline(country, v) and not _starts_a_landline(
                country, stripped
            ):
                return v
            return stripped
        return v

    @field_validator("city")
    @classmethod
    def _strip_city(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("city must not be blank")
        return v.strip() if v is not None else None

    @field_validator("region")
    @classmethod
    def _region_needs_nanp(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is None:
            return None
        country = info.data.get("country_code")
        if country not in REGIONS:
            raise ValueError("region works for US and CA numbers only")
        v = v.upper()
        if v not in REGIONS[country]:
            raise ValueError(
                f"{v} is not a {country} state or province code; "
                "GET /numbers/regions lists them"
            )
        return v

    @model_validator(mode="after")
    def _region_needs_city(self) -> "NumberQuoteRequest":
        # region narrows a city to one state; a state alone is not a search.
        if self.region is not None and self.city is None:
            raise ValueError("send region with a city")
        return self


class PhoneNumberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Unique identifier for this number.")
    e164: str = Field(description="The phone number, E.164 format.")
    country_code: str = Field(
        description="ISO alpha-2 country code this number belongs to."
    )
    number_type: str = Field(
        description="Kind of number: 'local', 'mobile', 'toll_free', or 'national'."
    )
    capabilities: list[str] = Field(
        description="Channels this number supports, e.g. ['voice'], ['sms'], or both."
    )
    provisioning_state: str = Field(
        description=(
            "'pending', 'active', 'failed', or 'released'. A connected number "
            "is 'unverified' until it passes its ownership check "
            "(POST /sip-connections/{id}/numbers/{number_id}/verify)."
        )
    )
    source: Literal["purchased", "connected"] = Field(
        default="purchased",
        description=(
            "'purchased': bought through Hail. 'connected': a number on your "
            "own phone system, connected with POST /sip-connections/{id}/numbers. "
            "Removing a connected number takes it off Hail only; it stays "
            "with your phone system and provider."
        ),
    )
    sip_connection_id: UUID | None = Field(
        default=None,
        description="The phone system connection of a connected number. Null for a purchased number.",
    )
    is_dedicated: bool = Field(
        description="True if this number is owned by the organization. False for shared-pool numbers."
    )
    voice_agent_id: UUID | None = Field(
        default=None,
        description="Agent that answers calls to this number. Null: calls ring out.",
    )
    sms_agent_id: UUID | None = Field(
        default=None,
        description="Agent that answers texts to this number. Null: texts only reach your webhooks.",
    )
    inbound_registered: bool = Field(
        default=False,
        description=(
            "True while the number is attached for inbound calls at the carrier "
            "and on Hail's LiveKit inbound trunk. Set when voice_agent_id is set."
        ),
    )
    subject_type: Literal["person", "business"] | None = Field(
        default=None,
        description=(
            "Who the verification used to buy this number is for: 'person' "
            "or 'business'. Null when no verification was used."
        ),
    )
    verification_id: UUID | None = Field(
        default=None,
        description=(
            "Hail id of the verification used to buy this number. Null when "
            "no verification was used."
        ),
    )
    subject_name: str | None = Field(
        default=None,
        description=(
            "Name of the person or company the verification used to buy this "
            "number is for. Null when no verification was used or the name "
            "is unknown."
        ),
    )


# --------------------------------------------------------------------------- #
# Connected phone systems (/sip-connections)
# --------------------------------------------------------------------------- #

PbxTransport = Literal["auto", "udp", "tcp", "tls"]


class SipConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        min_length=1, max_length=80, description="A name, such as 'Main office'."
    )
    default_region: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{2}$",
        description=(
            "Optional. ISO alpha-2 country of the phone system; Hail reads "
            "national caller numbers with it. The first number you add sets it "
            "to that number's country."
        ),
    )
    pbx_host: str | None = Field(
        default=None,
        description=(
            "Public host name or IP address where Hail dials your phone system "
            "for outbound calls and handover. Null: Hail only answers calls."
        ),
    )
    pbx_port: int = Field(default=5060, ge=1, le=65535, description="SIP port.")
    pbx_transport: PbxTransport = Field(default="auto", description="SIP transport.")
    pbx_username: str | None = Field(
        default=None,
        description="Username Hail sends when your phone system asks for one.",
    )
    pbx_password: str | None = Field(
        default=None,
        description="Password Hail sends with pbx_username. Stored encrypted.",
    )


class SipConnectionPatch(BaseModel):
    """Fields left out stay as they are; ``null`` clears a nullable field."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(
        default=None, min_length=1, max_length=80, description="New name."
    )
    pbx_host: str | None = Field(
        default=None,
        description=(
            "New host. Null removes it: Hail then only answers calls. A new "
            "host, port or transport needs pbx_password again when a "
            "pbx_username is set."
        ),
    )
    pbx_port: int | None = Field(
        default=None, ge=1, le=65535, description="New SIP port."
    )
    pbx_transport: PbxTransport | None = Field(
        default=None, description="New SIP transport."
    )
    pbx_username: str | None = Field(
        default=None,
        description="New username Hail sends. Null removes it and the password.",
    )
    pbx_password: str | None = Field(
        default=None, description="New password Hail sends. Stored encrypted."
    )

    @model_validator(mode="after")
    def _not_null(self) -> "SipConnectionPatch":
        for name in ("label", "pbx_port", "pbx_transport"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class SipConnectionResponse(BaseModel):
    id: UUID = Field(description="Unique identifier for this connection.")
    label: str = Field(description="Name of the connection.")
    default_region: str = Field(description="ISO alpha-2 country of the phone system.")
    sip_host: str = Field(
        description=(
            "SIP server your phone system sends calls to (the trunk's host). "
            "Shows only its first characters when sip_host_masked is true."
        )
    )
    sip_host_masked: bool = Field(
        description=(
            "True: sip_host shows only its first characters. Support sends "
            "you the full address."
        )
    )
    username: str = Field(description="Username your phone system sends to Hail.")
    password_last4: str = Field(
        description="Last 4 characters of the password. Read the full one with GET /sip-connections/{id}/password."
    )
    pbx_host: str | None = Field(
        description="Where Hail dials your phone system. Null: Hail only answers calls."
    )
    pbx_port: int = Field(description="SIP port of your phone system.")
    pbx_transport: str = Field(description="'auto', 'udp', 'tcp' or 'tls'.")
    pbx_username: str | None = Field(
        description="Username Hail sends to your phone system. Null: none."
    )
    pbx_password_set: bool = Field(
        description="True when Hail has a password to send to your phone system."
    )
    created_at: datetime = Field(description="When the connection was created.")


class SipConnectionCreated(SipConnectionResponse):
    password: str = Field(description="Password your phone system sends to Hail.")


class SipConnectionPassword(BaseModel):
    password: str = Field(description="The password your phone system sends to Hail.")


class SipConnectionListResponse(BaseModel):
    items: list[SipConnectionResponse] = Field(
        description="Connections in this page, newest first."
    )
    next_cursor: str | None = Field(
        default=None, description="Pass as cursor for the next page. Null at the end."
    )


class ConnectedNumberCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    e164: str = Field(
        description="Your number with its country code, such as +14155550100."
    )


class ConnectedNumberCheck(BaseModel):
    state: Literal["active", "unverified"] = Field(
        description="'active': the number passed and can be routed to an agent."
    )
    reason: (
        Literal["no_call_arrived", "not_authenticated", "code_not_received"] | None
    ) = Field(
        default=None,
        description=(
            "Why the check failed. 'no_call_arrived': no call reached this "
            "connection; check the trunk and its username and password, and "
            "that a routing rule sends this number to Hail with + and the "
            "country code. 'not_authenticated': the call came in on another "
            "connection's trunk, for example with another connection's "
            "username and password. 'code_not_received': "
            "the check code did not come through; turn on RFC 2833 key presses."
        ),
    )
    call_id: UUID | None = Field(
        default=None, description="The check call, billed as an outbound call."
    )


class PhoneNumberRoutingUpdate(BaseModel):
    """PATCH /numbers/{id}: which agent answers. A field left out keeps its
    value; ``null`` detaches."""

    model_config = ConfigDict(extra="forbid")

    voice_agent_id: UUID | None = Field(
        default=None,
        description=(
            "Agent that answers calls to this number. Setting it registers the "
            "number for inbound calls at the carrier and at LiveKit; null "
            "unregisters it. The number needs the voice capability."
        ),
    )
    sms_agent_id: UUID | None = Field(
        default=None,
        description=(
            "Agent that answers texts to this number. The number needs the sms "
            "capability. Null: texts only reach your webhooks."
        ),
    )

    @model_validator(mode="after")
    def _at_least_one(self):
        if not self.model_fields_set:
            raise ValueError("send voice_agent_id and/or sms_agent_id")
        return self


AgentStatus = Literal["live", "paused"]

_AGENT_LINE_MAX = 300


class HandoverContactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: str = Field(
        description=(
            "A person with a phone number, as GET /contacts returns it: a "
            "contact's id, or `member:<user id>` for a team member."
        ),
        examples=["member:6f1c2a9e-3b7d-4c51-9a8e-2d4f6b1e0c37"],
    )
    note: str = Field(
        min_length=1,
        max_length=200,
        description="When the agent should hand over to this person, e.g. 'billing questions'. Read by the agent.",
    )

    @field_validator("contact_id")
    @classmethod
    def _wire_id(cls, v: str) -> str:
        try:
            return normalize_contact_id(v)
        except ValueError as exc:
            raise ValueError(
                "must be a contact id or member:<user id>, as GET /contacts returns"
            ) from exc


class HandoverContactOut(BaseModel):
    contact_id: str = Field(
        description="The person: a contact's id, or `member:<user id>` for a team member."
    )
    name: str = Field(description="Name, as the agent says it.")
    phone_e164: str | None = Field(
        description="Number Hail dials. Null when the contact lost its number; it is then skipped."
    )
    note: str = Field(description="When the agent hands over to this person.")


# Customer HTTP tools (agent_tools.http_tool). Names the model already has
# from Hail; an HTTP tool may not take one. tests/test_http_tool_schemas.py
# checks this covers every built-in tool.
RESERVED_TOOL_NAMES = frozenset(
    {
        "cancel_transfer",
        "end_call",
        "hail_settings_help",
        "list_contacts",
        "read_browser_inbox",
        "read_browser_texts",
        "read_email",
        "send_dtmf",
        "send_email",
        "send_sms",
        "thread_history",
        "transfer_call",
    }
)
HTTP_TOOLS_MAX = 10
HTTP_TOOL_TIMEOUT_DEFAULT = 5
HTTP_TOOL_TIMEOUT_MAX = 10
_HTTP_TOOL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# Headers Hail sets itself, or that would break the request.
_RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "user-agent",
    }
)
_HTTP_TOOL_HEADERS_MAX = 10
# One type per node, no "null" and no additionalProperties/pattern/format:
# Gemini (in the house fallback chain) rejects those in function schemas.
_PARAM_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object"})
_PARAM_KEYS = frozenset(
    {
        "type",
        "description",
        "enum",
        "items",
        "properties",
        "required",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    }
)
_PARAM_NUMBER_KEYS = ("minimum", "maximum")
_PARAM_COUNT_KEYS = ("minLength", "maxLength", "minItems", "maxItems")
_PARAM_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
_PARAMS_MAX_CHARS = 8000
_PARAMS_MAX_DEPTH = 4


def _check_param_schema(node: Any, path: str, depth: int) -> None:
    """A small JSON Schema subset every LLM provider accepts. A schema a
    provider rejects would fail every model turn, not only this tool."""
    if type(node) is not dict:  # a ValueError, so pydantic reports it
        raise ValueError(f"{path} must be an object")
    if depth > _PARAMS_MAX_DEPTH:
        raise ValueError(f"{path} nests deeper than {_PARAMS_MAX_DEPTH} levels")
    unknown = sorted(set(node) - _PARAM_KEYS)
    if unknown:
        raise ValueError(f"{path} has unsupported keywords: {', '.join(unknown)}")
    kind = node.get("type")
    if not isinstance(kind, str) or kind not in _PARAM_TYPES:
        raise ValueError(
            f"{path}.type must be one of {', '.join(sorted(_PARAM_TYPES))}"
        )
    if "description" in node and not isinstance(node["description"], str):
        raise ValueError(f"{path}.description must be a string")
    if "enum" in node:
        enum = node["enum"]
        if (
            not isinstance(enum, list)
            or not enum
            or not all(isinstance(e, str) for e in enum)
        ):
            raise ValueError(f"{path}.enum must be a non-empty list of strings")
    for key in _PARAM_NUMBER_KEYS:
        value = node.get(key)
        if key in node and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise ValueError(f"{path}.{key} must be a number")
    for key in _PARAM_COUNT_KEYS:
        value = node.get(key)
        if key in node and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"{path}.{key} must be a whole number of 0 or more")
    if "items" in node and kind != "array":
        raise ValueError(f"{path}.items is only for type array")
    if "properties" in node and kind != "object":
        raise ValueError(f"{path}.properties is only for type object")
    if "items" in node:
        _check_param_schema(node["items"], f"{path}.items", depth + 1)
    props = node.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            raise ValueError(f"{path}.properties must be an object")
        for name, sub in props.items():
            if not _PARAM_NAME_RE.match(name):
                raise ValueError(
                    f"{path}.properties has an invalid name {name!r} "
                    "(letters, digits, _ . -; up to 64)"
                )
            _check_param_schema(sub, f"{path}.properties.{name}", depth + 1)
    required = node.get("required")
    if required is not None:
        if not isinstance(required, list) or not all(
            isinstance(r, str) for r in required
        ):
            raise ValueError(f"{path}.required must be a list of names")
        missing = sorted(set(required) - set(props or {}))
        if missing:
            raise ValueError(
                f"{path}.required names unknown properties: {', '.join(missing)}"
            )


def validate_http_tool_url(v: str) -> str:
    """Shape only (no DNS): https, a host, no credentials. The API resolves
    the host on save and the agent again on every request. Returns the url
    with a lowercase scheme (the table checks ``url LIKE 'https://%'``)."""
    if v[:8].lower() != "https://":
        raise ValueError("url must use https")
    v = "https://" + v[8:]
    parts = urlsplit(v)
    if not parts.hostname:
        raise ValueError("url must include a host")
    if parts.username or parts.password:
        raise ValueError("url must not contain a user name or password")
    if parts.fragment:
        raise ValueError("url must not contain a fragment (#...)")
    return v


class HttpToolIn(BaseModel):
    """One customer API the agent may call during a call, text or email."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description=(
            "Function name the model calls: a letter, then letters, digits or "
            "_ (up to 64). Unique per agent; built-in tool names are not allowed."
        ),
        examples=["lookup_order"],
    )
    description: str = Field(
        min_length=1,
        max_length=1000,
        description="What the tool does and when to use it. Read by the model.",
        examples=["Look up an order's status by its order number."],
    )
    url: str = Field(
        max_length=2000,
        description=(
            "HTTPS endpoint Hail POSTs to. It must resolve to a public "
            "address; private, loopback and link-local addresses are refused."
        ),
        examples=["https://api.example.com/hail/orders"],
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
        description=(
            "JSON Schema (type object) of the arguments the model fills in. "
            "Supported keywords: type (one of string, number, integer, "
            "boolean, array, object), description, enum (strings), items, "
            "properties, required, minimum, maximum, minLength, maxLength, "
            "minItems, maxItems."
        ),
        examples=[
            {
                "type": "object",
                "properties": {
                    "order_number": {
                        "type": "string",
                        "description": "The order number the caller gives.",
                    }
                },
                "required": ["order_number"],
            }
        ],
    )
    headers: dict[str, str] | None = Field(
        default=None,
        description=(
            "Static headers sent with every request, e.g. an Authorization "
            "token (up to 10). Stored encrypted; responses show only the "
            "names. On PATCH, null or left out keeps the saved values of the "
            "tool with the same name while its url keeps the same scheme, host "
            "and port; a url on a new host needs the headers again. {} removes "
            "them."
        ),
        examples=[{"Authorization": "Bearer sk_live_example"}],
    )
    timeout_seconds: int = Field(
        default=HTTP_TOOL_TIMEOUT_DEFAULT,
        ge=1,
        le=HTTP_TOOL_TIMEOUT_MAX,
        description="Seconds to wait for the answer (1..10). Default 5.",
    )
    wait_message: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Sentence the agent says on a call while it waits, e.g. 'One "
            "moment, let me check.' Not used for texts and email."
        ),
    )

    @field_validator("name")
    @classmethod
    def _tool_name(cls, v: str) -> str:
        if not _HTTP_TOOL_NAME_RE.match(v):
            raise ValueError(
                "name must start with a letter and use only letters, digits "
                "and _ (up to 64)"
            )
        if v in RESERVED_TOOL_NAMES:
            raise ValueError(f"{v} is a built-in tool name")
        return v

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("description must not be blank")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return validate_http_tool_url(v)

    @field_validator("parameters")
    @classmethod
    def _parameters(cls, v: dict[str, Any]) -> dict[str, Any]:
        if v.get("type") != "object":
            raise ValueError("parameters.type must be 'object'")
        _check_param_schema(v, "parameters", 1)
        if len(json.dumps(v)) > _PARAMS_MAX_CHARS:
            raise ValueError(
                f"parameters is longer than {_PARAMS_MAX_CHARS} characters"
            )
        return v

    @field_validator("headers")
    @classmethod
    def _headers(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return v
        if len(v) > _HTTP_TOOL_HEADERS_MAX:
            raise ValueError(f"at most {_HTTP_TOOL_HEADERS_MAX} headers")
        seen: set[str] = set()
        for name, value in v.items():
            low = name.lower()
            if not _HEADER_NAME_RE.match(name):
                raise ValueError(f"invalid header name {name!r}")
            if low in _RESERVED_HEADERS or low.startswith("x-hail-"):
                raise ValueError(f"header {name} is set by Hail")
            if low in seen:
                raise ValueError(f"header {name} is given twice")
            seen.add(low)
            if len(value) > 2000 or any(c in value for c in "\r\n\0"):
                raise ValueError(
                    f"header {name} must be one line of at most 2000 characters"
                )
        return v

    @field_validator("wait_message")
    @classmethod
    def _wait_message(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return v.strip() or None


class HttpToolOut(BaseModel):
    name: str = Field(description="Function name the model calls.")
    description: str = Field(description="What the tool does. Read by the model.")
    url: str = Field(description="HTTPS endpoint Hail POSTs to.")
    parameters: dict[str, Any] = Field(description="JSON Schema of the arguments.")
    header_names: list[str] = Field(
        description="Names of the saved static headers. Values are never returned."
    )
    timeout_seconds: int = Field(description="Seconds Hail waits for the answer.")
    wait_message: str | None = Field(
        description="Sentence said on a call while waiting, or null."
    )


def _unique_tool_names(tools: list[HttpToolIn] | None) -> None:
    if not tools:
        return
    seen: set[str] = set()
    for tool in tools:
        if tool.name in seen:
            raise ValueError(f"http_tools has two tools named {tool.name}")
        seen.add(tool.name)


class AgentCreate(BaseModel):
    """A saved agent: what a number answers with, and what POST /calls can
    place calls with via ``agent_id``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=80,
        description="Display name, unique per organization.",
    )
    system_prompt: str = Field(
        min_length=1,
        description="Task instructions, sent as the agent's leading system message.",
    )
    first_message: str | None = Field(
        default=None,
        max_length=1000,
        description="Opening line spoken after the AI line. Omitted: the agent waits for the other side to speak first.",
    )
    recording_enabled: bool | None = Field(
        default=None,
        description="Null inherits the workspace recording choice; true or false sets recording for this agent's calls, whatever the workspace setting.",
    )
    ai_disclosure: bool = Field(
        default=True,
        description=(
            "Speak the AI line first on every call. Enabled by default. "
            "Disable only if you have verified the disclosure is not required "
            "(47 CFR 64.1200(b)(1) and several AI bot-disclosure laws). Hail "
            "does not verify this for you."
        ),
    )
    ai_disclosure_line: str | None = Field(
        default=None,
        max_length=_AGENT_LINE_MAX,
        description=(
            "The AI line this agent speaks. '{org}' is replaced by the "
            "organization's name. Omitted: the workspace line, else Hail's "
            "built-in line for the call's direction."
        ),
    )
    voice_config: VoiceConfig = Field(
        default_factory=VoiceConfig,
        description="TTS voice and spoken language, same shape as on POST /calls.",
    )
    tools: list[str] | None = Field(
        default=None,
        description=(
            "Agent tools to allow. Omitted: every tool the organization's "
            "channels support. Empty list: only the tools every call keeps "
            "(end_call, send_dtmf and Hail's settings guide). HTTP tools stay "
            "on."
        ),
    )
    max_duration_seconds: int | None = Field(
        default=None,
        ge=60,
        le=3600,
        description="Soft cap per call in seconds (60..3600). Omitted: the workspace limit.",
    )
    handover_max_duration_seconds: int | None = Field(
        default=None,
        ge=60,
        le=3600,
        description=(
            "Call limit in seconds (60..3600) once a handover contact answers, "
            "counted from the answer. Omitted: 1800 (30 minutes)."
        ),
    )
    voice_enabled: bool = Field(
        default=True,
        description="Answer calls on numbers that route calls to this agent.",
    )
    sms_enabled: bool = Field(
        default=True,
        description="Answer texts on numbers that route texts to this agent.",
    )
    email_enabled: bool = Field(
        default=True,
        description="Answer email on addresses that route email to this agent.",
    )
    status: AgentStatus = Field(
        default="live",
        description="'paused' agents do not answer; calls to their numbers fail with end_reason 'no_agent'.",
    )
    handover_contacts: list[HandoverContactIn] = Field(
        default_factory=list,
        max_length=10,
        description="People the agent may hand a live call to, in order. The agent never sees their numbers.",
    )
    http_tools: list[HttpToolIn] = Field(
        default_factory=list,
        max_length=HTTP_TOOLS_MAX,
        description=(
            "Your APIs the agent may call during calls, texts and email (up "
            "to 10). Each request is signed with the agent's "
            "http_tool_secret; see docs/public/agents.md."
        ),
    )

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @model_validator(mode="after")
    def _http_tool_names(self):
        _unique_tool_names(self.http_tools)
        return self


class AgentUpdate(BaseModel):
    """PATCH /agents/{id}. Fields left out keep their value; ``null`` clears
    the nullable ones (first_message, ai_disclosure_line, tools,
    max_duration_seconds, handover_max_duration_seconds)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None, min_length=1, max_length=80, description="New display name."
    )
    system_prompt: str | None = Field(
        default=None, min_length=1, description="New task instructions."
    )
    first_message: str | None = Field(
        default=None,
        max_length=1000,
        description="New opening line; null makes the agent wait for the other side.",
    )
    recording_enabled: bool | None = Field(
        default=None,
        description="Null inherits workspace recording; true or false sets it for this agent's calls.",
    )
    ai_disclosure: bool | None = Field(
        default=None, description="Whether the AI line is spoken first."
    )
    ai_disclosure_line: str | None = Field(
        default=None,
        max_length=_AGENT_LINE_MAX,
        description="New AI line template ('{org}' = organization name); null returns to the workspace line.",
    )
    voice_config: VoiceConfig | None = Field(
        default=None, description="New TTS voice and language settings."
    )
    tools: list[str] | None = Field(
        default=None,
        description=(
            "New allowed tools; null allows every tool. Calls always keep "
            "end_call, send_dtmf and Hail's settings guide."
        ),
    )
    max_duration_seconds: int | None = Field(
        default=None,
        ge=60,
        le=3600,
        description="New soft cap per call; null returns to the workspace limit.",
    )
    handover_max_duration_seconds: int | None = Field(
        default=None,
        ge=60,
        le=3600,
        description="New call limit after a handover contact answers; null returns to 1800 (30 minutes).",
    )
    voice_enabled: bool | None = Field(
        default=None, description="Whether the agent answers calls."
    )
    sms_enabled: bool | None = Field(
        default=None, description="Whether the agent answers texts."
    )
    email_enabled: bool | None = Field(
        default=None, description="Whether the agent answers email."
    )
    status: AgentStatus | None = Field(default=None, description="'live' or 'paused'.")
    handover_contacts: list[HandoverContactIn] | None = Field(
        default=None,
        max_length=10,
        description="New handover list; replaces the old one. [] removes all; null leaves it.",
    )
    http_tools: list[HttpToolIn] | None = Field(
        default=None,
        max_length=HTTP_TOOLS_MAX,
        description=(
            "New HTTP tool list; replaces the old one. [] removes all; null "
            "leaves it. A tool sent without headers keeps the saved headers "
            "of the tool with the same name."
        ),
    )

    @model_validator(mode="after")
    def _http_tool_names(self):
        _unique_tool_names(self.http_tools)
        return self

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Unique identifier for this agent.")
    organization_id: UUID = Field(description="Organization that owns this agent.")
    name: str = Field(description="Display name, unique per organization.")
    system_prompt: str = Field(description="Task instructions.")
    first_message: str | None = Field(description="Opening line, or null to wait.")
    recording_enabled: bool | None = Field(
        default=None,
        description="Null inherits the workspace recording choice; true or false sets recording for this agent's calls, whatever the workspace setting.",
    )
    ai_disclosure: bool = Field(description="Whether the AI line is spoken first.")
    ai_disclosure_line: str | None = Field(
        description="This agent's AI line template, or null for the workspace line."
    )
    voice_config: dict = Field(description="TTS voice and language settings.")
    tools: list[str] | None = Field(description="Allowed tools; null means all.")
    max_duration_seconds: int | None = Field(
        description="Soft cap per call; null means the workspace limit."
    )
    handover_max_duration_seconds: int | None = Field(
        description="Call limit after a handover contact answers; null means 1800 (30 minutes)."
    )
    voice_enabled: bool = Field(description="Whether the agent answers calls.")
    sms_enabled: bool = Field(description="Whether the agent answers texts.")
    email_enabled: bool = Field(
        default=True, description="Whether the agent answers email."
    )
    status: AgentStatus = Field(description="'live' or 'paused'.")
    handover_contacts: list[HandoverContactOut] = Field(
        default_factory=list, description="People the agent may hand a live call to."
    )
    http_tools: list[HttpToolOut] = Field(
        default_factory=list, description="Your APIs the agent may call."
    )
    http_tool_secret: str | None = Field(
        default=None,
        description=(
            "Secret that signs this agent's HTTP tool requests "
            "(X-Hail-Signature). Returned only when Hail creates it (the "
            "first save with HTTP tools) or rotates it "
            "(POST /agents/{id}/rotate-tool-secret). Store it then."
        ),
    )
    created_at: datetime = Field(description="ISO 8601 timestamp.")
    updated_at: datetime = Field(description="ISO 8601 timestamp.")


class AgentListResponse(BaseModel):
    items: list[AgentResponse] = Field(description="Agents, newest first.")


class PhoneNumberListResponse(BaseModel):
    items: list[PhoneNumberResponse] = Field(description="Numbers in this page.")
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Null when there are no more results.",
    )


class SuppressionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Unique identifier for this suppression entry.")
    recipient: str = Field(
        description="The suppressed recipient — E.164 phone number for voice/sms, lowercased email address for email."
    )
    channel: str = Field(
        description="Channel this entry blocks sends on: 'voice', 'email', 'sms', or 'all' (every channel)."
    )
    reason: str = Field(
        description="Why the recipient was suppressed (e.g. an unsubscribe or a bounce)."
    )
    source: str = Field(
        description="How this entry was created: 'unsubscribe_link' (email unsubscribe link), 'stop_keyword' (recipient replied STOP by SMS) or 'trial_stop_keyword' (your trial phone replied STOP to Hail's trial number). 'manual' (operator action) and 'bounce' (bounce handler) are reserved; nothing writes them yet."
    )
    created_at: datetime = Field(
        description="When this entry was created, ISO 8601 timestamp."
    )


class SuppressionListResponse(BaseModel):
    items: list[SuppressionResponse] = Field(
        description="Suppressed recipients in this page."
    )
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Null when there are no more results.",
    )


class EventResponse(BaseModel):
    """One event on the unified GET /events stream (call, email, or SMS)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Unique identifier for this event.")
    source: Literal["call", "email", "sms"] = Field(
        description="Which channel this event belongs to: 'call', 'email', or 'sms'."
    )
    call_id: UUID | None = Field(
        default=None,
        description="The call this event belongs to. Set only when source='call'.",
    )
    email_id: UUID | None = Field(
        default=None,
        description="The email this event belongs to. Set only when source='email'.",
    )
    sms_id: UUID | None = Field(
        default=None,
        description="The message this event belongs to. Set only when source='sms'.",
    )
    kind: str = Field(
        description="Event kind within the source (e.g. 'queued', 'delivered', 'bounced'). Vocabulary differs per source."
    )
    payload: dict[str, Any] = Field(
        description="Event-kind-specific detail, as a free-form JSON object."
    )
    occurred_at: datetime = Field(
        description="When this event occurred, ISO 8601 timestamp."
    )


class EventStreamResponse(BaseModel):
    items: list[EventResponse] = Field(description="Events in this page, oldest first.")
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Null when there are no more results.",
    )
    # Only populated when the ``id`` query filter resolves to a call (e.g.
    # ``id=call:<uuid>``). Org-wide tails and non-call resource types leave
    # this null — there's no single "the" status to report.
    call_status: CallStatus | None = Field(
        default=None,
        description=(
            "Current status of the call named by the id filter (e.g. "
            "id=call:<uuid>). Null for org-wide tails and non-call filters, "
            "since there is no single call to report a status for."
        ),
    )


# --------------------------------------------------------------------------- #
# Email schemas.
# --------------------------------------------------------------------------- #

# A liberal regex — RFC 5322 in full is a tarpit. This catches the obvious
# mistakes (no @, whitespace, missing TLD) without rejecting things SES
# would accept. SES does its own validation at send time.
EMAIL_ADDR = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DOMAIN_NAME = re.compile(
    r"^([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
)
# Local-part prefix for hail-mail. 1-20 chars of lowercase alphanumeric and
# hyphens, no leading/trailing hyphen. A single character matches because the
# parenthesized suffix is optional, so we don't need an explicit alternation.
LOCAL_PREFIX = re.compile(r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$")

# Workspace prefixes no tenant may mint. ``noreply+<name>@<base>`` is the
# sender of every forward and system mail; ``forwarder`` was its old name.
# Name prefixes are free text, so without this another org could own the
# address a tenant's forwards come from and receive its bounces. Role names
# belong to the operator of the base domain.
RESERVED_WORKSPACE_PREFIXES = frozenset({"noreply", "forwarder"}) | ROLE_MAILBOXES

# Old request and response names of the two hail-mail prefix fields. They had
# the meanings swapped in the name: local_prefix_user held the workspace part
# (before the plus), local_prefix_org the name part (after it). Requests still
# accept them and responses still return them, so released SDK, CLI and MCP
# versions keep working.
DEPRECATED_PREFIX_FIELDS = {
    "local_prefix_user": "local_prefix_workspace",
    "local_prefix_org": "local_prefix_name",
}

_PREFIX_RULE = (
    "must be 1–20 chars of lowercase a–z, 0–9, or '-', "
    "with no leading or trailing '-'"
)


def _merge_deprecated_prefix_fields(model: BaseModel) -> dict[str, str]:
    """Read an old prefix field as its new name.

    The old fields are validated on their own first, so an error names the
    field the caller sent. Both names may be sent with the same value.
    Different values are refused: no guess about which one the caller meant.

    Returns, for each new field name, the name the caller used, so later
    errors can name that field.
    """
    sent: dict[str, str] = {}
    for old, new in DEPRECATED_PREFIX_FIELDS.items():
        value = getattr(model, old)
        sent[new] = new if value is None or getattr(model, new) is not None else old
        if value is None:
            continue
        given = getattr(model, new)
        if given is None:
            setattr(model, new, value)
        elif given != value:
            raise ValueError(f"{old} is the deprecated name of {new}; send only {new}")
    return sent


class _PrefixNames(BaseModel):
    """Remembers which prefix field names the caller sent."""

    _sent_prefix_names: dict[str, str] = PrivateAttr(default_factory=dict)

    def prefix_field(self, new: str) -> str:
        """The name the caller used for ``new``: itself or its deprecated name."""
        return self._sent_prefix_names.get(new, new)


def _normalize_local_prefix(v: str | None) -> str | None:
    if v is None:
        return v
    v = v.strip().lower()
    if not LOCAL_PREFIX.match(v):
        raise ValueError(_PREFIX_RULE)
    return v


def _workspace_prefix_not_reserved(v: str | None) -> str | None:
    if v is not None and v in RESERVED_WORKSPACE_PREFIXES:
        raise ValueError(f"workspace prefix {v!r} is reserved")
    return v


_WORKSPACE_PREFIX_DOC = (
    "the part before the '+'. It names the workspace, and one workspace only "
    "may hold it."
)
_NAME_PREFIX_DOC = (
    "the part after the '+'. A free name inside the workspace, such as a "
    "person or team."
)


def _normalize_domain(addr: str) -> str:
    """Lowercase the domain portion of an email address.

    RFC 5321 treats the domain as case-insensitive (and most senders treat
    the local part the same way in practice), but we keep the local part
    verbatim — some legacy mailservers still respect it. Lowercasing the
    domain lets ``alerts@ACME.com`` match a stored ``acme.com`` row, which
    is what callers expect when they type a domain in mixed case.
    """
    local, _, domain = addr.partition("@")
    if not domain:
        return addr
    return f"{local}@{domain.lower()}"


EmailDomainKind = Literal["hail_mail", "custom"]
EmailAgentScope = Literal["members", "anyone"]
EmailDomainVerificationStatus = Literal["pending", "verified", "failed"]


class DnsRecordSchema(BaseModel):
    """One DNS record the tenant must publish for a sending domain.

    Covers DKIM CNAMEs, MAIL FROM MX, and SPF TXT records.
    """

    model_config = ConfigDict(from_attributes=True, extra="allow")

    name: str = Field(
        description="DNS record name/host to publish (e.g. a CNAME's subdomain)."
    )
    value: str = Field(
        description="DNS record value to publish (e.g. a CNAME target or TXT content)."
    )
    type: Literal["CNAME", "MX", "TXT"] = Field(
        default="CNAME",
        description="DNS record type: 'CNAME' (DKIM), 'MX' (MAIL FROM), or 'TXT' (SPF).",
    )
    priority: int | None = Field(
        default=None,
        description="MX priority. Only present for type='MX'; null otherwise.",
    )


class DomainCheckResponse(BaseModel):
    domain: str = Field(description="The apex domain that was checked, lowercased.")
    in_use: bool = Field(
        description="True if the domain already has MX records — it receives mail elsewhere."
    )
    existing_mx: list[str] = Field(
        description="MX hostnames currently published for the domain. Empty when in_use is false."
    )
    suggested_domain: str = Field(
        description=(
            "Domain to use for a custom sending identity: the apex domain "
            "if it is not already receiving mail, or an 'inbox.' subdomain "
            "if it is (so setup doesn't collide with existing mail)."
        )
    )


class EmailDomainCreate(_PrefixNames):
    """Request body for POST /email-domains.

    For ``kind='hail_mail'`` ``domain`` is omitted; the server composes
    the full address as ``<local_prefix_workspace>+<local_prefix_name>@<base>``.
    Both prefixes are optional in the body. The workspace prefix falls back
    to ``HAIL_MAIL_FROM`` or ``HAIL_MAIL_DEFAULT_USER_PREFIX``; the server
    returns 503 if none is set. For ``kind='custom'`` ``domain`` is required
    and the prefix fields must be omitted. ``local_prefix_user`` and
    ``local_prefix_org`` are the deprecated names of the two prefix fields.
    """

    model_config = ConfigDict(extra="forbid")

    kind: EmailDomainKind = Field(
        description=(
            "'hail_mail' for a Hail-hosted address (domain omitted, "
            "composed from the prefix fields), or 'custom' to send from "
            "your own domain (domain required, prefix fields omitted)."
        )
    )
    domain: str | None = Field(
        default=None,
        description="DNS domain to send from (e.g. 'acme.com'). Required for kind='custom'; must be omitted for kind='hail_mail'.",
    )
    local_prefix_workspace: str | None = Field(
        default=None,
        description=(
            f"Workspace prefix of a hail_mail address: {_WORKSPACE_PREFIX_DOC} "
            "A taken prefix returns 409. Only valid for kind='hail_mail'. Falls back to "
            "HAIL_MAIL_DEFAULT_USER_PREFIX plus a per-organization token if "
            "omitted."
        ),
    )
    local_prefix_name: str | None = Field(
        default=None,
        description=(
            f"Name prefix of a hail_mail address: {_NAME_PREFIX_DOC} Only "
            "valid for kind='hail_mail'. Falls back to a prefix derived from "
            "the organization id if omitted."
        ),
    )
    local_prefix_user: str | None = Field(
        default=None,
        json_schema_extra={"deprecated": True},
        description="Deprecated: use local_prefix_workspace. Same meaning.",
    )
    local_prefix_org: str | None = Field(
        default=None,
        json_schema_extra={"deprecated": True},
        description="Deprecated: use local_prefix_name. Same meaning.",
    )

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if not DOMAIN_NAME.match(v):
            raise ValueError(
                "must be a valid DNS domain (e.g. 'acme.com'); no schemes or paths"
            )
        return v

    @field_validator(
        "local_prefix_workspace",
        "local_prefix_name",
        "local_prefix_user",
        "local_prefix_org",
    )
    @classmethod
    def _validate_local_prefix(cls, v: str | None) -> str | None:
        return _normalize_local_prefix(v)

    @field_validator("local_prefix_workspace", "local_prefix_user")
    @classmethod
    def _not_reserved(cls, v: str | None) -> str | None:
        return _workspace_prefix_not_reserved(v)

    @model_validator(mode="after")
    def _kind_field_consistency(self):
        self._sent_prefix_names = _merge_deprecated_prefix_fields(self)
        if self.kind == "custom":
            if not self.domain:
                raise ValueError("domain is required when kind='custom'")
            if (
                self.local_prefix_workspace is not None
                or self.local_prefix_name is not None
            ):
                sent = [
                    self.prefix_field(field)
                    for field in ("local_prefix_workspace", "local_prefix_name")
                    if getattr(self, field) is not None
                ]
                verb = "is" if len(sent) == 1 else "are"
                raise ValueError(
                    f"{'/'.join(sent)} {verb} only valid when kind='hail_mail'"
                )
        if self.kind == "hail_mail" and self.domain is not None:
            raise ValueError(
                "domain must be omitted when kind='hail_mail'; the server "
                "composes it from local_prefix_workspace + local_prefix_name"
            )
        return self


# Cap on ``forward_to`` per domain row. Every new non-member address costs
# one confirm mail to a possibly unwilling stranger, so the list stays short.
MAX_FORWARD_TARGETS = 10

# Email.metadata keys the queue uses internally; POST /emails rejects them.
RESERVED_EMAIL_METADATA_KEYS = frozenset(
    {
        "forwarded_from",
        "forward_headers",
        "system_kind",
        # An agent's reply bookkeeping: a forged key could pose as a prior reply.
        "reply_to_email_id",
        "inbound_email_id",
        "thread_sender",
    }
)


class EmailDomainPatch(_PrefixNames):
    """Body for PATCH /email-domains/{id}.

    Two clusters of mutable fields:

    * Hail-mail addressing — the workspace/name prefix pair (only valid on
      ``kind='hail_mail'`` rows; the handler returns 422 if a tenant tries
      to PATCH these on a custom row).
    * Send switch — ``sends_enabled`` (only valid on ``kind='custom'``
      rows; the handler returns 422 otherwise). ``false`` stops the
      domain from sending at all: skipped by sender selection, explicit
      ``from`` refused.
    * Inbound action — ``inbound_enabled`` + ``forward_to`` + the optional
      ``forward_rate_per_hour`` cap. These apply to either kind, but this
      milestone routes inbound only to ``hail_mail`` rows; custom-domain
      inbound (MX delegation) is the next milestone.

    Every field is independently optional so ``PATCH`` semantics work the
    way callers expect: send only what you want to change. The route
    enforces the cross-field rules (the CHECK constraint on the table
    requires an action when ``inbound_enabled`` is true) — we don't
    re-implement it here because the patch may merge with existing row
    state to satisfy the invariant.
    """

    model_config = ConfigDict(extra="forbid")

    local_prefix_workspace: str | None = Field(
        default=None,
        description=(
            f"New workspace prefix: {_WORKSPACE_PREFIX_DOC} A taken prefix "
            "returns 409. Only valid on kind='hail_mail' rows. Omit to leave unchanged."
        ),
    )
    local_prefix_name: str | None = Field(
        default=None,
        description=(
            f"New name prefix: {_NAME_PREFIX_DOC} Only valid on "
            "kind='hail_mail' rows. Omit to leave unchanged."
        ),
    )
    local_prefix_user: str | None = Field(
        default=None,
        json_schema_extra={"deprecated": True},
        description="Deprecated: use local_prefix_workspace. Same meaning.",
    )
    local_prefix_org: str | None = Field(
        default=None,
        json_schema_extra={"deprecated": True},
        description="Deprecated: use local_prefix_name. Same meaning.",
    )
    inbound_enabled: bool | None = Field(
        default=None,
        description="Whether to accept inbound mail on this domain. Requires forward_to (or an existing one) when true. Omit to leave unchanged.",
    )
    forward_to: list[str] | None = Field(
        default=None,
        max_length=MAX_FORWARD_TARGETS,
        description=(
            "Email addresses to forward inbound mail to (at most "
            f"{MAX_FORWARD_TARGETS}). A new address that is not a verified "
            "member's login email gets a confirm link and receives nothing "
            "until it is clicked — see GET /forward-targets. Omit to leave "
            "unchanged."
        ),
    )
    forward_rate_per_hour: int | None = Field(
        default=None,
        description="Cap on forwarded messages per hour. Omit to leave unchanged.",
    )
    sends_enabled: bool | None = Field(
        default=None,
        description=(
            "Send switch for kind='custom' rows. False stops the domain "
            "from sending at all: skipped by default sender selection and a "
            "send naming it explicitly returns 422. Only allowed on custom "
            "rows; Omit to leave unchanged."
        ),
    )
    email_agent_id: UUID | None = Field(
        default=None,
        description=(
            "The agent that answers mail to this address. The agent must be "
            "live and answer email. A custom domain must be verified with "
            "inbound and sending on. Null stops agent replies. Omit to leave unchanged."
        ),
    )
    email_agent_scope: EmailAgentScope | None = Field(
        default=None,
        description=(
            "Who the agent answers. 'members': verified members of the "
            "organization whose mail passes DMARC. 'anyone': every sender whose "
            "mail passes DMARC and the spam and header rules; such a sender can "
            "make the agent use its tools. Mail without a DMARC pass gets no "
            "reply. Omit to leave unchanged, except that a new agent starts at "
            "'members'."
        ),
    )

    @field_validator(
        "local_prefix_workspace",
        "local_prefix_name",
        "local_prefix_user",
        "local_prefix_org",
    )
    @classmethod
    def _validate_local_prefix(cls, v: str | None) -> str | None:
        return _normalize_local_prefix(v)

    @field_validator("local_prefix_workspace", "local_prefix_user")
    @classmethod
    def _not_reserved(cls, v: str | None) -> str | None:
        return _workspace_prefix_not_reserved(v)

    @field_validator("forward_to")
    @classmethod
    def _forward_to_look_like_addresses(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for addr in v:
            local, sep, domain = addr.rpartition("@")
            if not sep or not local or "." not in domain:
                raise ValueError(f"forward_to entry {addr!r} is not an email address")
        return v

    @model_validator(mode="after")
    def _at_least_one_field(self):
        self._sent_prefix_names = _merge_deprecated_prefix_fields(self)
        if (
            self.local_prefix_workspace is None
            and self.local_prefix_name is None
            and self.inbound_enabled is None
            and self.forward_to is None
            and self.forward_rate_per_hour is None
            and self.sends_enabled is None
            and "email_agent_id" not in self.model_fields_set
            and self.email_agent_scope is None
        ):
            raise ValueError(
                "at least one of local_prefix_workspace, local_prefix_name, "
                "inbound_enabled, forward_to, forward_rate_per_hour, "
                "sends_enabled, email_agent_id, or email_agent_scope must be set"
            )
        return self


class EmailDomainResponse(BaseModel):
    """Read view for an email domain.

    The inbound-action fields (``inbound_enabled``, ``forward_to``,
    ``forward_rate_per_hour``) surface what the row has configured for
    incoming mail.
    """

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="before")
    @classmethod
    def _read_deprecated_prefix_keys(cls, data: Any) -> Any:
        # A reply from a server before the rename (or a stored idempotent
        # reply) has only the old keys.
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for old, new in DEPRECATED_PREFIX_FIELDS.items():
            if data.get(new) is None and data.get(old) is not None:
                data[new] = data[old]
        return data

    id: UUID = Field(description="Unique identifier for this email domain.")
    organization_id: UUID = Field(description="Organization that owns this domain.")
    kind: EmailDomainKind = Field(
        description="'hail_mail' (Hail-hosted address) or 'custom' (your own domain)."
    )
    domain: str = Field(description="The DNS domain mail is sent from.")
    # Required (nullable). The before-validator fills them from the old keys
    # when a reply has only those.
    local_prefix_workspace: str | None = Field(
        description=(
            f"Workspace prefix of a hail_mail address: {_WORKSPACE_PREFIX_DOC} "
            "Null for kind='custom'."
        ),
    )
    local_prefix_name: str | None = Field(
        description=(
            f"Name prefix of a hail_mail address: {_NAME_PREFIX_DOC} Null for "
            "kind='custom'."
        ),
    )
    verification_status: EmailDomainVerificationStatus = Field(
        description="'pending' (not yet verified), 'verified' (ready to send), or 'failed'."
    )
    dns_records: list[DnsRecordSchema] = Field(
        description="DNS records (DKIM, MAIL FROM MX, SPF) the tenant must publish to verify this domain."
    )
    mail_from_domain: str | None = Field(
        description="Custom MAIL FROM domain, if configured. Null when using the provider default."
    )
    mail_from_status: str | None = Field(
        default=None,
        description="Verification status of the custom MAIL FROM domain, if one is configured. Secondary to verification_status.",
    )
    provider: str = Field(
        description="Email sending provider for this domain (currently always 'ses')."
    )
    verified_at: datetime | None = Field(
        description="When the domain became verified, ISO 8601 timestamp. Null until it is."
    )
    inbound_enabled: bool = Field(
        default=False,
        description="Whether this domain accepts and forwards inbound mail.",
    )
    sends_enabled: bool = Field(
        default=True,
        description=(
            "Send switch for kind='custom' rows. False stops the domain "
            "from sending at all: it is skipped by default sender selection "
            "and a send that names it explicitly returns 422. Always true "
            "for hail_mail rows."
        ),
    )
    forward_to: list[str] | None = Field(
        default=None,
        description="Email addresses inbound mail is forwarded to, if inbound is enabled.",
    )
    forward_rate_per_hour: int | None = Field(
        default=None,
        description="Configured cap on forwarded messages per hour, if set.",
    )
    email_agent_id: UUID | None = Field(
        default=None,
        description="The agent that answers mail to this address. Null when none does.",
    )
    email_agent_scope: EmailAgentScope = Field(
        default="members",
        description="Who the agent answers: 'members' or 'anyone'.",
    )
    created_at: datetime = Field(
        description="When this domain was added, ISO 8601 timestamp."
    )
    updated_at: datetime = Field(
        description="When this domain was last modified, ISO 8601 timestamp."
    )
    # Populated by POST /{id}/verify on custom domains only; None everywhere else.
    # True when the domain's published MX points at the SES inbound host.
    receive_ready: bool | None = Field(
        default=None,
        description=(
            "True when the domain's published MX points at Hail's inbound "
            "host. Only populated by POST /{id}/verify on custom domains; "
            "null everywhere else."
        ),
    )

    # Old names, kept so released SDK, CLI and MCP versions keep working.
    @computed_field(
        json_schema_extra={"deprecated": True},
        description="Deprecated: use local_prefix_workspace. Same value.",
    )
    @property
    def local_prefix_user(self) -> str | None:
        return self.local_prefix_workspace

    @computed_field(
        json_schema_extra={"deprecated": True},
        description="Deprecated: use local_prefix_name. Same value.",
    )
    @property
    def local_prefix_org(self) -> str | None:
        return self.local_prefix_name


class DnsProviderSchema(BaseModel):
    """A DNS host detected from a domain's nameservers, with its records UI."""

    id: str = Field(
        description="Short identifier for the detected DNS host (e.g. 'cloudflare')."
    )
    name: str = Field(description="Human-readable name of the detected DNS host.")
    dns_url: str = Field(description="URL to the DNS host's records-management UI.")
    note: str | None = Field(
        description="Host-specific tip for publishing records correctly. Null when there is none."
    )


class ObservedDnsRecord(DnsRecordSchema):
    """One record from EmailDomainResponse.dns_records, with a live DNS observation."""

    observed: bool | None = Field(
        description=(
            "True if Hail saw this record in public DNS, false if it looked "
            "and did not. Null when the lookup for this record failed or "
            "lookup_ok is false — null means 'could not check', never 'not "
            "published'. This does not change verification_status on the "
            "domain — SES stays the authority for that."
        )
    )


class DmarcCheck(BaseModel):
    """Whether a DMARC record covers the domain."""

    present: bool = Field(
        description=(
            "True if a DMARC TXT record exists at _dmarc.<domain> or at "
            "_dmarc.<organizational domain> — the two names a receiver "
            "checks (RFC 7489 section 6.6.3). The organizational domain is "
            "one label under the public suffix: acme.co.uk for "
            "mail.acme.co.uk."
        )
    )
    suggested: DnsRecordSchema | None = Field(
        description=(
            "A minimal DMARC record to publish, named _dmarc.<zone>. Null "
            "when present is true."
        )
    )


class EmailDomainDnsCheck(BaseModel):
    """Response for GET /email-domains/{id}/dns-check."""

    kind: EmailDomainKind = Field(
        description=(
            "The domain's kind. 'hail_mail' rows run no lookups: the domain "
            "is Hail's, so there is nothing for the customer to publish."
        )
    )
    dns_provider: DnsProviderSchema | None = Field(
        description=(
            "The DNS host detected from the domain's nameservers. Null when "
            "no known host matched, when the domain's zone could not be "
            "resolved, or for kind='hail_mail' rows."
        )
    )
    zone: str | None = Field(
        description=(
            "The DNS zone apex found for this domain: the nearest name at "
            "or above it with NS records, never above the organizational "
            "domain (a public suffix such as co.uk is never returned). Null "
            "when it could not be resolved, or for kind='hail_mail' rows."
        )
    )
    records: list[ObservedDnsRecord] = Field(
        description=(
            "The domain's dns_records, each annotated with whether Hail "
            "currently observes it in public DNS. Empty for kind='hail_mail' "
            "rows."
        )
    )
    dmarc: DmarcCheck = Field(
        description="Whether DMARC is published for this domain, and a suggested record if not."
    )
    lookup_ok: bool = Field(
        description=(
            "False when the zone or DMARC lookup failed or the check timed "
            "out — then dns_provider and zone are null, every record's "
            "observed is null, and dmarc is not reliable; none of it means "
            "the records are missing, so retry later instead of telling the "
            "customer to republish anything. True otherwise, whatever the "
            "lookups found. A single record whose own lookup failed has "
            "observed: null while lookup_ok stays true. Always true for "
            "kind='hail_mail' rows, which run no lookups."
        )
    )


ForwardTargetStatus = Literal["pending", "verified", "stopped"]


class ForwardTargetResponse(BaseModel):
    """One forward address of the organization and whether it may receive
    forwards. Rows come from ``email_forward_targets``; see
    ``hailhq.core.forward_targets``."""

    address: str = Field(description="Normalized (lower-cased) email address.")
    status: ForwardTargetStatus = Field(
        description=(
            "'verified' receives forwards. 'pending' was sent a confirm link "
            "that is not yet clicked. 'stopped' got a spam complaint and "
            "needs a new confirm link (POST /forward-targets/{address}/resend)."
        )
    )
    pending_reason: Literal["confirm_link", "account_unverified"] | None = Field(
        default=None,
        description=(
            "Why a 'pending' address is not live. 'confirm_link': a confirm mail "
            "was sent and not yet clicked. 'account_unverified': the address is a "
            "member's login email whose account email is not verified yet; it goes "
            "live on the account verify click, no confirm mail. Null otherwise."
        ),
    )
    verified_at: datetime | None = Field(
        default=None, description="When the address was verified, ISO 8601."
    )
    token_sent_at: datetime | None = Field(
        default=None,
        description="When the latest confirm link was sent, ISO 8601. Null once verified.",
    )
    stopped_at: datetime | None = Field(
        default=None, description="When forwarding was stopped, ISO 8601."
    )
    stopped_reason: str | None = Field(
        default=None, description="Why forwarding was stopped, e.g. 'complaint'."
    )


class ForwardTargetListResponse(BaseModel):
    items: list[ForwardTargetResponse] = Field(
        description="Every forward address ever configured for this organization."
    )


class EmailDomainListResponse(BaseModel):
    items: list[EmailDomainResponse] = Field(description="Email domains in this page.")
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Null when there are no more results.",
    )
    # The address a send with no ``from`` goes out as. ``None`` when such a
    # send would be rejected: several verified custom domains (name one), or
    # none that can send yet. Computed across the whole org, not the page.
    default_from: str | None = Field(
        default=None,
        description=(
            "The From address used when a send omits 'from'. A verified "
            "custom domain wins over the hail-mail address. Null when no "
            "such default can be resolved (e.g. multiple verified custom "
            "domains, or none that can send yet). Computed across the "
            "whole organization, not just this page."
        ),
    )


class EmailCreate(ConsentAttestationMixin):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ``from`` is reserved; ``from_`` mirrors how CallCreate handles it.
    from_: str | None = Field(
        default=None,
        alias="from",
        description=(
            "Sender email address. Must be a verified identity on the "
            "organization's email domains. Omitted: the org's resolved "
            "default sending address, if one exists."
        ),
    )
    # Friendly display name for the From: header ("Acme Billing
    # <billing@acme.com>"). Rendered by the email provider at send time
    # (see providers/email/ses.py); ``from_`` stays a bare address
    # everywhere else (sender resolution, compliance, events).
    from_name: str | None = Field(
        default=None,
        max_length=256,
        description="Display name for the From: header (e.g. 'Acme Billing'). Omitted: no display name.",
    )
    to: list[str] = Field(
        min_length=1, description="Recipient email addresses. At least one required."
    )
    cc: list[str] | None = Field(
        default=None, description="CC recipient email addresses."
    )
    bcc: list[str] | None = Field(
        default=None, description="BCC recipient email addresses."
    )
    reply_to: str | None = Field(
        default=None,
        description="Reply-To email address. Omitted: replies go to the From address.",
    )
    subject: str = Field(
        min_length=1, max_length=998, description="Email subject line."
    )
    body_text: str | None = Field(
        default=None,
        description=(
            "Plain-text body. Either body_text or body_html (or both) is "
            "required. A plain-text-only email cannot be tracked for opens "
            "or clicks; include body_html to get those events."
        ),
    )
    body_html: str | None = Field(
        default=None,
        description=(
            "HTML body. Either body_text or body_html (or both) is required. "
            "Prefer including body_html: open and click tracking only works "
            "for emails with an HTML body. Plain-text-only emails still get "
            "sent, delivered, and bounce events, but opens and clicks are "
            "never tracked."
        ),
    )
    metadata: dict = Field(
        default_factory=dict,
        description=(
            "Free-form JSON object attached to the email and echoed back on "
            "reads. Not interpreted by Hail. The keys 'forwarded_from', "
            "'forward_headers', 'system_kind', 'reply_to_email_id', "
            "'inbound_email_id' and 'thread_sender' are reserved for Hail's own "
            "bookkeeping and are rejected."
        ),
    )
    attachment_ids: list[UUID] | None = Field(
        default=None,
        description="Ids returned by POST /email-attachments to attach to this send. Omitted: no attachments.",
    )

    @field_validator("metadata")
    @classmethod
    def _metadata_has_no_reserved_keys(cls, v: dict) -> dict:
        # The outbound worker claims queued rows by these keys (forwards and
        # Hail's own system mail, which is not metered). A caller must not be
        # able to plant them on a direct send.
        bad = sorted(set(v) & RESERVED_EMAIL_METADATA_KEYS)
        if bad:
            raise ValueError(f"metadata keys are reserved: {', '.join(bad)}")
        return v

    @field_validator("from_name", mode="before")
    @classmethod
    def _strip_from_name(cls, v: object) -> object:
        # Strip BEFORE max_length runs so a padded-but-valid name
        # ("Acme" + spaces) isn't rejected as too long.
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
        return v

    @field_validator("from_name")
    @classmethod
    def _validate_from_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # The display name lands in the From: header. isprintable() rejects
        # every C0/C1 control (CR/LF header injection, NEL U+0085, U+2028)
        # plus zero-width/format characters — strictly wider than a bare
        # ord(ch) < 32 check.
        if not v.isprintable():
            raise ValueError("must contain only printable characters")
        return v

    @field_validator("from_", "reply_to")
    @classmethod
    def _validate_optional_email(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not EMAIL_ADDR.match(v):
            raise ValueError("must be a valid email address (local@domain.tld)")
        return _normalize_domain(v)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def _validate_email_list(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out: list[str] = []
        for addr in v:
            if not EMAIL_ADDR.match(addr):
                raise ValueError(
                    f"invalid email address {addr!r} (expected local@domain.tld)"
                )
            out.append(_normalize_domain(addr))
        return out

    @model_validator(mode="after")
    def _body_required(self):
        if not self.body_text and not self.body_html:
            raise ValueError("either body_text or body_html must be provided")
        return self


EmailStatus = Literal[
    "queued", "sent", "delivered", "failed", "bounced", "complained", "received"
]

TERMINAL_EMAIL_STATUSES: frozenset[str] = frozenset(
    {"sent", "delivered", "failed", "bounced", "complained"}
)


EmailEventKind = Literal[
    "sent",
    "delivered",
    "delivery_delayed",
    "bounced",
    "complained",
    "rejected",
    "opened",
    "clicked",
]


class EmailEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Unique identifier for this event.")
    email_id: UUID = Field(description="The email this event belongs to.")
    kind: EmailEventKind = Field(
        description=(
            "Event kind: 'sent', 'delivered', 'delivery_delayed', "
            "'bounced', 'complained', 'rejected', 'opened', or 'clicked'. "
            "'opened' and 'clicked' only occur for emails sent with an HTML "
            "body; plain-text-only emails never produce them."
        )
    )
    payload: dict[str, Any] = Field(
        description="Event-kind-specific detail, as a free-form JSON object."
    )
    occurred_at: datetime = Field(
        description="When this event occurred, ISO 8601 timestamp."
    )


class EmailEventListResponse(BaseModel):
    items: list[EmailEventResponse] = Field(
        description="Events for this email, oldest first."
    )
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Null when there are no more results.",
    )


class EmailStatsCounts(BaseModel):
    sent: int = Field(default=0, description="Emails sent in the window.")
    delivered: int = Field(
        default=0, description="Emails confirmed delivered in the window."
    )
    delivery_delayed: int = Field(
        default=0, description="Emails with a delivery-delayed event in the window."
    )
    bounced: int = Field(
        default=0, description="Emails bounced (soft or hard) in the window."
    )
    bounced_hard: int = Field(
        default=0, description="Emails hard-bounced in the window. Subset of bounced."
    )
    complained: int = Field(
        default=0, description="Emails that received a spam complaint in the window."
    )
    rejected: int = Field(
        default=0,
        description="Emails rejected by the provider before sending, in the window.",
    )
    opened: int = Field(
        default=0,
        description=(
            "Total open events in the window, including repeat opens by the "
            "same recipient. HTML emails only; plain-text-only emails are "
            "never tracked for opens."
        ),
    )
    clicked: int = Field(
        default=0,
        description=(
            "Total click events in the window, including repeat clicks by "
            "the same recipient. HTML emails only; plain-text-only emails "
            "are never tracked for clicks."
        ),
    )
    unique_opened: int = Field(
        default=0,
        description=(
            "Distinct emails opened at least once in the window (HTML emails only)."
        ),
    )
    unique_clicked: int = Field(
        default=0,
        description=(
            "Distinct emails clicked at least once in the window (HTML emails only)."
        ),
    )


class EmailStatsBucket(EmailStatsCounts):
    bucket_start: datetime = Field(
        description="Start of this bucket, ISO 8601 timestamp."
    )


class EmailStatsRates(BaseModel):
    """All None when sent == 0 in the window."""

    delivery: float | None = Field(
        default=None,
        description="delivered / sent for the window. Null when sent == 0.",
    )
    bounce: float | None = Field(
        default=None,
        description="bounced_hard / sent for the window. Null when sent == 0.",
    )  # hard bounces / sent
    complaint: float | None = Field(
        default=None,
        description="complained / sent for the window. Null when sent == 0.",
    )
    open: float | None = Field(
        default=None,
        description=(
            "unique_opened / sent for the window. Null when sent == 0. "
            "Only HTML emails can be opened-tracked, so plain-text sends "
            "lower this rate."
        ),
    )  # unique_opened / sent
    click: float | None = Field(
        default=None,
        description=(
            "unique_clicked / sent for the window. Null when sent == 0. "
            "Only HTML emails can be click-tracked, so plain-text sends "
            "lower this rate."
        ),
    )  # unique_clicked / sent


class EmailStatsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_ts: datetime = Field(
        serialization_alias="from",
        validation_alias=AliasChoices("from_ts", "from"),
        description="Start of the queried window, ISO 8601 timestamp (inclusive).",
    )
    to_ts: datetime = Field(
        serialization_alias="to",
        validation_alias=AliasChoices("to_ts", "to"),
        description="End of the queried window, ISO 8601 timestamp (exclusive).",
    )
    bucket: Literal["hour", "day"] = Field(
        description="Time-bucket size used for the series."
    )
    totals: EmailStatsCounts = Field(
        description="Event counts summed across the whole window."
    )
    rates: EmailStatsRates = Field(
        description=(
            "Derived rates (delivery, bounce, complaint, open, click) for "
            "the whole window. Each individual rate is null when sent == 0."
        )
    )
    series: list[EmailStatsBucket] = Field(
        description="Per-bucket event counts across the window, in chronological order."
    )


class EmailSummary(BaseModel):
    """Trimmed view for list endpoints — drops the message bodies.

    Bodies can be large and contain PII; paging through a year of mail
    shouldn't return every byte of every message just to render a list.
    Use ``EmailResponse`` (via ``GET /emails/{id}``) for the full row.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID = Field(description="Unique identifier for this email.")
    organization_id: UUID = Field(
        description="Organization that sent or received this email."
    )
    email_domain_id: UUID | None = Field(
        description="The sending domain used, if from_address belongs to one of the org's configured domains."
    )
    direction: Literal["outbound", "inbound"] = Field(
        default="outbound",
        description="'outbound' for emails Hail sent, 'inbound' for emails received.",
    )
    from_address: str = Field(description="Sender email address.")
    # Display name used on the From: header, when the sender supplied one.
    # Always None on inbound rows and pre-existing outbound rows.
    from_name: str | None = Field(
        default=None,
        description=(
            "Display name used on the From: header, when the sender "
            "supplied one. Always null on inbound rows."
        ),
    )
    to_addresses: list[str] = Field(description="Recipient email addresses.")
    cc_addresses: list[str] | None = Field(
        description="CC recipient email addresses, if any."
    )
    bcc_addresses: list[str] | None = Field(
        description="BCC recipient email addresses, if any."
    )
    reply_to: str | None = Field(description="Reply-To email address, if set.")
    subject: str = Field(description="Email subject line.")
    status: EmailStatus = Field(
        description=(
            "Delivery status: 'queued', 'sent', 'delivered', 'failed', "
            "'bounced', 'complained', or 'received' (inbound emails)."
        )
    )
    end_reason: str | None = Field(
        description="Reason delivery failed or bounced, if applicable. Null on success or while pending."
    )
    provider_message_id: str | None = Field(
        description="The email provider's identifier for this message, if assigned."
    )
    requested_at: datetime = Field(
        description="When the send was requested, ISO 8601 timestamp."
    )
    sent_at: datetime | None = Field(
        description="When the message was handed to the provider, ISO 8601 timestamp. Null until sent."
    )
    failed_at: datetime | None = Field(
        description="When the send failed, ISO 8601 timestamp. Null unless it failed."
    )
    # ``Email.metadata_`` is the SQLAlchemy attribute (``metadata`` is
    # reserved by Declarative). The validation_alias bridges that name so
    # ``from_attributes=True`` reads the right column; the field on the
    # response is still called ``metadata`` on the wire.
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="metadata_",
        description=(
            "Free-form JSON object attached to the email, as sent on "
            "create. Not interpreted by Hail."
        ),
    )


class EmailAttachmentResponse(BaseModel):
    """One inbound MIME attachment as exposed to API consumers.

    ``url`` is the stable Hail API endpoint that 302-redirects to a
    presigned S3 URL on access — see GET /emails/{id}/attachments/{aid}.
    """

    id: UUID = Field(description="Unique identifier for this attachment.")
    filename: str = Field(description="Original filename of the attachment.")
    content_type: str = Field(description="MIME type of the attachment.")
    size_bytes: int = Field(description="Size of the attachment in bytes.")
    content_id: str | None = Field(
        default=None,
        description="MIME Content-ID, present when this attachment is referenced inline (cid:) from the HTML body. Null otherwise.",
    )
    url: str = Field(
        description="API endpoint that 302-redirects to a presigned download URL for this attachment."
    )

    model_config = ConfigDict(from_attributes=True)


class EmailAttachmentUploadResponse(BaseModel):
    """Returned by POST /email-attachments.

    ``id`` is reusable across many ``POST /emails`` calls via
    ``EmailCreate.attachment_ids`` until Hail garbage-collects it (24h
    if never referenced by a send).
    """

    id: UUID = Field(
        description="Reusable attachment id — pass it in EmailCreate.attachment_ids to attach it to a send."
    )
    filename: str = Field(description="Original filename of the uploaded file.")
    content_type: str = Field(description="MIME type of the uploaded file.")
    size_bytes: int = Field(description="Size of the uploaded file in bytes.")

    model_config = ConfigDict(from_attributes=True)


class FileUploadCreate(BaseModel):
    """Body of ``POST /uploads``: what the file is for, and its name and type."""

    purpose: Purpose = Field(
        description=(
            "What the file is for. `email_attachment`: the id works in "
            "`POST /v1/emails` `attachment_ids`. `verification_document`: the "
            "id works as the `upload.<slot>` part of `POST /v1/verifications` "
            "and `POST /v1/verifications/check`. `sms_evidence`: a US texting "
            "consent screenshot; the PUT returns a public `url` for an "
            "`*_evidence_url` field, like `POST /v1/verifications/sms/evidence`."
        )
    )
    filename: str = Field(
        min_length=1,
        max_length=255,
        description="The file's name, 1 to 255 chars, no control characters.",
    )
    content_type: str = Field(
        description=(
            "MIME type, `type/subtype` with no parameters. A "
            "`verification_document` or `sms_evidence` takes `image/jpeg`, "
            "`image/png` or `application/pdf` only. This type is stored; the "
            "PUT request's own `Content-Type` header is not used."
        )
    )

    @field_validator("filename")
    @classmethod
    def _filename_printable(cls, v: str) -> str:
        # The name goes into a MIME header of the email; CR or LF there
        # would start a new header.
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            raise ValueError("filename must not contain control characters")
        return v

    @field_validator("content_type")
    @classmethod
    def _content_type_shape(cls, v: str) -> str:
        if not valid_content_type(v):
            raise ValueError(
                "content_type must be a MIME type like application/pdf, "
                "with no parameters"
            )
        return v

    @model_validator(mode="after")
    def _verification_types(self) -> "FileUploadCreate":
        if (
            self.purpose in ("verification_document", "sms_evidence")
            and self.content_type not in VERIFICATION_FILE_TYPES
        ):
            raise ValueError(
                f"a {self.purpose} must be image/jpeg, image/png or application/pdf"
            )
        return self


class FileUploadLinkResponse(BaseModel):
    """Returned by ``POST /uploads``: a one-time link to send the bytes to."""

    id: UUID = Field(description="The upload's id.")
    purpose: Purpose = Field(description="What the file is for.")
    upload_url: str = Field(
        description=(
            "Send the raw file bytes here with `PUT`. No Authorization "
            "header: the `token` in the URL is the only credential. Works "
            "once, until `expires_at`. Treat it like a password."
        )
    )
    method: Literal["PUT"] = Field(description="The HTTP method for `upload_url`.")
    content_type: str = Field(description="The type the file is stored with.")
    max_bytes: int = Field(description="The largest file the link takes, in bytes.")
    expires_at: datetime = Field(description="When the link stops working.")


class FileUploadResponse(BaseModel):
    """Returned by ``PUT /uploads/{id}``: what Hail stored."""

    id: UUID = Field(description="The upload's id.")
    purpose: Purpose = Field(description="What the file is for.")
    filename: str = Field(description="The name given when the link was made.")
    content_type: str = Field(description="The type given when the link was made.")
    size_bytes: int = Field(description="Bytes stored. Compare with the local file.")
    sha256: str = Field(
        description="sha256 of the bytes stored, hex. Compare with the local file."
    )
    url: str | None = Field(
        default=None,
        description=(
            "For `sms_evidence`: the public link to the file. Put it in an "
            "`*_evidence_url` field. Carrier reviewers open it without signing "
            "in. Null for other purposes."
        ),
    )

    model_config = ConfigDict(from_attributes=True)


class VerificationCheckFile(BaseModel):
    """One file a verification check read."""

    slot: str = Field(description="The document slot.")
    upload_id: UUID | None = Field(
        description="The upload used for the slot, or null for a `file.<slot>` part."
    )
    size_bytes: int = Field(description="Size of the file in bytes.")
    sha256: str = Field(description="sha256 of the file, hex.")


class VerificationCheckResponse(BaseModel):
    """Returned by ``POST /verifications/check`` when every local check passes."""

    ok: Literal[True] = Field(description="Always true: a failed check is a 4xx.")
    files: list[VerificationCheckFile] = Field(
        description="The files the check read, one per slot."
    )
    note: str = Field(description="What the check does not cover.")


class VerificationDocumentWarning(BaseModel):
    """Something on an uploaded document the carrier may refuse. A warning
    only: you can still submit."""

    field: str = Field(
        description=(
            "Where to show it: a slot name for the document itself, or the "
            "path of a typed value (`first_name`, `<slot>.<field>`, "
            "`address.customer_name`)."
        )
    )
    kind: Literal["unreadable", "rotated", "expired", "document_type", "name"] = Field(
        description=(
            "'unreadable', 'rotated' (upside down or on its side), 'expired', "
            "'document_type' (the file does not match the chosen type), or "
            "'name' (the typed name differs from the printed one)."
        )
    )
    message: str = Field(description="What is wrong and how to fix it.")


class VerificationPrintedName(BaseModel):
    """The name printed on a document, as read. Empty strings when not printed."""

    given_names: str = Field(
        description=(
            "The given names of the person the document is about or "
            "addressed to, never a person who signs for the issuer."
        )
    )
    surnames: str = Field(description="That person's surnames.")
    latin_given_names: str = Field(
        default="",
        description=(
            "The given names in Latin letters as the document prints them "
            "(its machine-readable zone or a transliteration), or empty."
        ),
    )
    latin_surnames: str = Field(
        default="", description="The surnames in Latin letters, the same way."
    )
    business_name: str = Field(
        description=(
            "The name of the company the document is about or addressed "
            "to, never the company that issued it."
        )
    )


class VerificationDocumentRead(BaseModel):
    """What was read from one slot's file."""

    read: bool = Field(
        description=(
            "False when nothing was read from the file: the reader is off, "
            "the file type is not supported, the read failed or took too "
            "long (no warning), or the file is unreadable (an 'unreadable' "
            "warning). The other values are then empty."
        )
    )
    option: str | None = Field(
        description=(
            "The slot's document type the file looks like, as an option key, "
            "or null when none fits or it was not read."
        )
    )
    fields: dict[str, str] = Field(
        description=(
            "Values for the fields of the slot's document types, by field "
            "name, as printed on the file."
        )
    )
    expiry_date: str | None = Field(
        description="The expiry date printed on the file (YYYY-MM-DD), or null."
    )
    printed_name: VerificationPrintedName | None = Field(
        description="The name printed on the file, or null when not read."
    )
    skipped: Literal["daily_limit"] | None = Field(
        default=None,
        description=(
            "Why the file was not read, when Hail chose not to: "
            "'daily_limit' when the workspace has used today's document "
            "reads. Null otherwise."
        ),
    )
    none_of_types: bool = Field(
        default=False,
        description=(
            "True when the file was read and looks like none of the slot's "
            "document types (`option` is then null)."
        ),
    )
    file_slot: str | None = Field(
        default=None,
        description=(
            "The slot whose file this was read from: this slot, or the slot "
            "whose file covers it. Slots with the same `file_slot` share one "
            "file. Null when not read."
        ),
    )


class VerificationDocumentCheckResponse(BaseModel):
    """Returned by ``POST /verifications/document-check``."""

    fields: dict[str, str] = Field(
        description="Values for the form's own `fields`, by field name, read from the files."
    )
    address: dict[str, str] = Field(
        description=(
            "Address parts read from the files (`customer_name`, `street`, "
            "`city`, `region`, `postal_code`, `country_code`). Only the parts found."
        )
    )
    documents: dict[str, VerificationDocumentRead] = Field(
        description="What was read from each slot that had a file, by slot name."
    )
    warnings: list[VerificationDocumentWarning] = Field(
        description="Things the carrier may refuse. Empty when nothing was found."
    )
    note: str = Field(description="What the check does not cover.")


class VerificationDocumentCautionRead(BaseModel):
    """A slot's earlier read, as `POST /verifications/document-check`
    returned it in `documents.<slot>`. Send that whole object: `option`,
    `printed_name`, `expiry_date`, `none_of_types` and `file_slot` are used;
    the other keys may be sent and are ignored."""

    option: str | None = Field(
        default=None,
        description="The document type the file looked like, or null.",
    )
    printed_name: VerificationPrintedName | None = Field(
        default=None,
        description="The name printed on the file, or null (no name check).",
    )
    expiry_date: str | None = Field(
        default=None,
        description=(
            "The expiry date read from the file (YYYY-MM-DD), or null (no "
            "expiry check)."
        ),
    )
    none_of_types: bool = Field(
        default=False,
        description=(
            "True when the file looked like none of the slot's document types."
        ),
    )
    file_slot: str | None = Field(
        default=None,
        description=(
            "The slot whose file this was read from. Slots with the same "
            "`file_slot` get one name check per box, as in the document "
            "check. Null: slots with the same printed name count as one file."
        ),
    )


class VerificationDocumentCautionDocument(BaseModel):
    """One slot of the form: what is typed now, and its earlier read."""

    option: str = Field(
        default="",
        description="The chosen document type key, or empty when not chosen yet.",
    )
    fields: dict[str, str] = Field(
        default_factory=dict,
        description="The values typed for this slot's fields, by field name.",
    )
    read: VerificationDocumentCautionRead | None = Field(
        default=None,
        description=(
            "The slot's `documents.<slot>` from an earlier document check, or "
            "null when it has no successful read. Only slots with a read are "
            "checked."
        ),
    )


class VerificationDocumentCautionsRequest(BaseModel):
    """Body of ``POST /verifications/document-cautions``."""

    quote_id: UUID = Field(
        description="The `quote_id` of the number offer being verified."
    )
    country_code: str = Field(
        min_length=2,
        max_length=2,
        description="ISO 3166-1 alpha-2 country of the number, like GB.",
    )
    number_type: str = Field(
        description="The number type of the offer, like `local` or `mobile`."
    )
    subject_type: Literal["person", "business"] = Field(
        description="'person' (you) or 'business' (your company)."
    )
    fields: dict[str, str] = Field(
        default_factory=dict, description="The form's typed fields, by name."
    )
    address: dict[str, str] = Field(
        default_factory=dict,
        description="The typed address parts (`customer_name` is compared).",
    )
    documents: dict[str, VerificationDocumentCautionDocument] = Field(
        default_factory=dict, description="The form's document slots, by name."
    )


class VerificationDocumentCautionsResponse(BaseModel):
    """Returned by ``POST /verifications/document-cautions``."""

    warnings: list[VerificationDocumentWarning] = Field(
        description=(
            "`name`, `document_type` and `expired` warnings: the typed form "
            "and chosen types against the earlier reads. Empty when nothing "
            "differs."
        )
    )


class EmailResponse(EmailSummary):
    body_text: str | None = Field(description="Plain-text body, if any.")
    body_html: str | None = Field(description="HTML body, if any.")
    # Inbound-only metadata. Outbound rows leave these all null/empty —
    # we surface them on the full-row endpoint (GET /emails/{id}) rather
    # than the list summary because most inbound consumers will fetch the
    # row anyway to read the body. Defaults match the outbound shape so
    # existing serializations keep working.
    message_id: str | None = Field(
        default=None,
        description="RFC 5322 Message-ID header. Inbound emails only; null on outbound rows.",
    )
    in_reply_to: str | None = Field(
        default=None,
        description="RFC 5322 In-Reply-To header. Set on inbound emails and agent replies; null on other outbound rows.",
    )
    references_ids: list[str] | None = Field(
        default=None,
        description="RFC 5322 References header, split into ids. Set on inbound emails and agent replies; null on other outbound rows.",
    )
    agent_id: UUID | None = Field(
        default=None,
        description="The agent this email belongs to: the address's agent on inbound rows, the writer on outbound rows.",
    )
    agent_reply_state: str | None = Field(
        default=None,
        description=(
            "Inbound rows routed to an agent: 'pending', 'processing', 'done', "
            "'skipped', or 'failed'. A skipped row names why in "
            "metadata.agent_skip_reason."
        ),
    )
    spam_verdict: str | None = Field(
        default=None,
        description="Provider spam-scan verdict (e.g. 'PASS'/'FAIL'). Inbound emails only; null on outbound rows.",
    )
    virus_verdict: str | None = Field(
        default=None,
        description="Provider virus-scan verdict (e.g. 'PASS'/'FAIL'). Inbound emails only; null on outbound rows.",
    )
    dkim_verdict: str | None = Field(
        default=None,
        description="Provider DKIM-authentication verdict (e.g. 'PASS'/'FAIL'). Inbound emails only; null on outbound rows.",
    )
    spf_verdict: str | None = Field(
        default=None,
        description="Provider SPF-authentication verdict (e.g. 'PASS'/'FAIL'). Inbound emails only; null on outbound rows.",
    )
    dmarc_verdict: str | None = Field(
        default=None,
        description="Provider DMARC-authentication verdict (e.g. 'PASS'/'FAIL'). Inbound emails only; null on outbound rows.",
    )
    provider_received_at: datetime | None = Field(
        default=None,
        description="When the provider received this email, ISO 8601 timestamp. Inbound emails only.",
    )
    # ``raw_url`` is the API endpoint that 302-redirects to a presigned
    # S3 URL for the original MIME blob; ``raw_s3_key`` is the column on
    # the row but we don't expose internal storage paths on the wire.
    raw_url: str | None = Field(
        default=None,
        description="API endpoint that redirects to the original MIME blob. Inbound emails only; null on outbound rows.",
    )
    attachments: list[EmailAttachmentResponse] = Field(
        default=[],
        description="Inbound MIME attachments on this email. Empty on outbound rows.",
    )
    last_event_at: datetime | None = Field(
        default=None,
        description="When the most recent delivery event for this email occurred, ISO 8601 timestamp.",
    )


class EmailListResponse(BaseModel):
    items: list[EmailSummary] = Field(description="Emails in this page, newest first.")
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Null when there are no more results.",
    )


# --------------------------------------------------------------------------- #
# Webhook subscriptions + deliveries
# --------------------------------------------------------------------------- #

WebhookSubscriptionStatus = Literal["active", "disabled"]
WebhookDeliveryStatus = Literal["pending", "succeeded", "failed", "dead"]


class WebhookEventDefinition(BaseModel):
    type: WebhookEventType = Field(
        description="Event identifier used in subscriptions."
    )
    label: str = Field(description="Short display label for this event.")
    channel: str = Field(description="Display group, such as Email, Calls, or SMS.")


class WebhookEventCatalogResponse(BaseModel):
    items: list[WebhookEventDefinition] = Field(description="Supported webhook events.")


class WebhookSubscriptionCreate(BaseModel):
    authorization: SecretStr | None = Field(
        default=None,
        description="Optional Authorization header, e.g. Bearer <receiver key>. Write-only; encrypted at rest. Use printable ASCII, at most 2000 characters.",
    )
    target_url: str = Field(
        min_length=1, description="HTTPS URL Hail POSTs event payloads to."
    )
    event_types: list[WebhookEventType] = Field(
        min_length=1,
        description="Event types to subscribe to (e.g. 'call.completed', 'email.bounced'). At least one required.",
    )


class WebhookSubscriptionPatch(BaseModel):
    authorization: SecretStr | None = Field(
        default=None,
        description="Replacement Authorization header. Omit or null to keep it; empty string removes it. A changed target URL requires replacement or removal when a header is saved.",
    )
    target_url: str | None = Field(
        default=None, description="New delivery URL. Omit to leave unchanged."
    )
    event_types: list[WebhookEventType] | None = Field(
        default=None,
        description="New set of subscribed event types. Omit to leave unchanged.",
    )
    status: WebhookSubscriptionStatus | None = Field(
        default=None,
        description="Set to 'disabled' to pause deliveries, or 'active' to resume. Omit to leave unchanged.",
    )


class WebhookTestRequest(BaseModel):
    target_url: str = Field(
        min_length=1, description="HTTPS URL to send the test event to."
    )
    authorization: SecretStr | None = Field(
        default=None,
        description="Authorization header to send, e.g. Bearer <receiver key>. With subscription_id, omit or null to send the saved header (only when target_url is the saved URL); an empty string sends none.",
    )
    subscription_id: UUID | None = Field(
        default=None,
        description="Saved subscription to test. Its secret signs the test. Without it the test is not signed, because a new subscription has no secret yet.",
    )


class WebhookTestResponse(BaseModel):
    """Result of one test POST. Nothing is stored or retried."""

    ok: bool = Field(description="True when the receiver answered with a 2xx status.")
    status_code: int | None = Field(
        default=None,
        description="HTTP status the receiver answered with. Null when no answer came.",
    )
    duration_ms: int = Field(description="Time from sending to the answer or error.")
    response_body: str | None = Field(
        default=None,
        description="Start of the receiver's answer (at most 4096 characters), with the Authorization header removed.",
    )
    error: str | None = Field(
        default=None, description="Why no answer came (timeout, connection refused)."
    )
    signed: bool = Field(
        description="Whether the test carried an X-Hail-Signature header."
    )


class WebhookSubscriptionResponse(BaseModel):
    """Subscription as returned by the API.

    ``secret`` is populated **only** by create + rotate-secret responses;
    later GETs return ``None`` so the plaintext never round-trips.
    """

    id: UUID = Field(description="Unique identifier for this subscription.")
    has_authorization: bool = Field(
        default=False,
        description="Whether an Authorization header is saved. Its value is never returned.",
    )
    organization_id: UUID = Field(
        description="Organization that owns this subscription."
    )
    target_url: str = Field(description="HTTPS URL event payloads are POSTed to.")
    event_types: list[str] = Field(
        description="Event types this subscription receives."
    )
    status: WebhookSubscriptionStatus = Field(
        description="'active' (delivering) or 'disabled' (paused)."
    )
    consecutive_failures: int = Field(
        description="Consecutive failed delivery attempts since the last success. Resets to 0 on success."
    )
    last_success_at: datetime | None = Field(
        default=None,
        description="When a delivery last succeeded, ISO 8601 timestamp. Null if never.",
    )
    last_failure_at: datetime | None = Field(
        default=None,
        description="When a delivery last failed, ISO 8601 timestamp. Null if never.",
    )
    created_at: datetime = Field(
        description="When this subscription was created, ISO 8601 timestamp."
    )
    updated_at: datetime = Field(
        description="When this subscription was last modified, ISO 8601 timestamp."
    )
    secret: str | None = Field(
        default=None,
        description=(
            "Plaintext signing secret for verifying delivery payloads. "
            "Only present in the create and rotate-secret responses; "
            "every later read returns null."
        ),
    )

    model_config = ConfigDict(from_attributes=True)


class WebhookSubscriptionListResponse(BaseModel):
    items: list[WebhookSubscriptionResponse] = Field(
        description="Subscriptions in this page."
    )
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Null when there are no more results.",
    )


class WebhookDeliveryResponse(BaseModel):
    id: UUID = Field(description="Unique identifier for this delivery attempt.")
    subscription_id: UUID | None = Field(
        description="Subscription this delivery belongs to."
    )
    email_domain_id: UUID | None = Field(
        description=(
            "Email domain the triggering event relates to, if any. "
            "Informational only (surfaced as the X-Hail-Email-Domain "
            "header) — not a routing target."
        )
    )
    event_type: str = Field(
        description="The event type being delivered (e.g. 'call.completed')."
    )
    event_id: UUID = Field(
        description="Identifier of the underlying event that triggered this delivery."
    )
    attempt: int = Field(
        description="Number of delivery attempts made so far for this event, starting at 0."
    )
    status: WebhookDeliveryStatus = Field(
        description=(
            "'pending' (queued/retrying), 'succeeded', 'failed' (will "
            "retry), or 'dead' (retries exhausted)."
        )
    )
    response_status: int | None = Field(
        default=None,
        description="HTTP status code returned by the target URL on the last attempt. Null before any attempt.",
    )
    response_body: str | None = Field(
        default=None,
        description="Response body returned by the target URL on the last attempt, if any. Null before any attempt.",
    )
    next_attempt_at: datetime = Field(
        description="When the next delivery attempt is scheduled, ISO 8601 timestamp."
    )
    succeeded_at: datetime | None = Field(
        default=None,
        description="When this delivery succeeded, ISO 8601 timestamp. Null until it does.",
    )
    created_at: datetime = Field(
        description="When this delivery was queued, ISO 8601 timestamp."
    )

    model_config = ConfigDict(from_attributes=True)


class WebhookDeliveryListResponse(BaseModel):
    items: list[WebhookDeliveryResponse] = Field(
        description="Delivery attempts in this page."
    )
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Null when there are no more results.",
    )


# --------------------------------------------------------------------------- #
# Contacts — computed union of org members and manual rows
# (hailhq.core.contacts.search_contacts) plus the manual-contact CRUD schemas.
# --------------------------------------------------------------------------- #


def _normalize_contact_email(v: str | None) -> str | None:
    """Contacts-only email validator: fully lowercased at write time (unlike
    the shared ``_email_or_error``/``_normalize_domain`` rule elsewhere,
    which only lowercases the domain). Contacts stores are looked up
    case-insensitively in two places that must agree on what "the same
    email" means — the ``contacts_org_email_key`` unique index (which is
    case-sensitive, so it only dedupes ``Bob@x.com``/``bob@x.com`` if both
    are stored lowercase) and DSAR's ``func.lower(Contact.email)`` match
    (``hailhq.core.dsar.lookup_recipient``). Full lowercasing at write time
    makes both correct without a case-insensitive index. The ``contacts``
    table is unreleased (migration 0030, no production rows) so this needs
    no backfill."""
    if v is None:
        return v
    if not EMAIL_ADDR.match(v):
        raise ValueError("must be a valid email address (local@domain.tld)")
    return _normalize_domain(v).lower()


class ContactEntry(BaseModel):
    """One row in the computed contacts union — an org member or a manual
    contact. ``id`` is ``member:<user_id>`` for members, the contact row's
    UUID (as str) for manual rows."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(
        description="'member:<user_id>' for an org member, or the contact row's UUID (as a string) for a manual contact."
    )
    kind: Literal["member", "manual"] = Field(
        description="'member' if this row is a member of the organization, 'manual' if it was added as a contact."
    )
    name: str = Field(description="Display name.")
    phone_e164: str | None = Field(
        default=None, description="Phone number, E.164 format. Null if none on file."
    )
    email: str | None = Field(
        default=None, description="Email address. Null if none on file."
    )
    extension: str | None = Field(
        default=None,
        description="Extension on your connected phone system. Null if none on file.",
    )
    role: str | None = Field(
        default=None,
        description="Organization role (e.g. 'owner', 'admin', 'member') for kind='member'. Always null for kind='manual'.",
    )


class ContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=200,
        description="Display name for the contact.",
    )
    phone_e164: str | None = Field(
        default=None,
        description="Phone number, E.164 format. At least one of phone_e164, email or extension is required.",
    )
    email: str | None = Field(
        default=None,
        description="Email address, stored lowercased. At least one of phone_e164, email or extension is required.",
    )
    extension: str | None = Field(
        default=None,
        pattern=r"^[0-9*#]{1,6}$",
        description=(
            "Extension on your connected phone system (1 to 6 digits, * or #), "
            "for handover on calls to a connected number. At least one of "
            "phone_e164, email or extension is required."
        ),
    )

    _validate_phone = field_validator("phone_e164")(_e164_or_error)
    _validate_email = field_validator("email")(_normalize_contact_email)

    @model_validator(mode="after")
    def _phone_or_email(self) -> "ContactCreate":
        if self.phone_e164 is None and self.email is None and self.extension is None:
            raise ValueError("provide at least one of phone_e164, email or extension")
        return self


class ContactPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="New display name. Omit to leave unchanged; cannot be set to null.",
    )
    phone_e164: str | None = Field(
        default=None,
        description=(
            "New phone number, E.164 format. Omit to leave unchanged; "
            "explicit null clears it. The contact must keep at least one "
            "of phone_e164, email or extension."
        ),
    )
    email: str | None = Field(
        default=None,
        description=(
            "New email address, stored lowercased. Omit to leave "
            "unchanged; explicit null clears it. The contact must keep at "
            "least one of phone_e164, email or extension."
        ),
    )
    extension: str | None = Field(
        default=None,
        pattern=r"^[0-9*#]{1,6}$",
        description=(
            "New extension on your connected phone system (1 to 6 digits, * "
            "or #). Omit to leave unchanged; explicit null clears it. The "
            "contact must keep at least one of phone_e164, email or extension."
        ),
    )

    _validate_phone = field_validator("phone_e164")(_e164_or_error)
    _validate_email = field_validator("email")(_normalize_contact_email)

    @model_validator(mode="after")
    def _name_not_null(self) -> "ContactPatch":
        # name is NOT NULL on the row; an explicit `{"name": null}` would
        # otherwise reach the DB and surface as a 409 (indistinguishable
        # from a phone/email uniqueness conflict) instead of a clear 422.
        # `model_fields_set` distinguishes an explicit null from an omitted
        # field (both parse to `self.name is None`).
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        return self


class ContactListResponse(BaseModel):
    items: list[ContactEntry] = Field(description="Contacts in this page.")
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Null when there are no more results.",
    )


class MemberPhonePut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone_e164: str = Field(
        description="Phone number to save for the caller, E.164 format."
    )

    _validate_phone = field_validator("phone_e164")(_e164_or_error)


class WhoamiResponse(BaseModel):
    """Who the caller is — the answer ``GET /whoami`` gives.

    ``user_id``/``email``/``name`` are ``None`` for shared-key
    (``HAIL_API_KEY``) callers: that key carries no human identity. An
    agent that wants to put the operator's address in ``Reply-To`` reads
    ``email`` and skips the header when it is ``None``.
    """

    auth_kind: Literal["apikey", "jwt", "shared"] = Field(
        description=(
            "How the caller authenticated: 'apikey' (org API key), 'jwt' "
            "(logged-in user session), or 'shared' (the shared HAIL_API_KEY, "
            "which carries no human identity)."
        )
    )
    organization_id: UUID = Field(description="Organization the caller belongs to.")
    user_id: UUID | None = Field(
        default=None,
        description="The authenticated user's id. Null for 'shared' callers.",
    )
    email: str | None = Field(
        default=None,
        description="The authenticated user's email. Null for 'shared' callers.",
    )
    name: str | None = Field(
        default=None,
        description="The authenticated user's display name. Null for 'shared' callers.",
    )
    superadmin: bool = Field(
        default=False,
        description="True for a Hail staff console session acting on this organization.",
    )


# --------------------------------------------------------------------------- #
# Standing (per-organization) BYO provider config — the public /providers
# surface. The organization is always the one resolved from the API key, so
# it never appears in a request body or path here.
#
# These are the public contract: their names become the OpenAPI component
# names, and from there the Go CLI's and SDK's type names. The internal
# console router (``routes/internal/provider_config.py``) keeps its own
# request models; it is not refactored, and its handlers return bare dicts —
# the response models below are what give the public routes a schema.
#
# ``params`` stays a free-form object on the wire: its shape depends on the
# layer, and the canonical per-layer schemas are ``LLMParams`` / ``TTSParams``
# / ``STTParams`` in ``hailhq.core.provider_config``, which the route
# validates against (422 on a mismatch).
# --------------------------------------------------------------------------- #


class ProviderConfigUpsert(BaseModel):
    """Body of ``PUT /providers/{layer}`` — save and activate one provider.

    A partial write. Fields you omit keep the value already saved for this
    ``(layer, provider)`` pair: omit ``api_key`` to edit params without
    resending the key, send only the ``params`` keys you want to change,
    and omit ``fallback_enabled`` to leave the flag as it is. The merged
    result is what gets validated against the layer's schema (422 on a
    mismatch), so a partial write can never leave an invalid config behind.

    Rows are keyed by ``(organization, layer, provider)``, so writing a
    *different* provider for the same layer starts from scratch rather than
    inheriting the previous provider's params. On a brand-new row
    ``fallback_enabled`` defaults to ``false``.

    Keys are write-only: no response ever echoes one back.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        description="Provider name to save/activate for this layer (e.g. 'openai', 'cartesia')."
    )
    api_key: str | None = Field(
        default=None,
        description="API key for this provider. Omit to edit params without resending the key. Write-only — never echoed back.",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Provider-specific config, validated against the layer's "
            "schema (LLMParams/TTSParams/STTParams). Only the keys you "
            "send are changed; other saved keys are kept."
        ),
    )
    fallback_enabled: bool | None = Field(
        default=None,
        description="Whether to fall back to Hail's default provider on failure. Omit to leave unchanged; defaults to false on a new row.",
    )


class ProviderActivateRequest(BaseModel):
    """Body of ``POST /providers/{layer}/activate``."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        description="Previously saved provider to make active for this layer."
    )


class ProviderValidateRequest(BaseModel):
    """Body of ``POST /providers/{layer}/validate`` — a live key probe.

    All fields optional: with an empty body the layer's active provider and
    its stored key are tested. ``provider`` tests that provider's stored key
    instead. ``api_key`` tests a key that has not been saved yet.
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(
        default=None,
        description="Key to test instead of the stored one. Not persisted.",
    )
    provider: str | None = Field(
        default=None,
        description="Provider to test. Omitted: the layer's currently active provider.",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific config to test alongside the key.",
    )


class ProviderConfigEntry(BaseModel):
    """One saved provider row. Write-only key: ``key_last4`` and
    ``key_set_at`` are the only key-derived fields that leave the API.

    Every field is required (``key_last4``/``key_set_at`` nullable): the
    serializer always emits all of them, and required response fields
    generate plain values instead of pointers in the Go CLI's client.
    """

    layer: Literal["llm", "tts", "stt"] = Field(
        description="Voice-pipeline layer this config applies to."
    )
    provider: str = Field(description="Provider name (e.g. 'openai', 'cartesia').")
    key_last4: str | None = Field(
        description="Last 4 characters of the saved API key, for display. Null if no key is saved."
    )
    key_set_at: str | None = Field(
        description="When the API key was last set, ISO 8601 timestamp. Null if no key is saved."
    )
    params: dict[str, Any] = Field(
        description="Saved provider-specific config for this row."
    )
    fallback_enabled: bool = Field(
        description="Whether Hail's default provider is used as a fallback if this one fails."
    )
    is_active: bool = Field(
        description="True if this is the row currently used by calls on this layer."
    )


class ProviderConfigListResponse(BaseModel):
    providers: list[ProviderConfigEntry] = Field(
        description="Every saved provider row for the organization, across all layers."
    )


class ProviderValidateResult(BaseModel):
    """Outcome of a live provider-key probe."""

    status: str = Field(
        description="Probe outcome: 'valid', 'invalid', or 'indeterminate' (the provider could not be reached)."
    )
    message: str | None = Field(
        description="Human-readable detail about the outcome. 'ok' on success, an error description otherwise."
    )


class Address(BaseModel):
    customer_name: str = Field(description="Name associated with the address.")
    street: str = Field(description="Street address.")
    city: str = Field(
        description="City or town of the street address, as written on mail sent there."
    )
    region: str = Field(description="State, province or region.")
    postal_code: str = Field(
        description="Postal or ZIP code of the street address, such as `94103`."
    )
    country_code: str = Field(description="ISO alpha-2 address country.")


class KeptDocumentAnswer(BaseModel):
    """A document slot's saved choice on a number registration."""

    option: str = Field(default="", description="The document option chosen.")
    fields: dict[str, str] = Field(
        default_factory=dict, description="The document's typed fields."
    )


class KeptFileInfo(BaseModel):
    """A file Hail keeps for a resubmit. The bytes are never returned."""

    slot: str = Field(description="The document slot it was filed for.")
    filename: str = Field(description="The file name as it was sent.")
    content_type: str = Field(
        description="'image/jpeg', 'image/png' or 'application/pdf'."
    )
    size_bytes: int = Field(description="Size in bytes.")


class SmsApplicationResponse(BaseModel):
    """Private saved answers of a registration: a US texting application
    (or verified sender details for an old filing), or a number
    registration kept for a resubmit."""

    schema_version: int = Field(
        default=1, description="Version of this saved-answer response format."
    )
    subject_type: Literal["person", "business"] = Field(
        description="Original sender route: person or business."
    )
    fields: dict[str, str] = Field(
        description="Saved application answers; legacy responses may contain only verified sender details."
    )
    address: Address | None = Field(
        default=None, description="Original sender address, when available."
    )
    documents: dict[str, KeptDocumentAnswer] | None = Field(
        default=None,
        description=(
            "Number registration only: each document slot's option and "
            "fields, by slot name. Null for US texting."
        ),
    )
    files: list[KeptFileInfo] = Field(
        default_factory=list,
        description=(
            "Number registration only: the files Hail keeps, by slot. A "
            "resubmit reuses each one unless you send a new file for its slot."
        ),
    )
    kept_until: datetime | None = Field(
        default=None,
        description="Number registration only: when Hail deletes these answers and files.",
    )
    quote_id: UUID | None = Field(
        default=None,
        description=(
            "Number registration only: the quote it was filed for. It keeps "
            "working with GET /v1/verifications/requirements and the resubmit "
            "while the answers are kept."
        ),
    )
    hold_cents: int | None = Field(
        default=None,
        description=(
            "Number registration only: the credit a resubmit with that quote "
            "sets aside (its setup and first month). Null when nothing is set "
            "aside or the quote is gone."
        ),
    )


class SmsApplicationAmendRequest(BaseModel):
    """Complete replacement answers for an existing sender's missing evidence."""

    model_config = ConfigDict(extra="forbid")
    fields: dict[str, str] = Field(
        description="Complete current form answers. The legal sender must remain unchanged."
    )
    address: Address | None = Field(
        default=None,
        description="Original sender address; changing it requires support.",
    )


class VerificationResponse(BaseModel):
    """A carrier verification: its state and the name of the person or
    company it is for. No other personal data."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Unique identifier for this verification.")
    number_id: UUID | None = Field(
        default=None,
        description="The owned number covered by toll-free SMS verification. Null for organization-wide registrations.",
    )
    country_code: str = Field(description="ISO alpha-2 country code.")
    number_type: str = Field(
        description="Kind of number: 'local', 'mobile', 'toll_free', or 'national'."
    )
    subject_type: str = Field(description="'person' or 'business'.")
    subject_name: str | None = Field(
        default=None,
        description=(
            "Name of the person or company this verification is for, taken "
            "from the form (for example the business name, or the first and "
            "last name). Null when unknown."
        ),
    )
    purpose: str = Field(
        description=(
            "'number': permission to buy a number in this country. "
            "'sms': US local texting registration or per-number US/Canada toll-free texting verification."
        )
    )
    state: str = Field(
        description=(
            "'awaiting_review', 'submitting', 'submitted', 'approved', 'rejected', "
            "or 'cancelled'."
        )
    )
    rejection_reason: str | None = Field(
        default=None, description="Why it was rejected. Null unless rejected."
    )
    created_at: datetime = Field(description="When the verification was created.")
    updated_at: datetime = Field(description="When it last changed.")
    submitted_at: datetime | None = Field(
        default=None, description="When it was submitted to the carrier."
    )
    approved_at: datetime | None = Field(
        default=None, description="When the carrier approved it."
    )
    confirmation: str | None = Field(
        default=None,
        description=(
            "For a person's US texting registration: 'code' when you must enter "
            "the code the carrier texted (POST /verifications/{id}/confirm), "
            "'reply' when you must answer the carrier's text, null when nothing "
            "is pending."
        ),
    )
    step: (
        Literal[
            "confirm_identity",
            "filing",
            "carrier_review",
            "runtime_setup",
            "needs_information",
        ]
        | None
    ) = Field(
        default=None,
        description=(
            "For a US texting registration that is not yet approved or "
            "rejected: 'confirm_identity' while the person must confirm (see "
            "`confirmation`), 'filing' while Hail files the details with the "
            "carriers, 'carrier_review' once the carriers are reviewing them "
            "(1 to 7 business days), or runtime_setup when accepted but required "
            "messaging responses still need configuration; needs_information when "
            "the existing sender needs current campaign evidence before filing. Null otherwise."
        ),
    )
    campaign_accepted: bool = Field(
        default=False,
        description="The carrier accepted the SMS campaign; this alone does not mean a number is ready to send.",
    )
    runtime_ready: bool | None = Field(
        default=None,
        description="True when required runtime configuration was verified, false while setup retries, null when not independently tracked.",
    )
    cancellable: bool = Field(
        default=False,
        description="True when `DELETE /v1/verifications/{id}` accepts this verification now.",
    )
    kept_until: datetime | None = Field(
        default=None,
        description=(
            "For a number registration: when Hail deletes the answers and "
            "files it keeps so the filing can be fixed and sent again (30 "
            "days after submit). They go sooner when it is approved or "
            "cancelled, or the account closes. Null when nothing is kept."
        ),
    )
    resubmittable: bool = Field(
        default=False,
        description=(
            "True when `POST /v1/verifications/{id}/resubmit` accepts this "
            "verification now: a rejected number registration whose answers "
            "are still kept (`kept_until`)."
        ),
    )
    identity_confirmed: bool = Field(
        default=False,
        description=(
            "True on a person's newest US texting registration when it is "
            "cancelled, or rejected and not yet dismissed, and the person stays "
            "confirmed: the next filing with the same first name, last name, "
            "email and mobile needs no code and has no submission fee. Dismiss "
            "a rejected registration first. Ignore it on older registrations."
        ),
    )
    waiting_for_credit: bool = Field(
        default=False,
        description=(
            "True when a US texting registration waits for credit: its next "
            "step, filing the campaign, has a carrier fee your balance does "
            "not cover. Nothing is filed until it does. Add credit; Hail "
            "goes on at its next check."
        ),
    )
    credit_needed_cents: int | None = Field(
        default=None,
        description=(
            "The credit this registration still needs before Hail files its "
            "campaign, in US cents; null when `waiting_for_credit` is false. "
            "Per registration, not a running total: the first waiting "
            "registration shows the carrier's campaign fee less the credit "
            "left after the registrations already covered (more when the "
            "balance is short), each later one its whole fee. Add the sum "
            "over every registration with `waiting_for_credit` true, and "
            "one top-up lets them all go. It follows the balance, so read it "
            "again before you pay."
        ),
    )
