"""Hail Python SDK.

Public surface — everything you'd reasonably ``from hail import`` is here.
Internal modules (``_http``, ``_resource_id``, ``_errors``) are still
importable but their names start with ``_`` to mark them unstable.
"""

from hail._errors import (
    HailAPIError,
    HailAuthError,
    HailClientError,
    HailConfigError,
    HailError,
    HailIdempotencyConflict,
    HailMalformedResourceId,
    HailNotFoundError,
    HailServerError,
    HailValidationError,
)
from hail._resource_id import SUPPORTED_RESOURCE_TYPES, parse_resource_id
from hail.client import Client
from hail.models import (
    TERMINAL_CALL_STATUSES,
    TERMINAL_EMAIL_STATUSES,
    CallCreate,
    CallEventResponse,
    CallListResponse,
    CallResponse,
    CallStatus,
    DkimRecord,
    EmailAttachmentResponse,
    EmailCreate,
    EmailDomainCreate,
    EmailDomainKind,
    EmailDomainListResponse,
    EmailDomainPatch,
    EmailDomainResponse,
    EmailDomainVerificationStatus,
    EmailListResponse,
    EmailResponse,
    EmailStatus,
    EmailSummary,
    EventStreamResponse,
    LLMConfig,
    NumberType,
    PhoneNumberListResponse,
    PhoneNumberResponse,
    SenderIdResponse,
    SmsCreate,
    SmsListResponse,
    SmsResponse,
    SmsStatus,
    VoiceConfig,
)

__version__ = "0.8.0"

__all__ = [
    # helpers
    "SUPPORTED_RESOURCE_TYPES",
    "TERMINAL_CALL_STATUSES",
    "TERMINAL_EMAIL_STATUSES",
    # models
    "CallCreate",
    "CallEventResponse",
    "CallListResponse",
    "CallResponse",
    "CallStatus",
    "Client",
    "DkimRecord",
    "EmailAttachmentResponse",
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
    "HailAPIError",
    "HailAuthError",
    "HailClientError",
    "HailConfigError",
    # errors
    "HailError",
    "HailIdempotencyConflict",
    "HailMalformedResourceId",
    "HailNotFoundError",
    "HailServerError",
    "HailValidationError",
    "LLMConfig",
    "NumberType",
    "PhoneNumberListResponse",
    "PhoneNumberResponse",
    "SenderIdResponse",
    "SmsCreate",
    "SmsListResponse",
    "SmsResponse",
    "SmsStatus",
    "VoiceConfig",
    "__version__",
    "parse_resource_id",
]
