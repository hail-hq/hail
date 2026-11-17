"""UK (GB) numbers: local, mobile and toll-free from Twilio only; Telnyx GB
national for companies only (telephony_catalog._NOT_SOLD, _BUSINESS_ONLY).
Telnyx declines private individuals for UK numbers. Numbers already bought
on Telnyx keep working."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from hailhq.api.main import app
from hailhq.api.routes.verifications import get_verification_registry
from hailhq.core import telephony_catalog
from hailhq.core.billing import get_balance_cents
from hailhq.core.carrier_offer import CarrierOffer
from hailhq.core.config import settings
from hailhq.core.models import NumberOffer
from hailhq.core.providers.telnyx import TelnyxClient
from hailhq.core.providers.verification.telnyx import TelnyxVerificationProvider

VOICE = "hailhq.core.providers.voice"


@pytest.fixture(autouse=True)
def _committed_catalog():
    """These tests read the committed costs/*.json GB rows."""
    telephony_catalog._load.cache_clear()
    yield
    telephony_catalog._load.cache_clear()


def _offer(provider, kind, e164, monthly=100, setup=100, readiness="ready", **kw):
    return CarrierOffer(
        provider=provider,
        e164=e164,
        country_code="GB",
        number_type=kind,
        capabilities=["voice"],
        monthly_cents=monthly,
        setup_cents=setup,
        readiness=readiness,
        **kw,
    )


async def _quote(db, org, offer):
    row = NumberOffer(
        organization_id=org,
        offer=offer.model_dump(mode="json"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(row)
    await db.commit()
    return row


@pytest.mark.parametrize("kind", ["local", "mobile", "toll_free"])
async def test_gb_quote_offers_twilio_not_telnyx(
    client, org_and_key, monkeypatch, kind
):
    _, _, key = org_and_key
    twilio = _offer("twilio", kind, "+442071234567", monthly=115, setup=0)
    calls = {
        "twilio": AsyncMock(return_value=[twilio]),
        "telnyx": AsyncMock(
            return_value=[_offer("telnyx", kind, "+442071234568", monthly=100)]
        ),
        "didww": AsyncMock(return_value=[]),
    }
    for name, mock in calls.items():
        monkeypatch.setattr(f"{VOICE}.{name}.{name}_offers", mock)
    resp = await client.post(
        "/numbers/quotes",
        json={"country_code": "GB", "number_type": kind, "capabilities": ["voice"]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    bought = [o for o in resp.json()["offers"] if not o["by_request"]]
    assert [o["e164"] for o in bought] == ["+442071234567"]
    assert bought[0]["monthly_cents"] == 115
    calls["twilio"].assert_awaited_once()
    calls["telnyx"].assert_not_awaited()


async def test_gb_national_quote_is_for_companies_only(
    client, org_and_key, monkeypatch
):
    _, _, key = org_and_key
    national = _offer(
        "telnyx",
        "national",
        "+443301234567",
        readiness="verification_required",
        subject_types=["business"],
    )
    for name in ("twilio", "didww"):
        monkeypatch.setattr(f"{VOICE}.{name}.{name}_offers", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        f"{VOICE}.telnyx.telnyx_offers", AsyncMock(return_value=[national])
    )
    headers = {"Authorization": f"Bearer {key}"}
    body = {"country_code": "GB", "number_type": "national", "capabilities": ["voice"]}
    resp = await client.post("/numbers/quotes", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    (offer,) = [o for o in resp.json()["offers"] if not o["by_request"]]
    assert offer["subject_types"] == ["business"]
    person = await client.post(
        "/numbers/quotes", json={**body, "subject_type": "person"}, headers=headers
    )
    assert [o for o in person.json()["offers"] if not o["by_request"]] == []


async def test_stale_telnyx_gb_local_quote_cannot_be_bought(
    client, async_session, org_and_key, monkeypatch
):
    # A quote stored before the rule: the purchase gate refuses it before
    # any charge or carrier call.
    org, _, key = org_and_key
    row = await _quote(async_session, org, _offer("telnyx", "local", "+442071234568"))
    discover = AsyncMock()
    monkeypatch.setattr("hailhq.api.number_orders.discover_offers", discover)
    resp = await client.post(
        "/numbers",
        json={"country_code": "GB", "quote_id": str(row.id)},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422, resp.text
    discover.assert_not_awaited()
    assert await get_balance_cents(async_session, org) == 100000


def _telnyx_registry(rules):
    payload = {
        "data": [
            {
                "country_code": "GB",
                "phone_number_type": "national",
                "action": "ordering",
                "regulatory_requirements": rules,
            }
        ]
    }
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    )
    provider = TelnyxVerificationProvider("secret", http)
    return (
        lambda name, purpose="number": (
            provider if name == "telnyx" and purpose == "number" else None
        )
    ), http


@pytest.fixture()
async def telnyx_registry():
    registry, http = _telnyx_registry(
        [
            {
                "id": "r-name",
                "name": "Contact name",
                "field_type": "textual",
                "acceptance_criteria": {},
            }
        ]
    )
    app.dependency_overrides[get_verification_registry] = lambda: registry
    yield
    app.dependency_overrides.pop(get_verification_registry, None)
    await http.aclose()


async def test_gb_national_person_verification_is_refused(
    client, async_session, org_and_key, telnyx_registry
):
    org, _, key = org_and_key
    row = await _quote(async_session, org, _offer("telnyx", "national", None))
    params = {
        "country_code": "GB",
        "number_type": "national",
        "quote_id": str(row.id),
    }
    headers = {"Authorization": f"Bearer {key}"}
    person = await client.get(
        "/verifications/requirements",
        params={**params, "subject_type": "person"},
        headers=headers,
    )
    assert person.status_code == 422, person.text
    detail = person.json()["detail"][0]
    assert detail["loc"] == ["query", "subject_type"]
    assert detail["ctx"] == {"subject_types": ["business"]}
    business = await client.get(
        "/verifications/requirements",
        params={**params, "subject_type": "business"},
        headers=headers,
    )
    assert business.status_code == 200, business.text
    assert business.json()["subject_types"] == ["business"]


async def test_stale_telnyx_gb_local_quote_cannot_start_a_verification(
    client, async_session, org_and_key, telnyx_registry
):
    org, _, key = org_and_key
    row = await _quote(async_session, org, _offer("telnyx", "local", "+442071234568"))
    resp = await client.get(
        "/verifications/requirements",
        params={
            "country_code": "GB",
            "number_type": "local",
            "quote_id": str(row.id),
            "subject_type": "person",
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 409, resp.text


async def _owned_telnyx_gb(db, org, add_phone_number, kind="local"):
    number = await add_phone_number(
        db, org, e164="+442071234569", provider="telnyx", provider_resource_id="1"
    )
    number.country_code, number.number_type = "GB", kind
    await db.commit()
    return number


async def test_owned_telnyx_gb_number_still_calls_through_telnyx(
    client, async_session, org_and_key, add_phone_number, livekit_mock, monkeypatch
):
    org, _, key = org_and_key
    number = await _owned_telnyx_gb(async_session, org, add_phone_number)
    monkeypatch.setattr(settings, "livekit_telnyx_sip_outbound_trunk_id", "ST_telnyx")
    monkeypatch.setattr(settings, "telnyx_sip_username", "hail-sip")
    resp = await client.post(
        "/calls",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "to": "+442071230000",
            "from": number.e164,
            "system_prompt": "Hello",
            "recipient_consent": True,
        },
    )
    assert resp.status_code == 201, resp.text
    args = livekit_mock.create_sip_participant.await_args.kwargs
    assert args["sip_trunk_id"] == "ST_telnyx"


async def test_owned_telnyx_gb_number_is_released_at_telnyx(
    client, async_session, org_and_key, add_phone_number, monkeypatch
):
    org, _, key = org_and_key
    number = await _owned_telnyx_gb(async_session, org, add_phone_number)
    monkeypatch.setattr(settings, "telnyx_api_key", "test")
    release = AsyncMock()
    monkeypatch.setattr(TelnyxClient, "release_number", release)
    resp = await client.delete(
        f"/numbers/{number.id}", headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 204, resp.text
    release.assert_awaited_once_with("1")
