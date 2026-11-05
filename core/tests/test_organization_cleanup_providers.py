"""Full organization cleanup must retain references when providers fail."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from didww.exceptions import DidwwApiError
from hailhq.core.providers.verification.didww import DidwwVerificationProvider
from hailhq.core.providers.verification.telnyx import TelnyxVerificationProvider
from hailhq.core.providers.verification.telnyx_sms import TelnyxSmsRegistrationProvider
from hailhq.core.providers.verification.twilio import TwilioVerificationProvider
from hailhq.core.providers.verification.twilio_sms import TwilioSmsRegistrationProvider
from twilio.base.exceptions import TwilioRestException


def make_provider(kind, status):
    if kind == "twilio-number":
        p = TwilioVerificationProvider.__new__(TwilioVerificationProvider)
        p._client = MagicMock()
        p._rc.bundles.return_value.delete.side_effect = TwilioRestException(
            status, "https://example.com", "failed"
        )
        return p, {"bundle_sid": "BU1"}
    if kind == "twilio-sms":
        p = TwilioSmsRegistrationProvider.__new__(TwilioSmsRegistrationProvider)
        p._client = MagicMock()
        p._client.messaging.v1.services.return_value.us_app_to_person.return_value.delete.side_effect = TwilioRestException(
            status, "https://example.com", "failed"
        )
        return p, {"campaign_sid": "C1", "messaging_service_sid": "MG1"}
    if kind == "didww":
        p = DidwwVerificationProvider.__new__(DidwwVerificationProvider)
        p._client = SimpleNamespace(
            delete=MagicMock(side_effect=DidwwApiError([], status_code=status))
        )
        return p, {"address_id": "A1"}
    cls = (
        TelnyxVerificationProvider
        if kind == "telnyx-number"
        else TelnyxSmsRegistrationProvider
    )
    p = cls.__new__(cls)
    request = httpx.Request("DELETE", "https://example.com/resource")
    error = httpx.HTTPStatusError(
        "failed", request=request, response=httpx.Response(status, request=request)
    )
    p._api = SimpleNamespace(request=AsyncMock(side_effect=error))
    return p, {"group_id": "G1"} if kind == "telnyx-number" else {"campaign_id": "C1"}


@pytest.mark.parametrize(
    "kind", ["twilio-number", "twilio-sms", "telnyx-number", "telnyx-sms", "didww"]
)
async def test_strict_discard_propagates_failure(kind):
    provider, refs = make_provider(kind, 503)
    with pytest.raises(Exception):
        await provider.discard(refs, strict=True)


@pytest.mark.parametrize(
    "kind", ["twilio-number", "twilio-sms", "telnyx-number", "telnyx-sms", "didww"]
)
async def test_strict_discard_accepts_already_missing(kind):
    provider, refs = make_provider(kind, 404)
    await provider.discard(refs, strict=True)
