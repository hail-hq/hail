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
    CallCreate,
    CallEventResponse,
    CallListResponse,
    CallResponse,
    CallStatus,
    EventStreamResponse,
    LLMConfig,
    NumberType,
    VoiceConfig,
)

__version__ = "0.1.0"

__all__ = [
    "Client",
    "__version__",
    # models
    "CallCreate",
    "CallEventResponse",
    "CallListResponse",
    "CallResponse",
    "CallStatus",
    "EventStreamResponse",
    "LLMConfig",
    "NumberType",
    "TERMINAL_CALL_STATUSES",
    "VoiceConfig",
    # helpers
    "SUPPORTED_RESOURCE_TYPES",
    "parse_resource_id",
    # errors
    "HailError",
    "HailAPIError",
    "HailAuthError",
    "HailClientError",
    "HailConfigError",
    "HailIdempotencyConflict",
    "HailMalformedResourceId",
    "HailNotFoundError",
    "HailServerError",
    "HailValidationError",
]
