"""DIDWW carrier: offers, orders, registration outcome, release.

All HTTP goes through the ``didww`` SDK's low-level client with plain
JSON:API dicts, so tests mock at the ``requests`` boundary (``responses``)
and SDK drift shows up as test failures. The SDK is sync; every public
function here is ``async`` and runs its call in ``asyncio.to_thread``.
"""

from __future__ import annotations

from didww.client import DidwwClient
from didww.configuration import Environment
from didww.exceptions import DidwwApiError
from hailhq.core.config import settings
from hailhq.core.providers.voice.base import CarrierNotConfigured

PROVIDER = "didww"

_ENVIRONMENTS = {
    "production": Environment.PRODUCTION,
    "sandbox": Environment.SANDBOX,
}


def didww_client() -> DidwwClient:
    """A client for the configured environment. ``CarrierNotConfigured``
    when the key is missing or the environment name is unknown."""
    if not settings.didww_api_key:
        raise CarrierNotConfigured("DIDWW is not configured (DIDWW_API_KEY)")
    env = _ENVIRONMENTS.get(settings.didww_environment)
    if env is None:
        raise CarrierNotConfigured(
            "DIDWW_ENVIRONMENT must be 'production' or 'sandbox'"
        )
    return DidwwClient(api_key=settings.didww_api_key, environment=env)


def carrier_status(exc: DidwwApiError) -> int:
    """HTTP status of a DIDWW error; 502 when the SDK did not record one."""
    return exc.status_code or 502
