"""Trial texts from POST /v1/sms: a workspace with no approved US
registration sends to its verified trial phone from Hail's trial number."""

from __future__ import annotations

import uuid

import pytest
from hailhq.core.config import settings
from hailhq.core.models import CarrierVerification, PhoneNumber, Sms, UsageEvent
from hailhq.core.sms_registration_gate import BLOCK_MESSAGE
from hailhq.core.sms_trial import (
    SMS_TRIAL_META_KEY,
    TRIAL_HINT,
    TrialBodyTooLong,
    trial_texts_used,
)
from sqlalchemy import func, select, update

from .conftest import (
    approve_us_sms,
    insert_org_and_key,
    seed_sms_trial,
    seed_trial_texts,
    verify_trial_phone,
)

TRIAL_E164 = "+18005550199"
PHONE = "+14155550142"
OTHER_PHONE = "+14155550143"
OWN_NUMBER = "+14155550144"
WRAPPED_HI = "[Hail trial] hi\nHail does not read replies."


def _auth(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}


def _payload(to: str = PHONE, **extra) -> dict:
    return {"to": to, "body": "hi", "recipient_consent": True, **extra}


def _msg(resp) -> str:
    return resp.json()["detail"][0]["msg"]


async def _sms_count(session, org_id) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Sms).where(Sms.organization_id == org_id)
        )
    ).scalar_one()


async def _own_number(session, org_id, e164: str = OWN_NUMBER) -> None:
    session.add(
        PhoneNumber(
            organization_id=org_id,
            e164=e164,
            country_code="US",
            number_type="local",
            capabilities=["sms"],
            provider="twilio",
            provider_resource_id=f"PN_{e164}",
            provisioning_state="active",
        )
    )
    await session.commit()


def _trial_sends(sms_mock) -> list:
    return [
        c
        for c in sms_mock.send_sms.call_args_list
        if c.kwargs["from_e164"] == TRIAL_E164
    ]


@pytest.mark.parametrize(
    "case,hint",
    [
        ("not_verified_phone", True),
        ("pending_phone", True),
        ("other_workspace_phone", True),
        ("cap_used_25", True),
        ("explicit_own_unregistered_from", True),
        ("feature_off", False),
    ],
)
async def test_trial_send_refused(
    case, hint, client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    """Each case is the passing setup with one condition changed."""
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    payload = _payload()
    if case == "not_verified_phone":
        await verify_trial_phone(async_session, org_id)
        payload = _payload(to=OTHER_PHONE)
    elif case == "pending_phone":
        await verify_trial_phone(async_session, org_id, status="pending")
    elif case == "other_workspace_phone":
        other_id, _, _ = await insert_org_and_key(
            async_session, org_name="Other", org_slug="other"
        )
        await verify_trial_phone(async_session, other_id, PHONE)
        await verify_trial_phone(async_session, org_id, OTHER_PHONE)
    elif case == "cap_used_25":
        await verify_trial_phone(async_session, org_id)
        await seed_trial_texts(async_session, org_id, 25)
    elif case == "explicit_own_unregistered_from":
        await verify_trial_phone(async_session, org_id)
        await _own_number(async_session, org_id)
        payload = _payload(**{"from": OWN_NUMBER})
    elif case == "feature_off":
        await verify_trial_phone(async_session, org_id)
        monkeypatch.setattr(settings, "hail_sms_trial_sending", False)
    before = await _sms_count(async_session, org_id)

    resp = await client.post("/sms", json=payload, headers=_auth(plaintext))

    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "from"]
    assert _msg(resp).endswith(" " + TRIAL_HINT) is hint
    assert _trial_sends(sms_mock) == []
    assert await _sms_count(async_session, org_id) == before


