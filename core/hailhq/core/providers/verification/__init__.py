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
    SmsRegistrationProvider,
    SubjectType,
    UnsupportedSubjectType,
    UploadedFile,
    VerificationProvider,
    VerificationProviderError,
    VerificationRequirements,
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
    "SmsRegistrationProvider",
    "SubjectType",
    "UnsupportedSubjectType",
    "UploadedFile",
    "VerificationProvider",
    "VerificationProviderError",
    "VerificationRequirements",
    "default_verification_provider_name",
    "get_verification_provider",
    "register_verification_provider",
]

_FACTORIES: dict[tuple[str, str], Callable[[], VerificationProvider]] = {}
# One instance per plug-in, built on first use (a plug-in holds an HTTP client).
_INSTANCES: dict[tuple[str, str], VerificationProvider] = {}


def register_verification_provider(
    name: str, factory: Callable[[], VerificationProvider], purpose: str = "number"
) -> None:
    """Make a carrier plug-in available under ``name`` for a ``purpose``
    ('number' or 'sms'). Adding a carrier is one plug-in module plus one call
    to this function."""
    _FACTORIES[(name, purpose)] = factory
    _INSTANCES.pop((name, purpose), None)


def get_verification_provider(
    name: str, purpose: str = "number"
) -> VerificationProvider | None:
    """The plug-in registered as ``name`` for ``purpose``, or None when unknown
    or not configured (a plug-in raises ValueError when its credentials are
    missing)."""
    key = (name, purpose)
    instance = _INSTANCES.get(key)
    if instance is not None:
        return instance
    factory = _FACTORIES.get(key)
    if factory is None:
        return None
    try:
        instance = factory()
    except ValueError:
        return None
    _INSTANCES[key] = instance
    return instance


def default_verification_provider_name(purpose: str = "number") -> str | None:
    """The first configured plug-in for ``purpose``. Used only when a request
    names no carrier; once several carriers exist, callers pass the carrier of
    the number."""
    return next(
        (
            n
            for (n, p) in _FACTORIES
            if p == purpose and get_verification_provider(n, p)
        ),
        None,
    )


def _register_builtin() -> None:
    from hailhq.core.providers.verification.didww import DidwwVerificationProvider
    from hailhq.core.providers.verification.twilio import (
        TwilioVerificationProvider,
    )

    register_verification_provider("twilio", TwilioVerificationProvider)
    # After Twilio on purpose: the default (first configured) stays Twilio.
    from hailhq.core.providers.verification.telnyx import (
        TelnyxVerificationProvider,
    )

    register_verification_provider("telnyx", TelnyxVerificationProvider)
    register_verification_provider("didww", DidwwVerificationProvider)
    from hailhq.core.providers.verification.twilio_sms import (
        TwilioSmsRegistrationProvider,
    )

    register_verification_provider(
        "twilio", TwilioSmsRegistrationProvider, purpose="sms"
    )
    from hailhq.core.providers.verification.telnyx_sms import (
        TelnyxSmsRegistrationProvider,
    )

    register_verification_provider(
        "telnyx", TelnyxSmsRegistrationProvider, purpose="sms"
    )


_register_builtin()
