"""Carrier verification plug-ins and their registry."""

from __future__ import annotations

from collections.abc import Callable

from hailhq.core.providers.verification.base import (
    Address,
    DocumentInput,
    DocumentOption,
    DocumentSlot,
    DraftResult,
    FieldSpec,
    Problem,
    ProviderStatus,
    Requirements,
    SubjectType,
    UnsupportedSubjectType,
    UploadedFile,
    VerificationProvider,
    VerificationProviderError,
)

__all__ = [
    "Address",
    "DocumentInput",
    "DocumentOption",
    "DocumentSlot",
    "DraftResult",
    "FieldSpec",
    "Problem",
    "ProviderStatus",
    "Requirements",
    "SubjectType",
    "UnsupportedSubjectType",
    "UploadedFile",
    "VerificationProvider",
    "VerificationProviderError",
    "default_verification_provider_name",
    "get_verification_provider",
    "register_verification_provider",
]

_FACTORIES: dict[str, Callable[[], VerificationProvider]] = {}


def register_verification_provider(
    name: str, factory: Callable[[], VerificationProvider]
) -> None:
    """Make a carrier plug-in available under ``name``. Adding a carrier is one
    plug-in module plus one call to this function."""
    _FACTORIES[name] = factory


def get_verification_provider(name: str) -> VerificationProvider | None:
    """The plug-in registered as ``name``, or None when unknown or not
    configured (a plug-in raises ValueError when its credentials are missing)."""
    factory = _FACTORIES.get(name)
    if factory is None:
        return None
    try:
        return factory()
    except ValueError:
        return None


def default_verification_provider_name() -> str | None:
    """The first configured plug-in. Used only when a request names no carrier;
    once several carriers exist, callers pass the carrier of the number."""
    return next((n for n in _FACTORIES if get_verification_provider(n)), None)


def _register_builtin() -> None:
    from hailhq.core.providers.verification.twilio import (
        TwilioVerificationProvider,
    )

    register_verification_provider("twilio", TwilioVerificationProvider)


_register_builtin()