async def test_explicit_own_from_422_names_the_trial(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    await _own_number(async_session, org_id)

    resp = await client.post(
        "/sms", json=_payload(**{"from": OWN_NUMBER}), headers=_auth(plaintext)
    )

    assert resp.status_code == 422
    assert _msg(resp) == BLOCK_MESSAGE + " " + TRIAL_HINT
    sms_mock.send_sms.assert_not_awaited()


async def test_trial_send_with_no_number(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)

    resp = await client.post(
        "/sms",
        json=_payload(metadata={"run": "a"}),
        headers=_auth(plaintext),
    )

    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["from_e164"] == TRIAL_E164
    assert out["body"] == WRAPPED_HI
    sent = sms_mock.send_sms.call_args.kwargs
    assert sent["body"] == WRAPPED_HI
    assert sent["from_e164"] == TRIAL_E164
    assert sent["to_e164"] == PHONE
    row = await async_session.get(Sms, uuid.UUID(out["id"]))
    assert row.body == WRAPPED_HI
    assert row.from_e164 == TRIAL_E164
    assert row.metadata_ == {"run": "a", SMS_TRIAL_META_KEY: True}
    usage = (
        await async_session.execute(
            select(UsageEvent).where(UsageEvent.channel == "sms")
        )
    ).scalar_one()
    assert usage.organization_id == org_id
    assert usage.ref == f"sms:{out['id']}:us"


async def test_from_equal_to_trial_number_uses_trial(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)

    resp = await client.post(
        "/sms", json=_payload(**{"from": TRIAL_E164}), headers=_auth(plaintext)
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["from_e164"] == TRIAL_E164
    assert sms_mock.send_sms.call_args.kwargs["body"] == WRAPPED_HI
    assert await trial_texts_used(async_session, org_id) == 1


async def test_registered_number_preferred(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    await _own_number(async_session, org_id)
    await approve_us_sms(async_session, org_id)

    resp = await client.post("/sms", json=_payload(), headers=_auth(plaintext))

    assert resp.status_code == 201, resp.text
    assert resp.json()["from_e164"] == OWN_NUMBER
    sent = sms_mock.send_sms.call_args.kwargs
    assert sent["from_e164"] == OWN_NUMBER
    assert sent["body"] == "hi"
    assert await trial_texts_used(async_session, org_id) == 0


async def test_hail_own_workspace_sends_normally(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    """The trial number's owner sends from it as its own number, with no
    trial wrapping, even to a verified trial phone of its own."""
    org_id, _, plaintext = org_and_key
    number = await seed_sms_trial(async_session, monkeypatch)
    number_id = number.id
    await async_session.execute(
        update(PhoneNumber)
        .where(PhoneNumber.id == number_id)
        .values(organization_id=org_id)
    )
    await async_session.execute(
        update(CarrierVerification)
        .where(CarrierVerification.number_id == number_id)
        .values(organization_id=org_id)
    )
    await async_session.commit()
    await verify_trial_phone(async_session, org_id)

    resp = await client.post(
        "/sms", json=_payload(**{"from": TRIAL_E164}), headers=_auth(plaintext)
    )

    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["body"] == "hi"
    assert sms_mock.send_sms.call_args.kwargs["body"] == "hi"
    row = await async_session.get(Sms, uuid.UUID(out["id"]))
    assert SMS_TRIAL_META_KEY not in row.metadata_


async def test_trial_body_too_long_is_422(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)

    resp = await client.post(
        "/sms",
        json={**_payload(), "body": "x" * 1600},
        headers=_auth(plaintext),
    )

    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "body"]
    assert _msg(resp) == str(TrialBodyTooLong(1600))
    sms_mock.send_sms.assert_not_awaited()
    assert await _sms_count(async_session, org_id) == 0

    # The refusal rolled back and released the workspace lock: the next
    # send goes through.
    resp = await client.post("/sms", json=_payload(), headers=_auth(plaintext))
    assert resp.status_code == 201, resp.text


async def test_metadata_cannot_plant_marker(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    await _own_number(async_session, org_id)
    await approve_us_sms(async_session, org_id)

    resp = await client.post(
        "/sms",
        json=_payload(metadata={SMS_TRIAL_META_KEY: True}),
        headers=_auth(plaintext),
    )

    assert resp.status_code == 422
    assert "metadata keys are reserved: sms_trial" in _msg(resp)
    sms_mock.send_sms.assert_not_awaited()
    assert await _sms_count(async_session, org_id) == 0


async def test_26th_trial_text_refused(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    """The route's own text carries the marker and counts toward the cap."""
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    await seed_trial_texts(async_session, org_id, 24)

    first = await client.post("/sms", json=_payload(), headers=_auth(plaintext))
    assert first.status_code == 201, first.text
    assert await trial_texts_used(async_session, org_id) == 25

    second = await client.post("/sms", json=_payload(), headers=_auth(plaintext))
    assert second.status_code == 422
    assert _msg(second).endswith(" " + TRIAL_HINT)
    assert len(_trial_sends(sms_mock)) == 1
    assert await _sms_count(async_session, org_id) == 25


async def test_cap_counts_only_this_workspace(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    other_id, _, _ = await insert_org_and_key(
        async_session, org_name="Other", org_slug="other"
    )
    await seed_trial_texts(async_session, other_id, 25, to=OTHER_PHONE)
    await verify_trial_phone(async_session, org_id)

    resp = await client.post("/sms", json=_payload(), headers=_auth(plaintext))

    assert resp.status_code == 201, resp.text
    assert resp.json()["from_e164"] == TRIAL_E164
