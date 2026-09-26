"""Tests for POST/GET /numbers — generic cross-channel number provisioning."""

from __future__ import annotations

import json
import uuid

import pytest
from hailhq.core import telephony_catalog


@pytest.fixture(autouse=True)
def pinned_catalog(tmp_path, monkeypatch):
    """Pin the telephony catalog to a fixed fixture so these tests exercise
    route logic, not whatever the committed costs/twilio.json currently
    says — a routine carrier-sync data PR must not break API CI."""
    data = {
        "version": 2,
        "license": "CC-BY-4.0",
        "numbers": [
            {
                "country_code": "US",
                "number_type": "local",
                "usd_per_month": "1.15",
                "voice": True,
                "sms": True,
                "mms": True,
            },
            {
                "country_code": "SE",
                "number_type": "mobile",
                "usd_per_month": "3.00",
                "voice": False,
                "sms": True,
                "mms": False,
            },
        ],
        "a2p_10dlc": [],
    }
    path = tmp_path / "telephony.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_PATH", str(path))
    telephony_catalog._load.cache_clear()
    yield
    telephony_catalog._load.cache_clear()


@pytest.fixture()
def buy_number(client, async_session, org_and_key, monkeypatch, voice_provider_mock):
    """Buy a Twilio-quoted US/local number through POST /numbers."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    from hailhq.core.models import NumberOffer
    from hailhq.core.number_offers import CarrierOffer

    purchase = AsyncMock(return_value="PN_test_acquired")
    monkeypatch.setattr("hailhq.api.number_orders.purchase_ordered_number", purchase)

    async def _buy(
        key=None,
        org=None,
        headers=None,
        e164="+14155550001",
        monthly=115,
        carrier_down=False,
        reuse_quote=False,
    ):
        org = org or org_and_key[0]
        key = key or org_and_key[2]
        offer = CarrierOffer(
            provider="twilio",
            e164=e164,
            country_code="US",
            number_type="local",
            capabilities=["voice", "sms"],
            monthly_cents=monthly,
            setup_cents=0,
            readiness="ready",
        )
        monkeypatch.setattr(
            "hailhq.api.number_orders.discover_offers",
            AsyncMock(return_value=([], ["twilio"]) if carrier_down else ([offer], [])),
        )
        if reuse_quote:
            quote = _buy.last_quote
        else:
            quote = NumberOffer(
                organization_id=org,
                offer=offer.model_dump(mode="json"),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
            async_session.add(quote)
            await async_session.commit()
            _buy.last_quote = quote
        return await client.post(
            "/numbers",
            json={"country_code": "US", "quote_id": str(quote.id)},
            headers={"Authorization": f"Bearer {key}", **(headers or {})},
        )

    _buy.purchase = purchase
    return _buy


async def test_acquire_number_requires_auth(client) -> None:
    resp = await client.post(
        "/numbers", json={"quote_id": str(uuid.uuid4()), "country_code": "US"}
    )
    assert resp.status_code == 401


async def test_acquire_without_quote_id_is_422(client, org_and_key) -> None:
    """The legacy no-quote purchase is gone: the body must carry a quote_id."""
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/numbers",
        json={"country_code": "US", "number_type": "local"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422, resp.text
    assert "quote_id" in resp.text


async def test_acquire_unknown_quote_is_404(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/numbers",
        json={"country_code": "US", "quote_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 404, resp.text


async def test_acquire_number_402_zero_balance(
    client, async_session, buy_number
) -> None:
    """A zero-balance org must not purchase a number: 402 before the carrier
    is ever reached."""
    from .conftest import insert_org_and_key

    org, _, plaintext = await insert_org_and_key(
        async_session, org_slug="broke-numbers", initial_credit_cents=0
    )
    resp = await buy_number(key=plaintext, org=org)
    assert resp.status_code == 402, resp.text
    assert "credits" in resp.json()["detail"].lower()
    buy_number.purchase.assert_not_awaited()


async def test_acquire_number_happy_path(buy_number) -> None:
    resp = await buy_number()
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["e164"] == "+14155550001"
    assert body["is_dedicated"] is True
    assert set(body["capabilities"]) == {"voice", "sms"}


async def test_acquire_number_idempotent_replay(buy_number) -> None:
    """Same Idempotency-Key on a retried acquire must NOT purchase a second
    number: the replay returns the cached number without calling the carrier."""
    headers = {"Idempotency-Key": "acquire-retry-key"}
    first = await buy_number(headers=headers)
    assert first.status_code == 201, first.text
    second = await buy_number(headers=headers, reuse_quote=True)
    assert second.status_code == 201, second.text
    assert second.headers.get("idempotency-replay") == "true"
    assert second.json()["id"] == first.json()["id"]
    buy_number.purchase.assert_awaited_once()


async def test_release_number_204_marks_released(
    client, org_and_key, voice_provider_mock, buy_number
) -> None:
    _, _, plaintext = org_and_key
    headers = {"Authorization": f"Bearer {plaintext}"}
    acquired = await buy_number()
    assert acquired.status_code == 201, acquired.text
    number_id = acquired.json()["id"]

    resp = await client.delete(f"/numbers/{number_id}", headers=headers)
    assert resp.status_code == 204, resp.text
    voice_provider_mock.release_number.assert_awaited_once_with("PN_test_acquired")

    got = await client.get(f"/numbers/{number_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["provisioning_state"] == "released"

    # Idempotent: a second DELETE is a no-op 204, no second carrier call.
    again = await client.delete(f"/numbers/{number_id}", headers=headers)
    assert again.status_code == 204
    voice_provider_mock.release_number.assert_awaited_once()


async def test_reacquire_same_e164_after_release(
    client, org_and_key, voice_provider_mock, buy_number
) -> None:
    """Twilio recycles released numbers: a released row is a tombstone and
    must not block re-acquiring the same e164 (partial unique index,
    migration 0041). Before, the INSERT hit the full UNIQUE after the
    carrier purchase — a 500 and an orphaned paid number."""
    _, _, plaintext = org_and_key
    headers = {"Authorization": f"Bearer {plaintext}"}

    first = await buy_number()
    assert first.status_code == 201, first.text
    resp = await client.delete(f"/numbers/{first.json()['id']}", headers=headers)
    assert resp.status_code == 204

    # A fresh quote for the same e164.
    second = await buy_number()
    assert second.status_code == 201, second.text
    assert second.json()["e164"] == first.json()["e164"]
    assert second.json()["id"] != first.json()["id"]


async def test_enable_sms_on_released_number_422(
    client, org_and_key, voice_provider_mock, buy_number
) -> None:
    """A released number's PN is deleted at Twilio — enable-sms must be a
    clean 422, not a TwilioRestException 500 from attach_number."""
    _, _, plaintext = org_and_key
    headers = {"Authorization": f"Bearer {plaintext}"}
    acquired = await buy_number()
    assert acquired.status_code == 201, acquired.text
    number_id = acquired.json()["id"]
    resp = await client.delete(f"/numbers/{number_id}", headers=headers)
    assert resp.status_code == 204

    enable = await client.post(f"/numbers/{number_id}/enable-sms", headers=headers)
    assert enable.status_code == 422, enable.text
    assert "released" in enable.json()["detail"][0]["msg"].lower()


async def test_release_number_404_other_org(
    client, async_session, org_and_key, voice_provider_mock, buy_number
) -> None:
    from .conftest import insert_org_and_key

    acquired = await buy_number()
    assert acquired.status_code == 201, acquired.text
    number_id = acquired.json()["id"]

    _, _, intruder_key = await insert_org_and_key(
        async_session, org_slug="other-org-release"
    )
    resp = await client.delete(
        f"/numbers/{number_id}", headers={"Authorization": f"Bearer {intruder_key}"}
    )
    assert resp.status_code == 404
    voice_provider_mock.release_number.assert_not_awaited()


async def test_get_number_not_found(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.get(
        f"/numbers/{uuid.uuid4()}", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 404


async def test_list_numbers_scoped_to_org(client, async_session, org_and_key) -> None:
    from hailhq.core.models import PhoneNumber

    org_id, _, plaintext = org_and_key
    async_session.add(
        PhoneNumber(
            organization_id=org_id,
            e164="+14155551111",
            country_code="US",
            number_type="local",
            provider_resource_id="PN_a",
            provisioning_state="active",
        )
    )
    await async_session.commit()

    resp = await client.get(
        "/numbers", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


async def test_enable_sms_rejects_number_without_sms_capability(
    client, async_session, org_and_key
) -> None:
    from hailhq.core.models import PhoneNumber

    org_id, _, plaintext = org_and_key
    pn = PhoneNumber(
        organization_id=org_id,
        e164="+14155552222",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_voice_only",
        provisioning_state="active",
        capabilities=["voice"],  # no sms
    )
    async_session.add(pn)
    await async_session.commit()

    resp = await client.post(
        f"/numbers/{pn.id}/enable-sms", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 422
    assert "does not support sms" in resp.json()["detail"][0]["msg"].lower()


async def test_enable_sms_creates_messaging_service_and_attaches(
    client, async_session, org_and_key, sms_mock
) -> None:
    from hailhq.core.models import PhoneNumber

    org_id, _, plaintext = org_and_key
    pn = PhoneNumber(
        organization_id=org_id,
        e164="+14155553333",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_sms_ok",
        provisioning_state="active",
        capabilities=["voice", "sms"],
    )
    async_session.add(pn)
    await async_session.commit()

    sms_mock.ensure_messaging_service.return_value = "MG_new_service"

    resp = await client.post(
        f"/numbers/{pn.id}/enable-sms", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["messaging_service_sid"] == "MG_new_service"
    sms_mock.attach_number.assert_awaited_once_with(
        messaging_service_sid="MG_new_service", provider_resource_id="PN_sms_ok"
    )


async def test_enable_sms_reuses_existing_org_messaging_service(
    client, async_session, org_and_key, sms_mock
) -> None:
    """A second number in the same org attaches to the org's EXISTING Messaging
    Service (one per org) rather than creating a second one."""
    from hailhq.core.models import PhoneNumber

    org_id, _, plaintext = org_and_key
    already_enabled = PhoneNumber(
        organization_id=org_id,
        e164="+14155554444",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_first",
        provisioning_state="active",
        capabilities=["voice", "sms"],
        messaging_service_sid="MG_org_shared",
    )
    second = PhoneNumber(
        organization_id=org_id,
        e164="+14155555555",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_second",
        provisioning_state="active",
        capabilities=["voice", "sms"],
    )
    async_session.add_all([already_enabled, second])
    await async_session.commit()

    sms_mock.ensure_messaging_service.return_value = "MG_org_shared"

    resp = await client.post(
        f"/numbers/{second.id}/enable-sms",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["messaging_service_sid"] == "MG_org_shared"
    # The org's existing service SID was passed through, not None → no new service.
    sms_mock.ensure_messaging_service.assert_awaited_once_with(
        organization_id=org_id, existing_sid="MG_org_shared"
    )
    sms_mock.attach_number.assert_awaited_once_with(
        messaging_service_sid="MG_org_shared", provider_resource_id="PN_second"
    )


async def test_enable_sms_is_idempotent_when_already_enabled(
    client, async_session, org_and_key, sms_mock
) -> None:
    """Re-enabling an already-enabled number is a no-op — no re-attach (which
    Twilio would reject)."""
    from hailhq.core.models import PhoneNumber

    org_id, _, plaintext = org_and_key
    pn = PhoneNumber(
        organization_id=org_id,
        e164="+14155556666",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_done",
        provisioning_state="active",
        capabilities=["voice", "sms"],
        messaging_service_sid="MG_done",
    )
    async_session.add(pn)
    await async_session.commit()

    resp = await client.post(
        f"/numbers/{pn.id}/enable-sms", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["messaging_service_sid"] == "MG_done"
    sms_mock.ensure_messaging_service.assert_not_awaited()
    sms_mock.attach_number.assert_not_awaited()


@pytest.mark.parametrize("credit, expected", [(114, 402), (115, 201), (1000, 201)])
async def test_number_purchase_debits_full_price(
    async_session, buy_number, credit, expected
):
    from hailhq.core.billing import get_balance_cents

    from .conftest import insert_org_and_key

    org, _, key = await insert_org_and_key(async_session, initial_credit_cents=credit)
    response = await buy_number(key=key, org=org)
    assert response.status_code == expected, response.text
    assert await get_balance_cents(async_session, org) == credit - (
        115 if expected == 201 else 0
    )
    if expected == 402:
        buy_number.purchase.assert_not_awaited()


async def test_purchase_failure_does_not_charge(org_and_key, async_session, buy_number):
    from hailhq.core.billing import get_balance_cents

    org, _, _ = org_and_key
    before = await get_balance_cents(async_session, org)
    response = await buy_number(carrier_down=True)
    assert response.status_code == 503
    assert await get_balance_cents(async_session, org) == before
    buy_number.purchase.assert_not_awaited()


async def test_purchase_debit_uses_monthly_rater_key_and_replay_does_not_charge_twice(
    org_and_key,
    async_session,
    buy_number,
):
    from hailhq.core.models import AccountCredit, PhoneNumber
    from sqlalchemy import select

    org, _, _ = org_and_key
    headers = {"Idempotency-Key": "debit-once"}
    first = await buy_number(headers=headers)
    assert first.status_code == 201
    second = await buy_number(headers=headers, reuse_quote=True)
    assert second.status_code == 201
    number = await async_session.get(PhoneNumber, uuid.UUID(first.json()["id"]))
    debits = (
        (
            await async_session.execute(
                select(AccountCredit).where(
                    AccountCredit.organization_id == org,
                    AccountCredit.source == "monthly_fee",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(debits) == 1
    assert debits[0].amount_cents == -115
    assert (
        debits[0].ref
        == f"monthly_fee:{org}:{number.id}:dedicated_number:{number.acquired_at:%Y-%m}"
    )


async def test_delete_failed_number_dismisses_without_a_carrier_call(
    client, async_session, org_and_key, add_phone_number, voice_provider_mock
) -> None:
    org, _, plaintext = org_and_key
    headers = {"Authorization": f"Bearer {plaintext}"}
    number = await add_phone_number(async_session, org, state="failed")
    resp = await client.delete(f"/numbers/{number.id}", headers=headers)
    assert resp.status_code == 204, resp.text
    voice_provider_mock.release_number.assert_not_awaited()
    await async_session.refresh(number)
    assert number.released_at is not None
    got = await client.get(f"/numbers/{number.id}", headers=headers)
    assert got.status_code == 200
    # Idempotent, same as a normal release.
    again = await client.delete(f"/numbers/{number.id}", headers=headers)
    assert again.status_code == 204


async def test_list_numbers_hides_dismissed_failed_but_keeps_released(
    client, async_session, org_and_key, add_phone_number, voice_provider_mock
) -> None:
    org, _, plaintext = org_and_key
    headers = {"Authorization": f"Bearer {plaintext}"}
    failed = await add_phone_number(
        async_session, org, e164="+14155550001", state="failed"
    )
    released = await add_phone_number(
        async_session, org, e164="+14155550002", state="released"
    )
    listed = await client.get("/numbers", headers=headers)
    assert {i["id"] for i in listed.json()["items"]} == {
        str(failed.id),
        str(released.id),
    }
    # Dismiss the failed row: it disappears; the released tombstone stays.
    assert (
        await client.delete(f"/numbers/{failed.id}", headers=headers)
    ).status_code == 204
    listed = await client.get("/numbers", headers=headers)
    assert [i["id"] for i in listed.json()["items"]] == [str(released.id)]
