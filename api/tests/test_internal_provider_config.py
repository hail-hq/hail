"""Internal provider-config routes: HMAC gate, write-only keys, upsert semantics."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hailhq.core.config import settings
from hailhq.core.models import OrgProviderConfig
from hailhq.core.secret_cipher import generate_key

from .test_internal_dsar import _signed, internal_secret_set  # noqa: F401

ORG = str(uuid.uuid4())
BASE = f"/internal/orgs/{ORG}/providers"


@pytest.fixture()
def provider_key_set(monkeypatch):
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())


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
