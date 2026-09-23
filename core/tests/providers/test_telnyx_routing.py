import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hailhq.core.carrier_routing import voice_route
from hailhq.core.config import settings
from hailhq.core.number_offers import (
    CarrierOffer,
    rank_offers,
    telnyx_offers,
    twilio_offers,
)
from hailhq.core.providers.sms.telnyx import TelnyxSmsProvider
from hailhq.core.providers.telnyx import TelnyxClient, verify_webhook


def test_signature_rejects_tampering_and_replay():
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes_raw()).decode()
    raw, timestamp = b'{"data":{"id":"1"}}', "1000"
    sig = base64.b64encode(private.sign(timestamp.encode() + b"|" + raw)).decode()
    assert verify_webhook(raw, sig, timestamp, public, now=1000)
    assert not verify_webhook(raw + b" ", sig, timestamp, public, now=1000)
    assert not verify_webhook(raw, sig, timestamp, public, now=1301)
    assert not verify_webhook(raw, sig, timestamp, public, now=699)
    assert not verify_webhook(raw, "bad", timestamp, public, now=1000)
    assert not verify_webhook(raw, sig, "bad", public, now=1000)


def test_voice_route_never_uses_twilio_trunk_for_telnyx(monkeypatch):
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_twilio")
    monkeypatch.setattr(settings, "livekit_telnyx_sip_outbound_trunk_id", "ST_telnyx")
    monkeypatch.setattr(settings, "telnyx_sip_username", "hail-sip")
    assert voice_route("telnyx") == ("ST_telnyx", {"X-Telnyx-Username": "hail-sip"})
    assert voice_route("twilio") == ("ST_twilio", {})
    monkeypatch.setattr(settings, "telnyx_sip_username", "")
    with pytest.raises(ValueError):
        voice_route("telnyx")
    with pytest.raises(ValueError):
        voice_route("unknown")


def offer(provider="telnyx", monthly=100, setup=0, readiness="ready"):
    return CarrierOffer(
        provider=provider,
        e164="+351211234567",
        country_code="PT",
        number_type="local",
        capabilities=["voice"],
        monthly_cents=monthly,
        setup_cents=setup,
        readiness=readiness,
    )


def test_ranking_and_override_never_prefer_blocked_cheapest():
    blocked = offer(monthly=10, readiness="verification_required")
    cheap = offer(monthly=100)
    expensive = offer(provider="twilio", monthly=150)
    setup = offer(monthly=100, setup=50)
    assert rank_offers([blocked, expensive, setup, cheap]) == [
        cheap,
        setup,
        expensive,
        blocked,
    ]
    assert rank_offers([blocked, expensive, cheap], "twilio") == [expensive]


