"""Pydantic models for the Hail v1 API.

Mirror of ``core/hailhq/core/schemas.py`` — duplicated by hand because
the SDK ships standalone (``pip install hail-sdk`` must not pull any
``hailhq.*`` packages). Keep this file in lockstep with core's schemas
when fields change. A future task will codegen these from
``openapi/openapi.yaml``; until then the duplication is intentional and
audited by hand.

One intentional deviation from core:

1. ``CallCreate`` is configured with ``populate_by_name=True`` so
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


def _e164_or_error(v: str | None) -> str | None:
    """Shared to/from validator for the phone-channel create schemas
    (``CallCreate``, ``SmsCreate``) — one place to tighten the rule or
    reword the error. Mirrors ``core.schemas._e164_or_error``."""
    if v is not None and not E164.match(v):
        raise ValueError("must be E.164 (e.g. +14155551234)")
    return v


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: str
    model: str


class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tts: Literal["cartesia"] = "cartesia"
    vad: Literal["silero"] = "silero"
    turn_detection: Literal["livekit"] = "livekit"
    # Spoken language for the call (lowercase ISO 639-1, e.g. "da").
    # The *server* enforces the supported set (39 codes — see
    # docs/languages.md and the OpenAPI enum); the SDK deliberately stays
    # permissive (shape-only) so existing SDK releases keep working when
    # languages are added server-side. None -> provider defaults (English).
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")


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


class CallCreate(BaseModel):
    """Body shape for ``POST /calls``.

    Mode A: pass ``system_prompt``. Mode B: pass a full ``llm`` block. At
    least one is required; both together is allowed and means "run this
    prompt on my own endpoint" (mirrors the CLI's ``--prompt`` /
    ``--llm-*`` rule).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    to: str
    from_: str | None = Field(default=None, alias="from")
    system_prompt: str | None = None
    llm: LLMConfig | None = None
    first_message: str | None = None
    # Speak the AI self-disclosure line first on the call (default True).
    # Disabling is the caller's responsibility — see the API schema notes
    # on 47 CFR 64.1200(b)(1) and state AI bot-disclosure laws.
    ai_disclosure: bool = True
    voice_config: VoiceConfig = Field(default_factory=VoiceConfig)
    conversation_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Agent tools opt-out. Omitted (None): every tool the org's configured
    # channels support; []: no tools. Names are validated server-side
    # against the registry (unknown names 422).
    tools: list[str] | None = None
    recipient_consent: bool
    consent_source: str | None = None
    consent_obtained_at: datetime | None = None
    message_type: Literal["marketing", "informational"] = "informational"

    _validate_e164 = field_validator("to", "from_")(_e164_or_error)

    @model_validator(mode="after")
    def _prompt_or_llm(self) -> CallCreate:
        """Mirrors ``hailhq.core.schemas.CallCreate._prompt_or_llm``: at
        least one of the two, both together permitted."""
        has_prompt = self.system_prompt is not None and self.system_prompt != ""
        has_llm = self.llm is not None
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


class SmsCreate(BaseModel):
    """Body shape for ``POST /sms``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    to: str
    from_: str | None = Field(default=None, alias="from")
    body: str = Field(min_length=1, max_length=1600)
    metadata: dict[str, Any] = Field(default_factory=dict)
    recipient_consent: bool
    consent_source: str | None = None
    consent_obtained_at: datetime | None = None
    message_type: Literal["marketing", "informational"] = "informational"

    _validate_e164 = field_validator("to", "from_")(_e164_or_error)


SmsStatus = Literal["queued", "sent", "delivered", "failed", "undelivered", "received"]


class SmsResponse(BaseModel):
    """Shape returned by ``POST /sms`` and ``GET /sms/{id}``."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    from_e164: str
    to_e164: str
    direction: Literal["outbound", "inbound"]
    status: SmsStatus
    body: str
    provider_message_sid: str | None = None
    segment_count: int
    error_code: str | None = None
    requested_at: datetime
    sent_at: datetime | None = None


class SmsListResponse(BaseModel):
    items: list[SmsResponse]
    next_cursor: str | None = None


