"""Tests for POST/GET /numbers — generic cross-channel number provisioning."""

from __future__ import annotations

import uuid


async def test_acquire_number_requires_auth(client) -> None:
    resp = await client.post(
        "/numbers", json={"country_code": "US", "number_type": "local"}
    )
    assert resp.status_code == 401


async def test_acquire_number_happy_path(
    client, org_and_key, voice_provider_mock
) -> None:
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/numbers",
        json={"country_code": "US", "number_type": "local"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["e164"]
    assert body["is_dedicated"] is True
    assert "sms" in body["capabilities"] or "voice" in body["capabilities"]


async def test_acquire_number_idempotent_replay(
    client, org_and_key, voice_provider_mock
) -> None:
    """Same Idempotency-Key on a retried acquire must NOT purchase a second
    number — the replay returns the cached number without re-invoking the
    provider."""
    _, _, plaintext = org_and_key
    headers = {
        "Authorization": f"Bearer {plaintext}",
        "Idempotency-Key": "acquire-retry-key",
    }
    body = {"country_code": "US", "number_type": "local"}

    first = await client.post("/numbers", json=body, headers=headers)
    assert first.status_code == 201, first.text

    second = await client.post("/numbers", json=body, headers=headers)
    assert second.status_code == 201, second.text
    assert second.headers.get("idempotency-replay") == "true"

    assert second.json()["id"] == first.json()["id"]
    voice_provider_mock.acquire_number.assert_awaited_once()


async def test_acquire_rejects_unlisted_country_type(
    client, org_and_key, voice_provider_mock
):
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/numbers",
        json={"country_code": "ZZ", "number_type": "local"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422, resp.text
    voice_provider_mock.acquire_number.assert_not_awaited()  # guarded before the provider


async def test_acquire_allows_listed_country_type(
    client, org_and_key, voice_provider_mock
):
    _, _, plaintext = org_and_key
    # US/local is in the seeded catalog; the provider mock returns a fake number.
    resp = await client.post(
        "/numbers",
        json={"country_code": "US", "number_type": "local"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.text
    voice_provider_mock.acquire_number.assert_awaited_once()


async def test_acquire_sms_only_country_requests_sms_capability_only(
    client, org_and_key, voice_provider_mock
):
    """SE/mobile is SMS-only in costs/telephony.json (voice=False, sms=True).
    The route must request only the capabilities the catalog row advertises,
    not the hardcoded ["voice", "sms"] — otherwise the Twilio adapter's AND
    filter matches nothing and acquisition 503s."""
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/numbers",
        json={"country_code": "SE", "number_type": "mobile"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.text
    voice_provider_mock.acquire_number.assert_awaited_once()
    _, kwargs = voice_provider_mock.acquire_number.call_args
    assert kwargs["capabilities"] == ["sms"]


async def test_acquire_voice_and_sms_country_requests_both_capabilities(
    client, org_and_key, voice_provider_mock
):
    """US/local supports both voice and sms — both must still be requested."""
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/numbers",
        json={"country_code": "US", "number_type": "local"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.text
    voice_provider_mock.acquire_number.assert_awaited_once()
    _, kwargs = voice_provider_mock.acquire_number.call_args
    assert kwargs["capabilities"] == ["voice", "sms"]


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
