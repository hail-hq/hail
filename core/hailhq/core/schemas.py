import base64
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from hailhq.core.languages import Language
from hailhq.core.sender_id import PLATFORM_DEFAULT_SENDER_ID
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

E164 = re.compile(r"^\+[1-9]\d{1,14}$")


def _e164_or_error(v: str | None) -> str | None:
    """Shared to/from validator for the phone-channel create schemas
    (``CallCreate``, ``SmsCreate``) — one place to tighten the rule or
    reword the error."""
    if v is not None and not E164.match(v):
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
# convention so SMS / email / conversation can join later without a second
# rename.
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
    conversation_id: UUID | None = Field(
        default=None,
        description=(
            "Groups this call with other calls/emails/SMS into one "
            "conversation thread. Omitted: the call is not linked to a "
            "conversation."
        ),
    )
    metadata: dict = Field(
        default_factory=dict,
        description=(
            "Free-form JSON object attached to the call and echoed back on "
            "reads. Not interpreted by Hail."
        ),
    )
    tools: list[str] | None = Field(
        default=None,
        description=(
            "Agent tools to allow on this call. Omitted: every tool the "
            "organization's configured channels support (new channels appear "
            "automatically). Empty list: no tools. Tool names are validated "
            "against the server's registry."
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

TERMINAL_CALL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "busy", "no_answer", "canceled"}
)


NumberType = Literal["local", "mobile", "toll_free", "national"]


class CallResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Unique identifier for this call.")
    organization_id: UUID = Field(
        description="Organization that placed or received this call."
    )
    conversation_id: UUID | None = Field(
        description=(
            "Conversation thread this call is grouped into, if any. Null "
            "when the call was not linked to a conversation."
        )
    )
    from_e164: str = Field(description="Caller-id phone number used, E.164 format.")
    to_e164: str = Field(description="Recipient phone number, E.164 format.")
    direction: Literal["outbound", "inbound"] = Field(
        description="'outbound' for calls Hail placed, 'inbound' for calls received."
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
            "Sender phone number, E.164 format. Must be a number owned by "
            "the organization with the SMS capability. Omitted: an active "
            "org-owned number is used if one exists, else a number is "
            "claimed from the shared pool."
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
            "on reads. Not interpreted by Hail."
        ),
    )

    _validate_e164 = field_validator("to", "from_")(_e164_or_error)


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


class NumberAcquireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country_code: str = Field(
        min_length=2,
        max_length=2,
        description="ISO alpha-2 country code to acquire a number in (e.g. 'US'). Case-insensitive.",
    )
    number_type: NumberType = Field(
        default="local",
        description="Kind of number to acquire: 'local', 'mobile', 'toll_free', or 'national'.",
    )

    @field_validator("country_code")
    @classmethod
    def _uppercase_country_code(cls, v: str) -> str:
        # The telephony catalog, the provider, and the stored row all key on
        # uppercase ISO alpha-2; normalize here so "us" and "US" behave alike.
        return v.upper()


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
        description="'pending', 'active', 'failed', or 'released'."
    )
    is_dedicated: bool = Field(
        description="True if this number is owned by the organization. False for shared-pool numbers."
    )
    messaging_service_sid: str | None = Field(
        default=None,
        description="Provider messaging-service identifier once SMS has been enabled on this number. Null until then.",
    )


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
        description="How this entry was created: 'unsubscribe_link', 'manual' (an operator action), or 'bounce'."
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


