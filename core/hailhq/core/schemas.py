import base64
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

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

    base_url: str
    api_key: str
    model: str

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

    stt: Literal["deepgram"] = "deepgram"
    tts: Literal["cartesia"] = "cartesia"
    vad: Literal["silero"] = "silero"
    turn_detection: Literal["livekit"] = "livekit"
    # Per-call TTS voice override. Applies to whichever TTS provider serves
    # the call (org BYO config or Hail default). None → org/env default.
    voice_id: str | None = None


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

    to: str
    from_: str | None = Field(default=None, alias="from")
    system_prompt: str | None = None
    llm: LLMConfig | None = None
    first_message: str | None = None
    voice_config: VoiceConfig = Field(default_factory=VoiceConfig)
    conversation_id: UUID | None = None
    metadata: dict = Field(default_factory=dict)

    _validate_e164 = field_validator("to", "from_")(_e164_or_error)

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

TERMINAL_CALL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "busy", "no_answer", "canceled"}
)


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


class SmsCreate(ConsentAttestationMixin):
    model_config = ConfigDict(extra="forbid")

    to: str
    from_: str | None = Field(default=None, alias="from")
    body: str = Field(min_length=1, max_length=1600)
    metadata: dict = Field(default_factory=dict)

    _validate_e164 = field_validator("to", "from_")(_e164_or_error)


SmsStatus = Literal["queued", "sent", "delivered", "failed", "undelivered", "received"]


class SmsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    from_e164: str
    to_e164: str
    direction: Literal["outbound", "inbound"]
    status: SmsStatus
    body: str
    provider_message_sid: str | None
    segment_count: int
    error_code: str | None
    requested_at: datetime
    sent_at: datetime | None


class SmsListResponse(BaseModel):
    items: list[SmsResponse]
    next_cursor: str | None = None


SENDER_ID_RE = re.compile(r"^[A-Za-z0-9]{2,11}$")


class SenderIdPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    custom_sender_id: str | None = None

    @field_validator("custom_sender_id")
    @classmethod
    def _validate_sender_id(cls, v: str | None) -> str | None:
        if v is not None and not SENDER_ID_RE.match(v):
            raise ValueError(
                "must be 2-11 alphanumeric characters, no spaces or symbols"
            )
        return v


class SenderIdResponse(BaseModel):
    custom_sender_id: str | None
    effective_default: str = "HAIL"


class NumberAcquireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country_code: str = Field(min_length=2, max_length=2)
    number_type: NumberType = "local"


class PhoneNumberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    e164: str
    country_code: str
    number_type: str
    capabilities: list[str]
    provisioning_state: str
    is_dedicated: bool = Field(validation_alias="is_pool", serialization_alias="is_dedicated")
    messaging_service_sid: str | None = None

    @field_validator("is_dedicated", mode="before")
    @classmethod
    def _invert_is_pool(cls, v: bool) -> bool:
        return not v


class PhoneNumberListResponse(BaseModel):
    items: list[PhoneNumberResponse]
    next_cursor: str | None = None


class SuppressionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    recipient: str
    channel: str
    reason: str
    source: str
    created_at: datetime


class SuppressionListResponse(BaseModel):
    items: list[SuppressionResponse]
    next_cursor: str | None = None