async def test_sms_uses_messaging_api_and_no_mutation_retry():
    seen = []

    def respond(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer secret"
        body = json.loads(request.content)
        assert body == {
            "from": "+351211234567",
            "to": "+46701234567",
            "text": "hello",
            "type": "SMS",
            "webhook_url": "https://api.hail.so/sms/telnyx",
        }
        return httpx.Response(
            200,
            json={
                "data": {"id": "message-id", "to": [{"status": "queued"}], "parts": 2}
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        provider = TelnyxSmsProvider(TelnyxClient("secret", http))
        result = await provider.send_sms(
            "+351211234567", "+46701234567", "hello", "https://api.hail.so/sms/telnyx"
        )
    assert result.segment_count == 2
    assert result.provider_message_sid == "message-id"
    assert seen[0].url.path == "/v2/messages"
    assert len(seen) == 1


@pytest.mark.parametrize(
    "group_owner,group_status,expected",
    [
        ("own", "approved", "ready"),
        ("other", "approved", "verification_required"),
        ("own", "pending-approval", "verification_required"),
    ],
)
@pytest.mark.parametrize(
    "field_type,friction",
    [("textual", "information"), ("document", "documents"), ("new_type", "unknown")],
)
async def test_live_portugal_rules_and_org_group(
    monkeypatch, group_owner, group_status, expected, field_type, friction
):
    org = uuid4()
    monkeypatch.setattr(settings, "telnyx_api_key", "secret")
    monkeypatch.setattr(settings, "telnyx_connection_id", "connection")
    monkeypatch.setattr(settings, "telnyx_sip_username", "sip-user")
    monkeypatch.setattr(settings, "livekit_telnyx_sip_outbound_trunk_id", "ST_telnyx")

    def respond(request):
        if request.url.path.endswith("available_phone_numbers"):
            data = [
                {
                    "phone_number": "+351211234567",
                    "features": [{"name": "voice"}, {"name": "emergency"}],
                    "cost_information": {
                        "currency": "USD",
                        "monthly_cost": "2.30",
                        "upfront_cost": "1.00",
                    },
                }
            ]
        elif request.url.path.endswith("requirement_groups"):
            data = [
                {
                    "id": str(uuid4()),
                    "country_code": "PT",
                    "phone_number_type": "local",
                    "action": "ordering",
                    "status": group_status,
                    "customer_reference": (
                        f"hail-{org}" if group_owner == "own" else "hail-someone-else"
                    ),
                }
            ]
        else:
            assert request.url.params["filter[phone_number]"] == "+351211234567"
            data = [
                {
                    "country_code": "PT",
                    "phone_number_type": "local",
                    "action": "ordering",
                    "regulatory_requirements": [
                        {"name": "Local business address", "field_type": field_type}
                    ],
                }
            ]
        return httpx.Response(200, json={"data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await telnyx_offers(org, "PT", "local", ["voice"], http)
    assert len(result) == 1
    assert result[0].regulatory_friction == (
        "none" if expected == "ready" else friction
    )
    assert result[0].readiness == expected
    assert result[0].monthly_cents == 230
    assert result[0].setup_cents == 100


@pytest.mark.parametrize(
    "requirements,friction",
    [
        ({"end_user": [], "supporting_document": []}, "none"),
        (
            {"end_user": [{"name": "Business"}], "supporting_document": []},
            "information",
        ),
        (
            {"end_user": [], "supporting_document": [{"name": "Proof of address"}]},
            "documents",
        ),
    ],
)
async def test_twilio_live_rules_classify_verification_effort(
    monkeypatch, requirements, friction
):

    api = MagicMock()
    api.available_phone_numbers.return_value.local.list.return_value = [
        SimpleNamespace(
            phone_number="+12125550100",
            capabilities={"voice": True, "SMS": True},
            address_requirements="none",
        )
    ]
    api.pricing.v1.phone_numbers.countries.return_value.fetch.return_value = (
        SimpleNamespace(
            price_unit="USD",
            phone_number_prices=[{"number_type": "local", "current_price": "1.15"}],
        )
    )
    api.numbers.v2.regulatory_compliance.regulations.list.return_value = [
        SimpleNamespace(sid="RN_us", requirements=requirements)
    ]
    monkeypatch.setattr(settings, "twilio_account_sid", "AC_test")
    monkeypatch.setattr(settings, "twilio_auth_token", "test")
    monkeypatch.setattr(
        "hailhq.core.providers.voice.twilio.TwilioClient", lambda *a, **kw: api
    )
    offers = await twilio_offers(uuid4(), "US", "local", ["voice"], e164="+12125550100")
    assert offers[0].regulatory_friction == friction
    assert offers[0].readiness == (
        "ready" if friction == "none" else "verification_required"
    )
    assert offers[0].monthly_cents == 115
    if friction == "none":
        api.numbers.v2.regulatory_compliance.bundles.list.assert_not_called()
    assert (
        api.available_phone_numbers.return_value.local.list.call_args.kwargs["contains"]
        == "+12125550100"
    )


def test_equal_ready_prices_use_twilio_but_cheaper_telnyx_wins():
    twilio = offer(provider="twilio", monthly=100)
    telnyx = offer(monthly=100)
    assert rank_offers([telnyx, twilio])[0] == twilio
    telnyx.monthly_cents = 90
    assert rank_offers([twilio, telnyx])[0] == telnyx


def test_info_only_verification_precedes_cheaper_document_uploads():
    info = offer(provider="twilio", monthly=200, readiness="verification_required")
    info.regulatory_friction = "information"
    docs = offer(monthly=50, readiness="verification_required")
    docs.regulatory_friction = "documents"
    unknown = offer(monthly=10, readiness="verification_required")
    assert rank_offers([unknown, docs, info]) == [info, docs, unknown]
    docs.readiness = (
        "ready"  # This organization's approved group removes remaining work.
    )
    assert rank_offers([info, docs])[0] == docs