class SuppressionResponse(BaseModel):
    """Shape returned by ``GET /sms/suppressions`` items — one opted-out
    recipient. Mirrors ``core/hailhq/core/schemas.py:SuppressionResponse``.
    """

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


# --------------------------------------------------------------------------- #
# Number + sender-id models — mirror hailhq.core.schemas. A dedicated
# PhoneNumber is cross-channel (voice + SMS); sender-id is the org's custom
# alphanumeric "from" label for SMS.
# --------------------------------------------------------------------------- #


class PhoneNumberResponse(BaseModel):
    """Shape returned by the ``/numbers`` endpoints.

    ``is_dedicated`` is the server-side inversion of the internal ``is_pool``
    flag: dedicated numbers belong to one org, pool numbers are shared.
    ``capabilities`` are fixed by the carrier at purchase time.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    e164: str
    country_code: str
    number_type: str
    capabilities: list[str]
    provisioning_state: str
    is_dedicated: bool
    messaging_service_sid: str | None = None


class PhoneNumberListResponse(BaseModel):
    items: list[PhoneNumberResponse]
    next_cursor: str | None = None


class SenderIdResponse(BaseModel):
    """Shape returned by ``GET`` / ``PATCH /sms/sender-id``.

    ``custom_sender_id`` is null when the org hasn't set one;
    ``effective_default`` is the platform fallback used in that case.
    """

    model_config = ConfigDict(from_attributes=True)

    custom_sender_id: str | None = None
    effective_default: str = "HAIL"


class CallEventResponse(BaseModel):
    """One event on the unified ``GET /events`` stream (mirrors the API's
    ``EventResponse``). The name predates the unified stream — it carries
    call, email, and SMS events; kept for backwards compatibility. The
    server sets exactly one of ``call_id`` / ``email_id`` / ``sms_id``.
    """

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
    items: list[CallEventResponse]
    next_cursor: str | None = None
    # Only populated when the ``id`` filter resolves to a single call. Org-wide
    # tails leave this null — there's no single "the" status.
    call_status: CallStatus | None = None


# --------------------------------------------------------------------------- #
# Email models — mirror hailhq.core.schemas. Lockstep maintenance, audited
# by hand; codegen will replace this once openapi → SDK is wired.
# --------------------------------------------------------------------------- #

EMAIL_ADDR = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _normalize_domain(addr: str) -> str:
    """Lowercase the domain portion of an email address.

    Domains are case-insensitive (RFC 5321); local parts are preserved
    verbatim. Matches ``hailhq.core.schemas._normalize_domain`` so the
    SDK and the server agree on what's stored.
    """
    local, _, domain = addr.partition("@")
    if not domain:
        return addr
    return f"{local}@{domain.lower()}"


EmailStatus = Literal["queued", "sent", "failed", "bounced", "complained", "received"]

TERMINAL_EMAIL_STATUSES: frozenset[str] = frozenset(
    {"sent", "failed", "bounced", "complained"}
)


class EmailCreate(BaseModel):
    """Body shape for ``POST /emails``.

    At least one of ``body_text`` / ``body_html`` is required. ``to`` is a
    non-empty list of recipient addresses. ``from_`` is optional while the
    org has one verified sender; with several the server 422s and the
    caller must name one (or none at all, which auto-mints a hail-mail
    address per the operator's configuration). ``populate_by_name=True``
    so ``EmailCreate(from_="...")`` works at the Python boundary while
    ``model_dump(by_alias=True)`` still emits ``"from"`` on the wire.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    to: list[str] = Field(min_length=1)
    subject: str = Field(min_length=1, max_length=998)
    from_: str | None = Field(default=None, alias="from")
    # Display name for the From: header ("Acme Billing <billing@acme.com>").
    from_name: str | None = Field(default=None, max_length=256)
    cc: list[str] | None = None
    bcc: list[str] | None = None
    reply_to: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    conversation_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachment_ids: list[UUID] | None = None
    recipient_consent: bool
    consent_source: str | None = None
    consent_obtained_at: datetime | None = None
    message_type: Literal["marketing", "informational"] = "informational"

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


class EmailSummary(BaseModel):
    """Trimmed view for list endpoints — drops the message bodies.

    Fetch ``GET /emails/{id}`` to get the full ``EmailResponse`` with
    ``body_text`` / ``body_html`` populated.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    conversation_id: UUID | None = None
    email_domain_id: UUID | None = None
    direction: Literal["outbound", "inbound"] = "outbound"
    from_address: str
    from_name: str | None = None
    to_addresses: list[str]
    cc_addresses: list[str] | None = None
    bcc_addresses: list[str] | None = None
    reply_to: str | None = None
    subject: str
    status: EmailStatus
    end_reason: str | None = None
    provider_message_id: str | None = None
    requested_at: datetime
    sent_at: datetime | None = None
    failed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmailAttachmentResponse(BaseModel):
    """One inbound MIME attachment as exposed to API consumers.

    ``url`` is the stable Hail API endpoint that 302-redirects to a
    presigned S3 URL on access — see GET /emails/{id}/attachments/{aid}.
    Mirrors ``core/hailhq/core/schemas.py:EmailAttachmentResponse``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    content_id: str | None = None
    url: str


class EmailAttachmentUploadResponse(BaseModel):
    """Returned by ``client.email_attachments.create(...)``.

    Mirrors ``core/hailhq/core/schemas.py:EmailAttachmentUploadResponse``.
    The returned ``id`` is reusable across many
    ``emails.create(attachment_ids=...)`` calls until Hail garbage-collects
    it (24h if never referenced by a send).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    content_type: str
    size_bytes: int


class EmailResponse(EmailSummary):
    body_text: str | None = None
    body_html: str | None = None
    # Inbound-only metadata. Outbound rows leave these all null/empty.
    # Defaults match the outbound shape so existing serializations keep
    # working. Mirrors ``core/hailhq/core/schemas.py:EmailResponse``.
    message_id: str | None = None
    in_reply_to: str | None = None
    references_ids: list[str] | None = None
    spam_verdict: str | None = None
    virus_verdict: str | None = None
    dkim_verdict: str | None = None
    spf_verdict: str | None = None
    dmarc_verdict: str | None = None
    provider_received_at: datetime | None = None
    raw_url: str | None = None
    attachments: list[EmailAttachmentResponse] = []


class EmailListResponse(BaseModel):
    items: list[EmailSummary]
    next_cursor: str | None = None


# --------------------------------------------------------------------------- #
# Sender domain models — manage SES identities (hail-mail or custom).
# --------------------------------------------------------------------------- #


# 1-20 chars lowercase alphanumeric and hyphens, no leading/trailing hyphen.
LOCAL_PREFIX = re.compile(r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$")
DOMAIN_NAME = re.compile(
    r"^([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
)


EmailDomainKind = Literal["hail_mail", "custom"]
EmailDomainVerificationStatus = Literal["pending", "verified", "failed"]


class DnsRecord(BaseModel):
    """One DNS record the tenant must publish for a sending domain.

    Covers DKIM CNAMEs, MAIL FROM MX, and SPF TXT records.
    ``model_config`` allows extra keys so a future provider that returns
    additional metadata round-trips without breaking SDK consumers.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    value: str
    type: Literal["CNAME", "MX", "TXT"] = "CNAME"
    priority: int | None = None


# Back-compat alias.
DkimRecord = DnsRecord


class EmailDomainCreate(BaseModel):
    """Body for ``POST /email-domains``.

    For ``kind='hail_mail'``, ``domain`` is omitted; the server composes
    ``<local_prefix_user>+<local_prefix_org>@<HAIL_MAIL_BASE_DOMAIN>``.
    Prefixes are optional in the body — when omitted the server falls back
    to the operator's env-var defaults. For ``kind='custom'``, ``domain``
    is required and the prefix fields must be omitted.
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
    """Body for ``PATCH /email-domains/{id}``.

    Two clusters of mutable fields, mirroring the server contract:

    * Hail-mail addressing — ``local_prefix_user`` / ``local_prefix_org``
      (``kind='hail_mail'`` rows only; the server 422s otherwise).
    * Inbound action — ``inbound_enabled``, ``forward_to``,
      ``webhook_url`` (empty string clears it; the server returns the new
      plaintext secret once when set), ``forward_rate_per_hour``.
    """

    model_config = ConfigDict(extra="forbid")

    local_prefix_user: str | None = None
    local_prefix_org: str | None = None
    inbound_enabled: bool | None = None
    forward_to: list[str] | None = None
    webhook_url: str | None = None
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

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("at least one field must be set")
        return self


class EmailDomainResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    kind: EmailDomainKind
    domain: str
    local_prefix_user: str | None = None
    local_prefix_org: str | None = None
    verification_status: EmailDomainVerificationStatus
    dns_records: list[DnsRecord]
    mail_from_domain: str | None = None
    mail_from_status: str | None = None
    provider: str
    verified_at: datetime | None = None
    inbound_enabled: bool = False
    forward_to: list[str] | None = None
    webhook_url: str | None = None
    forward_rate_per_hour: int | None = None
    # Populated only by PATCH responses that minted or rotated a webhook
    # secret. The plaintext is returned once and never echoed on subsequent
    # GETs — the server stores only the encrypted form.
    webhook_secret: str | None = None
    created_at: datetime
    updated_at: datetime


class EmailDomainListResponse(BaseModel):
    items: list[EmailDomainResponse]
    next_cursor: str | None = None
    # The address a send with no ``from_`` goes out as. ``None`` when such
    # a send would be rejected: several verified identities (name one), or
    # none that can send yet. Whole-org answer, not per-page.
    default_from: str | None = None


class WhoamiResponse(BaseModel):
    """Who the API key belongs to — the answer ``client.whoami()`` gives.

    ``user_id``/``email``/``name`` are ``None`` when the caller is a
    shared operator key (``auth_kind == "shared"``), which carries no
    human identity.
    """

    auth_kind: Literal["apikey", "jwt", "shared"]
    organization_id: UUID
    user_id: UUID | None = None
    email: str | None = None
    name: str | None = None


# --------------------------------------------------------------------------- #
# Standing (per-organization) BYO provider config — ``/providers``.
# --------------------------------------------------------------------------- #

ProviderLayer = Literal["llm", "tts", "stt"]


class ProviderConfigEntry(BaseModel):
    """One saved provider row for the caller's organization.

    Keys are write-only: the API returns ``key_last4`` and ``key_set_at``
    and never the key itself. ``params`` is layer-shaped — ``base_url`` and
    ``model`` for ``llm``, ``voice_id``/``model`` for ``tts``, ``model``
    for ``stt``.
    """

    model_config = ConfigDict(from_attributes=True)

    layer: ProviderLayer
    provider: str
    key_last4: str | None
    key_set_at: str | None
    params: dict[str, Any]
    fallback_enabled: bool
    is_active: bool


class ProviderConfigListResponse(BaseModel):
    providers: list[ProviderConfigEntry]


class ProviderValidateResult(BaseModel):
    """Outcome of a live provider-key probe."""

    status: str
    message: str | None


__all__ = [
    "DOMAIN_NAME",
    "E164",
    "EMAIL_ADDR",
    "LOCAL_PREFIX",
    "TERMINAL_CALL_STATUSES",
    "TERMINAL_EMAIL_STATUSES",
    "CallCreate",
    "CallEventResponse",
    "CallListResponse",
    "CallResponse",
    "CallStatus",
    "DkimRecord",
    "DnsRecord",
    "EmailAttachmentResponse",
    "EmailAttachmentUploadResponse",
    "EmailCreate",
    "EmailDomainCreate",
    "EmailDomainKind",
    "EmailDomainListResponse",
    "EmailDomainPatch",
    "EmailDomainResponse",
    "EmailDomainVerificationStatus",
    "EmailListResponse",
    "EmailResponse",
    "EmailStatus",
    "EmailSummary",
    "EventStreamResponse",
    "LLMConfig",
    "NumberType",
    "PhoneNumberListResponse",
    "PhoneNumberResponse",
    "ProviderConfigEntry",
    "ProviderConfigListResponse",
    "ProviderLayer",
    "ProviderValidateResult",
    "SenderIdResponse",
    "SmsCreate",
    "SmsListResponse",
    "SmsResponse",
    "SmsStatus",
    "SuppressionListResponse",
    "SuppressionResponse",
    "VoiceConfig",
    "WhoamiResponse",
]