class EventResponse(BaseModel):
    """One event on the unified GET /events stream (call, email, or SMS)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: Literal["call", "email", "sms"]
    call_id: UUID | None = None
    email_id: UUID | None = None
    sms_id: UUID | None = None
    kind: str
    payload: dict[str, Any]
    occurred_at: datetime


class EventStreamResponse(BaseModel):
    items: list[EventResponse]
    next_cursor: str | None = None
    # Only populated when the ``id`` query filter resolves to a call (e.g.
    # ``id=call:<uuid>``). Org-wide tails and non-call resource types leave
    # this null — there's no single "the" status to report.
    call_status: CallStatus | None = None


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

    name: str
    value: str
    type: Literal["CNAME", "MX", "TXT"] = "CNAME"
    priority: int | None = None


class DomainCheckResponse(BaseModel):
    domain: str
    in_use: bool
    existing_mx: list[str]
    suggested_domain: str


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

    kind: EmailDomainKind
    domain: str | None = None
    local_prefix_user: str | None = None
    local_prefix_org: str | None = None

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

    local_prefix_user: str | None = None
    local_prefix_org: str | None = None
    inbound_enabled: bool | None = None
    forward_to: list[str] | None = None
    forward_rate_per_hour: int | None = None

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

    id: UUID
    organization_id: UUID
    kind: EmailDomainKind
    domain: str
    local_prefix_user: str | None
    local_prefix_org: str | None
    verification_status: EmailDomainVerificationStatus
    dns_records: list[DnsRecordSchema]
    mail_from_domain: str | None
    mail_from_status: str | None = None
    provider: str
    verified_at: datetime | None
    inbound_enabled: bool = False
    forward_to: list[str] | None = None
    forward_rate_per_hour: int | None = None
    created_at: datetime
    updated_at: datetime
    # Populated by POST /{id}/verify on custom domains only; None everywhere else.
    # True when the domain's published MX points at the SES inbound host.
    receive_ready: bool | None = None


class EmailDomainListResponse(BaseModel):
    items: list[EmailDomainResponse]
    next_cursor: str | None = None


class EmailCreate(ConsentAttestationMixin):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ``from`` is reserved; ``from_`` mirrors how CallCreate handles it.
    from_: str | None = Field(default=None, alias="from")
    to: list[str] = Field(min_length=1)
    cc: list[str] | None = None
    bcc: list[str] | None = None
    reply_to: str | None = None
    subject: str = Field(min_length=1, max_length=998)
    body_text: str | None = None
    body_html: str | None = None
    conversation_id: UUID | None = None
    metadata: dict = Field(default_factory=dict)
    attachment_ids: list[UUID] | None = None

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

    id: UUID
    email_id: UUID
    kind: EmailEventKind
    payload: dict[str, Any]
    occurred_at: datetime


class EmailEventListResponse(BaseModel):
    items: list[EmailEventResponse]
    next_cursor: str | None = None


class EmailStatsCounts(BaseModel):
    sent: int = 0
    delivered: int = 0
    delivery_delayed: int = 0
    bounced: int = 0
    bounced_hard: int = 0
    complained: int = 0
    rejected: int = 0
    opened: int = 0
    clicked: int = 0
    unique_opened: int = 0
    unique_clicked: int = 0


class EmailStatsBucket(EmailStatsCounts):
    bucket_start: datetime


class EmailStatsRates(BaseModel):
    """All None when sent == 0 in the window."""

    delivery: float | None = None
    bounce: float | None = None  # hard bounces / sent
    complaint: float | None = None
    open: float | None = None  # unique_opened / sent
    click: float | None = None  # unique_clicked / sent


class EmailStatsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_ts: datetime = Field(
        serialization_alias="from",
        validation_alias=AliasChoices("from_ts", "from"),
    )
    to_ts: datetime = Field(
        serialization_alias="to",
        validation_alias=AliasChoices("to_ts", "to"),
    )
    bucket: Literal["hour", "day"]
    totals: EmailStatsCounts
    rates: EmailStatsRates
    series: list[EmailStatsBucket]


class EmailSummary(BaseModel):
    """Trimmed view for list endpoints — drops the message bodies.

    Bodies can be large and contain PII; paging through a year of mail
    shouldn't return every byte of every message just to render a list.
    Use ``EmailResponse`` (via ``GET /emails/{id}``) for the full row.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    organization_id: UUID
    conversation_id: UUID | None
    email_domain_id: UUID | None
    direction: Literal["outbound", "inbound"] = "outbound"
    from_address: str
    to_addresses: list[str]
    cc_addresses: list[str] | None
    bcc_addresses: list[str] | None
    reply_to: str | None
    subject: str
    status: EmailStatus
    end_reason: str | None
    provider_message_id: str | None
    requested_at: datetime
    sent_at: datetime | None
    failed_at: datetime | None
    # ``Email.metadata_`` is the SQLAlchemy attribute (``metadata`` is
    # reserved by Declarative). The validation_alias bridges that name so
    # ``from_attributes=True`` reads the right column; the field on the
    # response is still called ``metadata`` on the wire.
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias="metadata_")