class EmailDomainCreate(BaseModel):
    """Request body for POST /email-domains.

    For ``kind='hail_mail'`` ``domain`` is omitted; the server composes
    the full address as ``<local_prefix_user>+<local_prefix_org>@<base>``.
    Both prefixes are optional in the body and fall back to the
    ``HAIL_MAIL_DEFAULT_*_PREFIX`` env vars; the server returns 503 if
    neither is supplied. For ``kind='custom'`` ``domain`` is required and
    the prefix fields must be omitted.
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
    local_prefix_user: str | None = Field(
        default=None,
        description=(
            "User-chosen local-part prefix for a hail_mail address. Only "
            "valid for kind='hail_mail'. Falls back to "
            "HAIL_MAIL_DEFAULT_USER_PREFIX if omitted."
        ),
    )
    local_prefix_org: str | None = Field(
        default=None,
        description=(
            "Org-chosen local-part prefix for a hail_mail address. Only "
            "valid for kind='hail_mail'. Falls back to "
            "HAIL_MAIL_DEFAULT_ORG_PREFIX if omitted."
        ),
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

    @field_validator("local_prefix_user", "local_prefix_org")
    @classmethod
    def _validate_local_prefix(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if not LOCAL_PREFIX.match(v):
            raise ValueError(
                "must be 1–20 chars of lowercase a–z, 0–9, or '-', "
                "with no leading or trailing '-'"
            )
        return v

    @model_validator(mode="after")
    def _kind_field_consistency(self):
        if self.kind == "custom":
            if not self.domain:
                raise ValueError("domain is required when kind='custom'")
            if self.local_prefix_user is not None or self.local_prefix_org is not None:
                raise ValueError(
                    "local_prefix_user/local_prefix_org are only valid when "
                    "kind='hail_mail'"
                )
        if self.kind == "hail_mail" and self.domain is not None:
            raise ValueError(
                "domain must be omitted when kind='hail_mail'; the server "
                "composes it from local_prefix_user + local_prefix_org"
            )
        return self


class EmailDomainPatch(BaseModel):
    """Body for PATCH /email-domains/{id}.

    Two clusters of mutable fields:

    * Hail-mail addressing — the user/org prefix pair (only valid on
      ``kind='hail_mail'`` rows; the handler returns 422 if a tenant tries
      to PATCH these on a custom row).
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

    local_prefix_user: str | None = Field(
        default=None,
        description="New user local-part prefix. Only valid on kind='hail_mail' rows. Omit to leave unchanged.",
    )
    local_prefix_org: str | None = Field(
        default=None,
        description="New org local-part prefix. Only valid on kind='hail_mail' rows. Omit to leave unchanged.",
    )
    inbound_enabled: bool | None = Field(
        default=None,
        description="Whether to accept inbound mail on this domain. Requires forward_to (or an existing one) when true. Omit to leave unchanged.",
    )
    forward_to: list[str] | None = Field(
        default=None,
        description="Email addresses to forward inbound mail to. Omit to leave unchanged.",
    )
    forward_rate_per_hour: int | None = Field(
        default=None,
        description="Cap on forwarded messages per hour. Omit to leave unchanged.",
    )

    @field_validator("local_prefix_user", "local_prefix_org")
    @classmethod
    def _validate_local_prefix(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if not LOCAL_PREFIX.match(v):
            raise ValueError(
                "must be 1–20 chars of lowercase a–z, 0–9, or '-', "
                "with no leading or trailing '-'"
            )
        return v

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
        if (
            self.local_prefix_user is None
            and self.local_prefix_org is None
            and self.inbound_enabled is None
            and self.forward_to is None
            and self.forward_rate_per_hour is None
        ):
            raise ValueError(
                "at least one of local_prefix_user, local_prefix_org, "
                "inbound_enabled, forward_to, "
                "or forward_rate_per_hour must be set"
            )
        return self


class EmailDomainResponse(BaseModel):
    """Read view for an email domain.

    The inbound-action fields (``inbound_enabled``, ``forward_to``,
    ``forward_rate_per_hour``) surface what the row has configured for
    incoming mail.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Unique identifier for this email domain.")
    organization_id: UUID = Field(description="Organization that owns this domain.")
    kind: EmailDomainKind = Field(
        description="'hail_mail' (Hail-hosted address) or 'custom' (your own domain)."
    )
    domain: str = Field(description="The DNS domain mail is sent from.")
    local_prefix_user: str | None = Field(
        description="User local-part prefix for a hail_mail address. Null for kind='custom'."
    )
    local_prefix_org: str | None = Field(
        description="Org local-part prefix for a hail_mail address. Null for kind='custom'."
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
    forward_to: list[str] | None = Field(
        default=None,
        description="Email addresses inbound mail is forwarded to, if inbound is enabled.",
    )
    forward_rate_per_hour: int | None = Field(
        default=None,
        description="Configured cap on forwarded messages per hour, if set.",
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


class EmailDomainListResponse(BaseModel):
    items: list[EmailDomainResponse] = Field(description="Email domains in this page.")
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Null when there are no more results.",
    )
    # The address a send with no ``from`` goes out as. ``None`` when such a
    # send would be rejected: several verified identities (name one), or
    # none that can send yet. Computed across the whole org, not the page.
    default_from: str | None = Field(
        default=None,
        description=(
            "The From address used when a send omits 'from'. Null when no "
            "such default can be resolved (e.g. multiple verified "
            "identities, or none that can send yet). Computed across the "
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
    conversation_id: UUID | None = Field(
        default=None,
        description=(
            "Groups this email with other calls/emails/SMS into one "
            "conversation thread. Omitted: the email is not linked to a "
            "conversation."
        ),
    )
    metadata: dict = Field(
        default_factory=dict,
        description="Free-form JSON object attached to the email and echoed back on reads. Not interpreted by Hail.",
    )
    attachment_ids: list[UUID] | None = Field(
        default=None,
        description="Ids returned by POST /email-attachments to attach to this send. Omitted: no attachments.",
    )

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
            "Distinct emails opened at least once in the window " "(HTML emails only)."
        ),
    )
    unique_clicked: int = Field(
        default=0,
        description=(
            "Distinct emails clicked at least once in the window " "(HTML emails only)."
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
    conversation_id: UUID | None = Field(
        description=(
            "Conversation thread this email is grouped into, if any. Null "
            "when it was not linked to a conversation."
        )
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
        description="RFC 5322 In-Reply-To header. Inbound emails only; null on outbound rows.",
    )
    references_ids: list[str] | None = Field(
        default=None,
        description="RFC 5322 References header, split into ids. Inbound emails only; null on outbound rows.",
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

# email.received.suppressed fires with data.reason ∈ {forward_loop,
# forward_rate_limit, inbound_rate_limit, insufficient_funds}. SES
# deliverability tracking includes email.delivered, email.delivery_delayed,
# email.bounced, email.complained, email.opened, and email.clicked.
WebhookEventType = Literal[
    "email.received",
    "email.delivered",
    "email.delivery_delayed",
    "email.bounced",
    "email.complained",
    "email.opened",
    "email.clicked",
    "email.received.suppressed",
    "email.send_failed",
    "sms.received",
    "sms.delivered",
    "sms.undelivered",
    "sms.failed",
    "call.answered",
    "call.completed",
    "call.failed",
    "call.busy",
    "call.no_answer",
]

WebhookSubscriptionStatus = Literal["active", "disabled"]
WebhookDeliveryStatus = Literal["pending", "succeeded", "failed", "dead"]


class WebhookSubscriptionCreate(BaseModel):
    target_url: str = Field(
        min_length=1, description="HTTPS URL Hail POSTs event payloads to."
    )
    event_types: list[WebhookEventType] = Field(
        min_length=1,
        description="Event types to subscribe to (e.g. 'call.completed', 'email.bounced'). At least one required.",
    )


class WebhookSubscriptionPatch(BaseModel):
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


class WebhookSubscriptionResponse(BaseModel):
    """Subscription as returned by the API.

    ``secret`` is populated **only** by create + rotate-secret responses;
    later GETs return ``None`` so the plaintext never round-trips.
    """

    id: UUID = Field(description="Unique identifier for this subscription.")
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
        description="Phone number, E.164 format. At least one of phone_e164 or email is required.",
    )
    email: str | None = Field(
        default=None,
        description="Email address, stored lowercased. At least one of phone_e164 or email is required.",
    )

    _validate_phone = field_validator("phone_e164")(_e164_or_error)
    _validate_email = field_validator("email")(_normalize_contact_email)

    @model_validator(mode="after")
    def _phone_or_email(self) -> "ContactCreate":
        if self.phone_e164 is None and self.email is None:
            raise ValueError("provide at least one of phone_e164 or email")
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
            "of phone_e164 or email."
        ),
    )
    email: str | None = Field(
        default=None,
        description=(
            "New email address, stored lowercased. Omit to leave "
            "unchanged; explicit null clears it. The contact must keep at "
            "least one of phone_e164 or email."
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
