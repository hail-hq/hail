"""Internal provider-config routes: HMAC gate, write-only keys, upsert semantics."""

from __future__ import annotations

import uuid

import pytest
from hailhq.core.config import settings
from hailhq.core.models import OrgProviderConfig
from hailhq.core.secret_cipher import generate_key
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .test_internal_dsar import _signed, internal_secret_set  # noqa: F401

ORG = str(uuid.uuid4())
BASE = f"/internal/orgs/{ORG}/providers"


@pytest.fixture()
def provider_key_set(monkeypatch):
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())
    monkeypatch.setattr(settings, "speechmatics_model", "linden-1")


async def test_requires_signature(client, internal_secret_set) -> None:  # noqa: F811
    resp = await client.get(BASE)
    assert resp.status_code == 401


async def test_put_get_roundtrip_masks_key(
    client, async_session, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    body = (
        b'{"provider":"cartesia","api_key":"sk-cart-ABCD",'
        b'"params":{"voice_id":"v-1"},"fallback_enabled":false}'
    )
    resp = await client.put(f"{BASE}/tts", content=body, headers=_signed(body))
    assert resp.status_code == 200, resp.text
    got = resp.json()
    assert got["key_last4"] == "ABCD"
    assert "api_key" not in got and "encrypted_api_key" not in got

    resp = await client.get(BASE, headers=_signed(b""))
    providers = resp.json()["providers"]
    assert providers == [
        {
            "layer": "tts",
            "provider": "cartesia",
            "key_last4": "ABCD",
            "key_set_at": providers[0]["key_set_at"],
            "params": {"voice_id": "v-1"},
            "fallback_enabled": False,
            "is_active": True,
        }
    ]
    assert providers[0]["key_set_at"] is not None

    row = (await async_session.execute(select(OrgProviderConfig))).scalar_one()
    assert row.encrypted_api_key != "sk-cart-ABCD"  # stored encrypted


async def test_put_without_key_keeps_stored_key(
    client, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    b1 = b'{"provider":"cartesia","api_key":"sk-1-AAAA","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/tts", content=b1, headers=_signed(b1))
    b2 = b'{"provider":"cartesia","params":{"voice_id":"v-2"},"fallback_enabled":true}'
    resp = await client.put(f"{BASE}/tts", content=b2, headers=_signed(b2))
    got = resp.json()
    assert got["key_last4"] == "AAAA"  # key survived the params-only update
    assert got["fallback_enabled"] is True


@pytest.mark.usefixtures("internal_secret_set")
async def test_language_choice_rejects_incompatible_voice_without_fallback(client):
    body = (
        b'{"provider":"elevenlabs","params":{"language":"th"},"fallback_enabled":false}'
    )
    response = await client.put(f"{BASE}/tts", content=body, headers=_signed(body))
    assert response.status_code == 422
    response = await client.get(BASE, headers=_signed(b""))
    assert response.json()["providers"] == []


async def test_put_rejects_wrong_provider_and_bad_params(
    client, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    bad_provider = b'{"provider":"deepgram","params":{},"fallback_enabled":false}'
    resp = await client.put(
        f"{BASE}/tts", content=bad_provider, headers=_signed(bad_provider)
    )
    assert resp.status_code == 422

    bad_params = (
        b'{"provider":"openai-compatible","api_key":"sk-x",'
        b'"params":{"model":"gpt-5.4-mini"},"fallback_enabled":false}'
    )  # missing base_url
    resp = await client.put(
        f"{BASE}/llm", content=bad_params, headers=_signed(bad_params)
    )
    assert resp.status_code == 422


async def test_put_key_without_cipher_key_is_503(
    client, internal_secret_set, monkeypatch  # noqa: F811
) -> None:
    monkeypatch.setattr(settings, "hail_provider_secret_key", "")
    body = (
        b'{"provider":"deepgram","api_key":"sk-d","params":{},"fallback_enabled":false}'
    )
    resp = await client.put(f"{BASE}/stt", content=body, headers=_signed(body))
    assert resp.status_code == 503


async def test_delete_reverts_to_default(
    client, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    b1 = b'{"provider":"deepgram","api_key":"sk-d-XY12","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/stt", content=b1, headers=_signed(b1))
    resp = await client.delete(f"{BASE}/stt/deepgram", headers=_signed(b""))
    assert resp.status_code == 204
    resp = await client.get(BASE, headers=_signed(b""))
    assert resp.json()["providers"] == []


async def test_put_second_provider_becomes_active_first_becomes_inactive(
    client, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    b1 = b'{"provider":"cartesia","api_key":"sk-1-AAAA","params":{},"fallback_enabled":false}'
    resp1 = await client.put(f"{BASE}/tts", content=b1, headers=_signed(b1))
    assert resp1.json()["is_active"] is True

    b2 = b'{"provider":"elevenlabs","api_key":"sk-2-BBBB","params":{},"fallback_enabled":false}'
    resp2 = await client.put(f"{BASE}/tts", content=b2, headers=_signed(b2))
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["is_active"] is True

    resp = await client.get(BASE, headers=_signed(b""))
    providers = {p["provider"]: p for p in resp.json()["providers"]}
    assert len(providers) == 2
    assert providers["cartesia"]["is_active"] is False
    assert providers["elevenlabs"]["is_active"] is True


async def test_activate_flips_active_provider(
    client, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    b1 = b'{"provider":"cartesia","api_key":"sk-1-AAAA","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/tts", content=b1, headers=_signed(b1))
    b2 = b'{"provider":"elevenlabs","api_key":"sk-2-BBBB","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/tts", content=b2, headers=_signed(b2))
    # elevenlabs is active after the second PUT; flip back to cartesia.
    body = b'{"provider":"cartesia"}'
    resp = await client.post(
        f"{BASE}/tts/activate", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["provider"] == "cartesia"
    assert resp.json()["is_active"] is True

    resp = await client.get(BASE, headers=_signed(b""))
    providers = {p["provider"]: p for p in resp.json()["providers"]}
    assert providers["cartesia"]["is_active"] is True
    assert providers["elevenlabs"]["is_active"] is False


async def test_activate_unknown_provider_404(
    client, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    b1 = b'{"provider":"cartesia","api_key":"sk-1-AAAA","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/tts", content=b1, headers=_signed(b1))
    body = b'{"provider":"elevenlabs"}'
    resp = await client.post(
        f"{BASE}/tts/activate", content=body, headers=_signed(body)
    )
    assert resp.status_code == 404


async def test_activate_race_returns_409(
    client, internal_secret_set, provider_key_set, monkeypatch  # noqa: F811
) -> None:
    b1 = b'{"provider":"cartesia","api_key":"sk-1-AAAA","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/tts", content=b1, headers=_signed(b1))

    async def fake_set_active(db, organization_id, layer, provider):
        raise IntegrityError("UPDATE", {}, Exception("race"))

    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config._set_active", fake_set_active
    )
    body = b'{"provider":"cartesia"}'
    resp = await client.post(
        f"{BASE}/tts/activate", content=body, headers=_signed(body)
    )
    assert resp.status_code == 409


async def test_delete_active_promotes_most_recently_updated_remaining(
    client, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    b1 = b'{"provider":"cartesia","api_key":"sk-1-AAAA","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/tts", content=b1, headers=_signed(b1))
    b2 = b'{"provider":"elevenlabs","api_key":"sk-2-BBBB","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/tts", content=b2, headers=_signed(b2))
    # elevenlabs is now active (most recently written); delete it.
    resp = await client.delete(f"{BASE}/tts/elevenlabs", headers=_signed(b""))
    assert resp.status_code == 204

    resp = await client.get(BASE, headers=_signed(b""))
    providers = resp.json()["providers"]
    assert len(providers) == 1
    assert providers[0]["provider"] == "cartesia"
    assert providers[0]["is_active"] is True


async def test_deactivation_preserves_inactive_siblings_updated_at(
    client, async_session, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    """Activating one provider must not bump inactive siblings' updated_at.

    Regression: the deactivate leg of _set_active once matched every
    sibling (provider != target), so on each activation it rewrote
    updated_at on rows that were already inactive too, collapsing every
    row to one timestamp and destroying the recency ordering that
    DELETE-promotion relies on. Only the row leaving active (and the target
    gaining it) should move.

    Uses the llm layer, the only one with three valid providers. Each PUT
    is its own request/transaction, so now() advances between them: after
    the three PUTs, anthropic was last written when google was activated,
    google was last written when openai-compatible was activated, so with
    the fix anthropic is strictly older than google. Under the bug the
    openai-compatible PUT bumps both, tying them.
    """
    bodies = [
        b'{"provider":"anthropic","api_key":"sk-AAAA","params":{"model":"m"}}',
        b'{"provider":"google","api_key":"sk-BBBB","params":{"model":"m"}}',
        (
            b'{"provider":"openai-compatible","api_key":"sk-CCCC",'
            b'"params":{"model":"m","base_url":"https://api.example.com"}}'
        ),
    ]
    for b in bodies:
        resp = await client.put(f"{BASE}/llm", content=b, headers=_signed(b))
        assert resp.status_code == 200, resp.text

    # expire_all() drops the identity-map cache so we read the committed DB
    # timestamps, not the stale values this shared test session loaded.
    async_session.expire_all()
    rows = {
        r.provider: r.updated_at
        for r in (await async_session.execute(select(OrgProviderConfig)))
        .scalars()
        .all()
    }
    assert rows["anthropic"] < rows["google"], rows

    # DELETE the active row (openai-compatible) → promotion must pick the
    # most-recently-updated survivor, google, not an arbitrary tie-break.
    resp = await client.delete(f"{BASE}/llm/openai-compatible", headers=_signed(b""))
    assert resp.status_code == 204
    resp = await client.get(BASE, headers=_signed(b""))
    active = [p for p in resp.json()["providers"] if p["is_active"]]
    assert [p["provider"] for p in active] == ["google"]


async def test_delete_inactive_provider_leaves_active_alone(
    client, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    b1 = b'{"provider":"cartesia","api_key":"sk-1-AAAA","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/tts", content=b1, headers=_signed(b1))
    b2 = b'{"provider":"elevenlabs","api_key":"sk-2-BBBB","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/tts", content=b2, headers=_signed(b2))
    # cartesia is inactive; deleting it should not disturb elevenlabs.
    resp = await client.delete(f"{BASE}/tts/cartesia", headers=_signed(b""))
    assert resp.status_code == 204

    resp = await client.get(BASE, headers=_signed(b""))
    providers = resp.json()["providers"]
    assert len(providers) == 1
    assert providers[0]["provider"] == "elevenlabs"
    assert providers[0]["is_active"] is True


async def test_validate_uses_stored_key(
    client, internal_secret_set, provider_key_set, monkeypatch  # noqa: F811
) -> None:
    async def fake_validate(layer, provider, api_key, params, client=None):
        return ("valid" if api_key == "sk-good" else "invalid", "checked")

    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config.validate_provider_key",
        fake_validate,
    )
    b1 = b'{"provider":"deepgram","api_key":"sk-good","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/stt", content=b1, headers=_signed(b1))
    resp = await client.post(
        f"{BASE}/stt/validate", content=b"{}", headers=_signed(b"{}")
    )
    assert resp.json() == {"status": "valid", "message": "checked"}

    b2 = b'{"api_key":"sk-bad"}'
    resp = await client.post(f"{BASE}/stt/validate", content=b2, headers=_signed(b2))
    assert resp.json()["status"] == "invalid"


async def test_validate_honors_explicit_provider_over_active(
    client, internal_secret_set, provider_key_set, monkeypatch  # noqa: F811
) -> None:
    # cartesia saved+active, then elevenlabs saved (now active, cartesia
    # inactive). Validating cartesia by name with no api_key must test
    # cartesia's stored key, not the active elevenlabs row.
    seen = {}

    async def fake_validate(layer, provider, api_key, params, client=None):
        seen.update(provider=provider, api_key=api_key)
        return "valid", "checked"

    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config.validate_provider_key",
        fake_validate,
    )
    b1 = b'{"provider":"cartesia","api_key":"sk-cart","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/tts", content=b1, headers=_signed(b1))
    b2 = b'{"provider":"elevenlabs","api_key":"sk-eleven","params":{},"fallback_enabled":false}'
    await client.put(f"{BASE}/tts", content=b2, headers=_signed(b2))

    body = b'{"provider":"cartesia"}'
    resp = await client.post(
        f"{BASE}/tts/validate", content=body, headers=_signed(body)
    )
    assert resp.json()["status"] == "valid"
    assert seen["provider"] == "cartesia"
    assert seen["api_key"] == "sk-cart"


async def test_validate_inflight_key_without_stored_row(
    client, internal_secret_set, provider_key_set, monkeypatch  # noqa: F811
) -> None:
    seen = {}

    async def fake_validate(layer, provider, api_key, params, client=None):
        seen.update(layer=layer, provider=provider, api_key=api_key, params=params)
        return "valid", "checked"

    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config.validate_provider_key",
        fake_validate,
    )
    body = (
        b'{"api_key":"sk-inflight","provider":"elevenlabs","params":{"voice_id":"v-1"}}'
    )
    resp = await client.post(
        f"{BASE}/tts/validate", content=body, headers=_signed(body)
    )
    assert resp.json() == {"status": "valid", "message": "checked"}
    assert seen == {
        "layer": "tts",
        "provider": "elevenlabs",
        "api_key": "sk-inflight",
        "params": {"voice_id": "v-1"},
    }


async def test_validate_inflight_bad_params_422(
    client, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    # openai-compatible with no base_url is an invalid config → 422 before probing
    body = b'{"api_key":"sk-x","provider":"openai-compatible","params":{"model":"m"}}'
    resp = await client.post(
        f"{BASE}/llm/validate", content=body, headers=_signed(body)
    )
    assert resp.status_code == 422


async def test_put_speechmatics_operating_point_accepted(
    client, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    body = (
        b'{"provider":"speechmatics","api_key":"sk-sm-WXYZ",'
        b'"params":{"operating_point":"standard"},"fallback_enabled":false}'
    )
    resp = await client.put(f"{BASE}/stt", content=body, headers=_signed(body))
    assert resp.status_code == 200, resp.text
    assert resp.json()["params"] == {"model": "linden-1"}

    resp = await client.get(BASE, headers=_signed(b""))
    providers = resp.json()["providers"]
    assert providers == [
        {
            "layer": "stt",
            "provider": "speechmatics",
            "key_last4": "WXYZ",
            "key_set_at": providers[0]["key_set_at"],
            "params": {"model": "linden-1"},
            "fallback_enabled": False,
            "is_active": True,
        }
    ]


async def test_put_deepgram_operating_point_rejected_422(
    client, internal_secret_set, provider_key_set  # noqa: F811
) -> None:
    body = (
        b'{"provider":"deepgram","api_key":"sk-dg-AAAA",'
        b'"params":{"operating_point":"standard"},"fallback_enabled":false}'
    )
    resp = await client.put(f"{BASE}/stt", content=body, headers=_signed(body))
    assert resp.status_code == 422


async def test_validate_inflight_speechmatics_operating_point(
    client, internal_secret_set, provider_key_set, monkeypatch  # noqa: F811
) -> None:
    seen = {}

    async def fake_validate(layer, provider, api_key, params, client=None):
        seen.update(layer=layer, provider=provider, api_key=api_key, params=params)
        return "valid", "checked"

    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config.validate_provider_key",
        fake_validate,
    )
    body = (
        b'{"api_key":"sk-sm-inflight","provider":"speechmatics",'
        b'"params":{"operating_point":"enhanced"}}'
    )
    resp = await client.post(
        f"{BASE}/stt/validate", content=body, headers=_signed(body)
    )
    assert resp.json() == {"status": "valid", "message": "checked"}
    assert seen == {
        "layer": "stt",
        "provider": "speechmatics",
        "api_key": "sk-sm-inflight",
        "params": {"model": "linden-1"},
    }


@pytest.mark.usefixtures("internal_secret_set")
async def test_picker_checks_retained_voice_with_stored_key_before_write(
    client, provider_key_set, monkeypatch
):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings, "cartesia_model", "sonic-3.5")
    original = b'{"provider":"cartesia","api_key":"customer-secret","params":{"voice_id":"custom","model":"custom-model","language":"en"},"fallback_enabled":false}'
    response = await client.put(
        f"{BASE}/tts", content=original, headers=_signed(original)
    )
    assert response.status_code == 200
    check = AsyncMock(side_effect=ValueError("This voice does not support French."))
    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config.validate_voice_language", check
    )
    changed = b'{"provider":"cartesia","params":{"voice_id":"custom","model":"custom-model","language":"fr"},"fallback_enabled":false,"verify_voice_language":true}'
    response = await client.put(
        f"{BASE}/tts", content=changed, headers=_signed(changed)
    )
    assert response.status_code == 422
    check.assert_awaited_once_with(
        "cartesia", "custom", "custom-model", "customer-secret", "fr"
    )
    saved = (await client.get(BASE, headers=_signed(b""))).json()["providers"][0]
    assert saved["params"]["language"] == "en"
    assert saved["key_last4"] == "cret"
    assert "customer-secret" not in str(saved)


@pytest.mark.usefixtures("internal_secret_set")
async def test_picker_validates_house_voice_and_persists_its_model(client, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings, "eleven_api_key", "house-key")
    check = AsyncMock()
    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config.validate_voice_language", check
    )
    body = b'{"provider":"elevenlabs","params":{"voice_id":"verified","model":"validated-model","language":"fr"},"fallback_enabled":true,"verify_voice_language":true}'
    response = await client.put(f"{BASE}/tts", content=body, headers=_signed(body))
    assert response.status_code == 200, response.text
    check.assert_awaited_once_with(
        "elevenlabs", "verified", "validated-model", "house-key", "fr"
    )
    assert response.json()["params"]["model"] == "validated-model"
    assert response.json()["key_last4"] is None


@pytest.mark.usefixtures("internal_secret_set")
async def test_picker_skips_voice_check_when_fallback_serves_the_language(
    client, monkeypatch
):
    from unittest.mock import AsyncMock

    check = AsyncMock(side_effect=ValueError("unsupported"))
    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config.validate_voice_language", check
    )
    body = b'{"provider":"elevenlabs","params":{"voice_id":"verified","language":"bn"},"fallback_enabled":true,"verify_voice_language":true}'
    response = await client.put(f"{BASE}/tts", content=body, headers=_signed(body))
    assert response.status_code == 200, response.text
    check.assert_not_awaited()


@pytest.mark.usefixtures("internal_secret_set")
@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [(404, 422), (422, 422), (401, 503), (403, 503), (429, 503), (500, 503)],
)
async def test_picker_maps_provider_status_errors(
    client, monkeypatch, provider_status, expected
):
    """A missing voice is the user's choice. A rejected key may be Hail's own."""
    from unittest.mock import AsyncMock

    import httpx

    request = httpx.Request("GET", "https://api.cartesia.ai/voices/missing")
    failure = httpx.HTTPStatusError(
        "provider error",
        request=request,
        response=httpx.Response(provider_status, request=request),
    )
    monkeypatch.setattr(settings, "cartesia_api_key", "house-key")
    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config.validate_voice_language",
        AsyncMock(side_effect=failure),
    )
    body = b'{"provider":"cartesia","params":{"voice_id":"missing","model":"sonic-3.5","language":"fr"},"fallback_enabled":false,"verify_voice_language":true}'
    response = await client.put(f"{BASE}/tts", content=body, headers=_signed(body))
    assert response.status_code == expected, response.text


async def _put(client, layer: str, body: bytes):
    return await client.put(f"{BASE}/{layer}", content=body, headers=_signed(body))


@pytest.mark.usefixtures("internal_secret_set", "provider_key_set")
async def test_stt_change_cannot_strand_the_saved_language(client):
    tts = b'{"provider":"cartesia","params":{"language":"zh"},"fallback_enabled":false}'
    assert (await _put(client, "tts", tts)).status_code == 200
    stt = b'{"provider":"speechmatics","params":{},"fallback_enabled":false}'
    response = await _put(client, "stt", stt)
    assert response.status_code == 422
    assert "speech recognition" in response.json()["detail"]
    assert (await client.get(BASE, headers=_signed(b""))).json()["providers"][0][
        "layer"
    ] == "tts"
    stt = b'{"provider":"speechmatics","params":{},"fallback_enabled":true}'
    assert (await _put(client, "stt", stt)).status_code == 200


@pytest.mark.usefixtures("internal_secret_set", "provider_key_set")
async def test_tts_activate_and_delete_cannot_strand_the_saved_language(client):
    chinese = (
        b'{"provider":"cartesia","params":{"language":"zh"},"fallback_enabled":false}'
    )
    assert (await _put(client, "tts", chinese)).status_code == 200
    plain = b'{"provider":"elevenlabs","params":{},"fallback_enabled":false}'
    assert (await _put(client, "tts", plain)).status_code == 200
    stt = b'{"provider":"speechmatics","params":{},"fallback_enabled":false}'
    assert (await _put(client, "stt", stt)).status_code == 200

    activate = b'{"provider":"cartesia"}'
    response = await client.post(
        f"{BASE}/tts/activate", content=activate, headers=_signed(activate)
    )
    assert response.status_code == 422
    response = await client.delete(f"{BASE}/tts/elevenlabs", headers=_signed(b""))
    assert response.status_code == 422

    rows = (await client.get(BASE, headers=_signed(b""))).json()["providers"]
    active_tts = [r["provider"] for r in rows if r["layer"] == "tts" and r["is_active"]]
    assert active_tts == ["elevenlabs"]


@pytest.mark.usefixtures("internal_secret_set", "provider_key_set")
async def test_delete_race_on_flush_returns_409(client, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm.exc import StaleDataError

    first = b'{"provider":"cartesia","params":{},"fallback_enabled":false}'
    second = b'{"provider":"elevenlabs","params":{},"fallback_enabled":false}'
    assert (await _put(client, "tts", first)).status_code == 200
    assert (await _put(client, "tts", second)).status_code == 200

    async def stale_flush(self, objects=None):
        raise StaleDataError("row deleted concurrently")

    monkeypatch.setattr(AsyncSession, "flush", stale_flush)
    response = await client.delete(f"{BASE}/tts/elevenlabs", headers=_signed(b""))
    assert response.status_code == 409