class EmailAttachmentResponse(BaseModel):
    """One inbound MIME attachment as exposed to API consumers.

    ``url`` is the stable Hail API endpoint that 302-redirects to a
    presigned S3 URL on access — see GET /emails/{id}/attachments/{aid}.
    """

    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    content_id: str | None = None
    url: str

    model_config = ConfigDict(from_attributes=True)


class EmailAttachmentUploadResponse(BaseModel):
    """Returned by POST /email-attachments.

    ``id`` is reusable across many ``POST /emails`` calls via
    ``EmailCreate.attachment_ids`` until Hail garbage-collects it (24h
    if never referenced by a send).
    """

    id: UUID
    filename: str
    content_type: str
    size_bytes: int

    model_config = ConfigDict(from_attributes=True)


class EmailResponse(EmailSummary):
    body_text: str | None
    body_html: str | None
    # Inbound-only metadata. Outbound rows leave these all null/empty —
    # we surface them on the full-row endpoint (GET /emails/{id}) rather
    # than the list summary because most inbound consumers will fetch the
    # row anyway to read the body. Defaults match the outbound shape so
    # existing serializations keep working.
    message_id: str | None = None
    in_reply_to: str | None = None
    references_ids: list[str] | None = None
    spam_verdict: str | None = None
    virus_verdict: str | None = None
    dkim_verdict: str | None = None
    spf_verdict: str | None = None
    dmarc_verdict: str | None = None
    provider_received_at: datetime | None = None
    # ``raw_url`` is the API endpoint that 302-redirects to a presigned
    # S3 URL for the original MIME blob; ``raw_s3_key`` is the column on
    # the row but we don't expose internal storage paths on the wire.
    raw_url: str | None = None
    attachments: list[EmailAttachmentResponse] = []
    last_event_at: datetime | None = None


class EmailListResponse(BaseModel):
    items: list[EmailSummary]
    next_cursor: str | None = None


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
    "sms.received",
]

WebhookSubscriptionStatus = Literal["active", "disabled"]
WebhookDeliveryStatus = Literal["pending", "succeeded", "failed", "dead"]


class WebhookSubscriptionCreate(BaseModel):
    target_url: str = Field(min_length=1)
    event_types: list[WebhookEventType] = Field(min_length=1)


class WebhookSubscriptionPatch(BaseModel):
    target_url: str | None = None
    event_types: list[WebhookEventType] | None = None
    status: WebhookSubscriptionStatus | None = None


class WebhookSubscriptionResponse(BaseModel):
    """Subscription as returned by the API.

    ``secret`` is populated **only** by create + rotate-secret responses;
    later GETs return ``None`` so the plaintext never round-trips.
    """

    id: UUID
    organization_id: UUID
    target_url: str
    event_types: list[str]
    status: WebhookSubscriptionStatus
    consecutive_failures: int
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    secret: str | None = None

    model_config = ConfigDict(from_attributes=True)


class WebhookSubscriptionListResponse(BaseModel):
    items: list[WebhookSubscriptionResponse]
    next_cursor: str | None = None


class WebhookDeliveryResponse(BaseModel):
    id: UUID
    subscription_id: UUID | None
    email_domain_id: UUID | None
    event_type: str
    event_id: UUID
    attempt: int
    status: WebhookDeliveryStatus
    response_status: int | None = None
    response_body: str | None = None
    next_attempt_at: datetime
    succeeded_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WebhookDeliveryListResponse(BaseModel):
    items: list[WebhookDeliveryResponse]
    next_cursor: str | None = None
