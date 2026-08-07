"""Public /providers routes: API-key auth, org scoping, write-only keys.

The org-scoping tests are the security-critical ones: the public router
resolves the organization from the API key and never from a path or body,
so a key for org A must not be able to read or write org B's rows.
"""

from __future__ import annotations

import httpx
import pytest
from hailhq.core.config import settings
from hailhq.core.models import OrgProviderConfig
from hailhq.core.secret_cipher import generate_key
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import insert_org_and_key


@pytest.fixture()
def provider_key_set(monkeypatch):
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())


@pytest.fixture()
async def auth(org_and_key) -> dict[str, str]:
    _, _, plaintext = org_and_key
    return {"Authorization": f"Bearer {plaintext}"}


async def _save(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    layer: str,
    provider: str,
    **fields,
) -> dict:
    body = {"provider": provider, **fields}
    resp = await client.put(f"/providers/{layer}", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Auth.
# --------------------------------------------------------------------------- #


async def test_list_requires_auth(client) -> None:
    resp = await client.get("/providers")
    assert resp.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "json"),
    [
        ("GET", "/providers", None),
        ("PUT", "/providers/tts", {"provider": "cartesia"}),
        ("DELETE", "/providers/tts/cartesia", None),
        ("POST", "/providers/tts/activate", {"provider": "cartesia"}),
        ("POST", "/providers/tts/validate", {}),
    ],
)
async def test_every_verb_requires_auth(client, method, path, json) -> None:
    resp = await client.request(method, path, json=json)
    assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"


async def test_bad_api_key_is_401(client) -> None:
    resp = await client.get(
        "/providers", headers={"Authorization": "Bearer hl_live_nope"}
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Org scoping — a key for org A must not reach org B's rows.
# --------------------------------------------------------------------------- #


async def test_list_is_scoped_to_the_keys_org(
    client, async_session: AsyncSession, provider_key_set
) -> None:
    org_a, _, key_a = await insert_org_and_key(async_session, org_slug="a")
    org_b, _, key_b = await insert_org_and_key(async_session, org_slug="b")
    auth_a = {"Authorization": f"Bearer {key_a}"}
    auth_b = {"Authorization": f"Bearer {key_b}"}

    await _save(client, auth_a, "tts", "cartesia", api_key="sk-a-AAAA")

    # B sees nothing of A's.
    resp = await client.get("/providers", headers=auth_b)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"providers": []}

    # A sees only its own.
    resp = await client.get("/providers", headers=auth_a)
    providers = resp.json()["providers"]
    assert [p["provider"] for p in providers] == ["cartesia"]

    rows = (await async_session.execute(select(OrgProviderConfig))).scalars().all()
    assert [r.organization_id for r in rows] == [org_a]
    assert org_b not in {r.organization_id for r in rows}


async def test_writes_land_in_the_keys_org_only(
    client, async_session: AsyncSession, provider_key_set
) -> None:
    org_a, _, key_a = await insert_org_and_key(async_session, org_slug="a")
    org_b, _, key_b = await insert_org_and_key(async_session, org_slug="b")
    auth_a = {"Authorization": f"Bearer {key_a}"}
    auth_b = {"Authorization": f"Bearer {key_b}"}

    await _save(client, auth_a, "tts", "cartesia", api_key="sk-a-AAAA")
    await _save(client, auth_b, "tts", "cartesia", api_key="sk-b-BBBB")

    async_session.expire_all()
    rows = {
        r.organization_id: r
        for r in (await async_session.execute(select(OrgProviderConfig)))
        .scalars()
        .all()
    }
    assert set(rows) == {org_a, org_b}
    assert rows[org_a].key_last4 == "AAAA"
    assert rows[org_b].key_last4 == "BBBB"  # B's write did not overwrite A's row


async def test_delete_cannot_reach_another_orgs_row(
    client, async_session: AsyncSession, provider_key_set
) -> None:
    org_a, _, key_a = await insert_org_and_key(async_session, org_slug="a")
    _, _, key_b = await insert_org_and_key(async_session, org_slug="b")
    auth_a = {"Authorization": f"Bearer {key_a}"}
    auth_b = {"Authorization": f"Bearer {key_b}"}

    await _save(client, auth_a, "stt", "deepgram", api_key="sk-a-AAAA")

    # B deleting the same layer/provider is a no-op 204 against B's (empty)
    # rows — A's row must survive untouched.
    resp = await client.delete("/providers/stt/deepgram", headers=auth_b)
    assert resp.status_code == 204

    async_session.expire_all()
    rows = (await async_session.execute(select(OrgProviderConfig))).scalars().all()
    assert len(rows) == 1
    assert rows[0].organization_id == org_a
    assert rows[0].is_active is True


async def test_activate_cannot_reach_another_orgs_row(
    client, async_session: AsyncSession, provider_key_set
) -> None:
    _, _, key_a = await insert_org_and_key(async_session, org_slug="a")
    _, _, key_b = await insert_org_and_key(async_session, org_slug="b")
    auth_a = {"Authorization": f"Bearer {key_a}"}
    auth_b = {"Authorization": f"Bearer {key_b}"}

    await _save(client, auth_a, "tts", "cartesia", api_key="sk-a-AAAA")
    await _save(client, auth_a, "tts", "elevenlabs", api_key="sk-a-BBBB")

    # elevenlabs is active for A. B activating cartesia must 404 (B has no
    # such row) and leave A's active provider alone.
    resp = await client.post(
        "/providers/tts/activate", json={"provider": "cartesia"}, headers=auth_b
    )
    assert resp.status_code == 404

    resp = await client.get("/providers", headers=auth_a)
    active = [p["provider"] for p in resp.json()["providers"] if p["is_active"]]
    assert active == ["elevenlabs"]


async def test_validate_cannot_read_another_orgs_stored_key(
    client, async_session: AsyncSession, provider_key_set, monkeypatch
) -> None:
    seen: list[str] = []

    async def fake_validate(layer, provider, api_key, params, client=None):
        seen.append(api_key)
        return "valid", "checked"

    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config.validate_provider_key",
        fake_validate,
    )
    _, _, key_a = await insert_org_and_key(async_session, org_slug="a")
    _, _, key_b = await insert_org_and_key(async_session, org_slug="b")
    auth_a = {"Authorization": f"Bearer {key_a}"}
    auth_b = {"Authorization": f"Bearer {key_b}"}

    await _save(client, auth_a, "stt", "deepgram", api_key="sk-a-SECRET")

    # B has nothing stored: the probe must 422, never fall through to A's key.
    resp = await client.post("/providers/stt/validate", json={}, headers=auth_b)
    assert resp.status_code == 422
    assert seen == []


# --------------------------------------------------------------------------- #
# Happy paths, verb by verb.
# --------------------------------------------------------------------------- #


async def test_list_empty(client, auth) -> None:
    resp = await client.get("/providers", headers=auth)
    assert resp.status_code == 200
    assert resp.json() == {"providers": []}


async def test_put_then_list_roundtrip(
    client, auth, async_session: AsyncSession, provider_key_set
) -> None:
    saved = await _save(
        client,
        auth,
        "tts",
        "cartesia",
        api_key="sk-cart-ABCD",
        params={"voice_id": "v-1"},
        fallback_enabled=True,
    )
    assert saved["layer"] == "tts"
    assert saved["key_last4"] == "ABCD"
    assert saved["key_set_at"] is not None
    assert saved["params"] == {"voice_id": "v-1"}
    assert saved["fallback_enabled"] is True
    assert saved["is_active"] is True

    resp = await client.get("/providers", headers=auth)
    assert resp.json()["providers"] == [saved]

    row = (await async_session.execute(select(OrgProviderConfig))).scalar_one()
    assert row.encrypted_api_key != "sk-cart-ABCD"  # stored encrypted


async def test_put_without_key_keeps_the_stored_key(
    client, auth, provider_key_set
) -> None:
    await _save(client, auth, "tts", "cartesia", api_key="sk-1-AAAA")
    updated = await _save(client, auth, "tts", "cartesia", params={"voice_id": "v-2"})
    assert updated["key_last4"] == "AAAA"
    assert updated["params"] == {"voice_id": "v-2"}


async def test_put_llm_openai_compatible(client, auth, provider_key_set) -> None:
    saved = await _save(
        client,
        auth,
        "llm",
        "openai-compatible",
        api_key="sk-llm-WXYZ",
        params={"base_url": "https://llm.example.com/v1", "model": "my-model"},
    )
    assert saved["params"] == {
        "base_url": "https://llm.example.com/v1",
        "model": "my-model",
    }


async def test_activate_flips_the_active_provider(
    client, auth, provider_key_set
) -> None:
    await _save(client, auth, "tts", "cartesia", api_key="sk-1-AAAA")
    await _save(client, auth, "tts", "elevenlabs", api_key="sk-2-BBBB")

    resp = await client.post(
        "/providers/tts/activate", json={"provider": "cartesia"}, headers=auth
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["provider"] == "cartesia"
    assert resp.json()["is_active"] is True

    resp = await client.get("/providers", headers=auth)
    by_provider = {p["provider"]: p for p in resp.json()["providers"]}
    assert by_provider["cartesia"]["is_active"] is True
    assert by_provider["elevenlabs"]["is_active"] is False


async def test_delete_promotes_a_sibling(client, auth, provider_key_set) -> None:
    await _save(client, auth, "tts", "cartesia", api_key="sk-1-AAAA")
    await _save(client, auth, "tts", "elevenlabs", api_key="sk-2-BBBB")

    resp = await client.delete("/providers/tts/elevenlabs", headers=auth)
    assert resp.status_code == 204

    resp = await client.get("/providers", headers=auth)
    providers = resp.json()["providers"]
    assert [p["provider"] for p in providers] == ["cartesia"]
    assert providers[0]["is_active"] is True


async def test_delete_unknown_provider_is_204(client, auth, provider_key_set) -> None:
    resp = await client.delete("/providers/tts/cartesia", headers=auth)
    assert resp.status_code == 204


async def test_validate_uses_the_stored_key(
    client, auth, provider_key_set, monkeypatch
) -> None:
    seen: dict = {}

    async def fake_validate(layer, provider, api_key, params, client=None):
        seen.update(layer=layer, provider=provider, api_key=api_key, params=params)
        return "valid", "checked"

    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config.validate_provider_key",
        fake_validate,
    )
    await _save(client, auth, "stt", "deepgram", api_key="sk-good")

    resp = await client.post("/providers/stt/validate", json={}, headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "valid", "message": "checked"}
    assert seen["provider"] == "deepgram"
    assert seen["api_key"] == "sk-good"


async def test_validate_accepts_an_unsaved_key(
    client, auth, provider_key_set, monkeypatch
) -> None:
    seen: dict = {}

    async def fake_validate(layer, provider, api_key, params, client=None):
        seen.update(provider=provider, api_key=api_key)
        return "invalid", "401 from provider"

    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config.validate_provider_key",
        fake_validate,
    )
    resp = await client.post(
        "/providers/tts/validate",
        json={"provider": "elevenlabs", "api_key": "sk-inflight"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "invalid", "message": "401 from provider"}
    assert seen == {"provider": "elevenlabs", "api_key": "sk-inflight"}


# --------------------------------------------------------------------------- #
# Merge-on-update — the public router reads the row for the SAME provider and
# merges the request over it, so a partial write never silently clears a field
# the caller didn't mention. (The internal/console router keeps full-replace
# semantics; see test_internal_provider_config.py.)
# --------------------------------------------------------------------------- #


async def test_put_model_only_keeps_the_stored_voice_id(
    client, auth, provider_key_set
) -> None:
    await _save(
        client,
        auth,
        "tts",
        "cartesia",
        api_key="sk-1-AAAA",
        params={"voice_id": "v-1", "model": "sonic-2"},
    )
    updated = await _save(client, auth, "tts", "cartesia", params={"model": "sonic-3"})
    assert updated["params"] == {"voice_id": "v-1", "model": "sonic-3"}


async def test_put_model_only_keeps_fallback_enabled(
    client, auth, provider_key_set
) -> None:
    await _save(
        client,
        auth,
        "tts",
        "cartesia",
        api_key="sk-1-AAAA",
        params={"model": "sonic-2"},
        fallback_enabled=True,
    )
    updated = await _save(client, auth, "tts", "cartesia", params={"model": "sonic-3"})
    assert updated["fallback_enabled"] is True


async def test_put_explicit_false_turns_fallback_off(
    client, auth, provider_key_set
) -> None:
    await _save(
        client,
        auth,
        "stt",
        "deepgram",
        api_key="sk-1-AAAA",
        fallback_enabled=True,
    )
    updated = await _save(client, auth, "stt", "deepgram", fallback_enabled=False)
    assert updated["fallback_enabled"] is False


async def test_put_new_row_defaults_fallback_off(
    client, auth, provider_key_set
) -> None:
    saved = await _save(client, auth, "stt", "deepgram", api_key="sk-1-AAAA")
    assert saved["fallback_enabled"] is False


async def test_partial_params_are_validated_after_the_merge(
    client, auth, provider_key_set
) -> None:
    """``{"model": ...}`` alone would fail LLMParams (openai-compatible needs
    a base_url) — it validates because the merged result carries the stored
    base_url. This is what proves validation runs on the merge, not the input.
    """
    await _save(
        client,
        auth,
        "llm",
        "openai-compatible",
        api_key="sk-llm-AAAA",
        params={"base_url": "https://llm.example.com/v1", "model": "m-1"},
    )
    updated = await _save(
        client, auth, "llm", "openai-compatible", params={"model": "m-2"}
    )
    assert updated["params"] == {
        "base_url": "https://llm.example.com/v1",
        "model": "m-2",
    }


async def test_a_merge_that_would_be_invalid_is_422(
    client, auth, provider_key_set
) -> None:
    """The merged config is what gets validated, so a partial write cannot
    smuggle an invalid row past the layer schema — and the stored row is
    left exactly as it was."""
    await _save(
        client,
        auth,
        "llm",
        "openai-compatible",
        api_key="sk-llm-AAAA",
        params={"base_url": "https://llm.example.com/v1", "model": "m-1"},
    )
    resp = await client.put(
        "/providers/llm",
        json={
            "provider": "openai-compatible",
            "params": {"base_url": "http://insecure.example.com/v1"},
        },
        headers=auth,
    )
    assert resp.status_code == 422, resp.text

    resp = await client.get("/providers", headers=auth)
    assert resp.json()["providers"][0]["params"] == {
        "base_url": "https://llm.example.com/v1",
        "model": "m-1",
    }


async def test_a_different_provider_does_not_inherit_the_previous_params(
    client, auth, provider_key_set
) -> None:
    """Rows are keyed by (org, layer, provider): switching provider is a
    fresh config, so cartesia's voice_id must not leak into elevenlabs."""
    await _save(
        client,
        auth,
        "tts",
        "cartesia",
        api_key="sk-1-AAAA",
        params={"voice_id": "v-cartesia", "model": "sonic-2"},
        fallback_enabled=True,
    )
    saved = await _save(
        client,
        auth,
        "tts",
        "elevenlabs",
        api_key="sk-2-BBBB",
        params={"model": "eleven-v3"},
    )
    assert saved["params"] == {"model": "eleven-v3"}
    assert saved["fallback_enabled"] is False

    resp = await client.get("/providers", headers=auth)
    by_provider = {p["provider"]: p for p in resp.json()["providers"]}
    assert by_provider["cartesia"]["params"] == {
        "voice_id": "v-cartesia",
        "model": "sonic-2",
    }


# --------------------------------------------------------------------------- #
# 404 / 422.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "path", "json"),
    [
        ("PUT", "/providers/vision", {"provider": "cartesia"}),
        ("DELETE", "/providers/vision/cartesia", None),
        ("POST", "/providers/vision/activate", {"provider": "cartesia"}),
        ("POST", "/providers/vision/validate", {}),
    ],
)
async def test_unknown_layer_is_404(client, auth, method, path, json) -> None:
    resp = await client.request(method, path, json=json, headers=auth)
    assert resp.status_code == 404, resp.text
    assert "unknown layer" in resp.json()["detail"]


async def test_activate_unknown_provider_is_404(client, auth, provider_key_set) -> None:
    await _save(client, auth, "tts", "cartesia", api_key="sk-1-AAAA")
    resp = await client.post(
        "/providers/tts/activate", json={"provider": "elevenlabs"}, headers=auth
    )
    assert resp.status_code == 404


async def test_put_wrong_provider_for_layer_is_422(
    client, auth, provider_key_set
) -> None:
    resp = await client.put(
        "/providers/tts", json={"provider": "deepgram"}, headers=auth
    )
    assert resp.status_code == 422


async def test_put_bad_params_is_422(client, auth, provider_key_set) -> None:
    # openai-compatible requires base_url.
    resp = await client.put(
        "/providers/llm",
        json={"provider": "openai-compatible", "params": {"model": "m"}},
        headers=auth,
    )
    assert resp.status_code == 422


async def test_put_rejects_unknown_body_field(client, auth, provider_key_set) -> None:
    resp = await client.put(
        "/providers/tts",
        json={
            "provider": "cartesia",
            "organization_id": "00000000-0000-0000-0000-000000000000",
        },
        headers=auth,
    )
    assert resp.status_code == 422


async def test_put_missing_provider_is_422(client, auth, provider_key_set) -> None:
    resp = await client.put("/providers/tts", json={}, headers=auth)
    assert resp.status_code == 422


async def test_put_key_without_cipher_key_is_503(client, auth, monkeypatch) -> None:
    monkeypatch.setattr(settings, "hail_provider_secret_key", "")
    resp = await client.put(
        "/providers/stt",
        json={"provider": "deepgram", "api_key": "sk-d"},
        headers=auth,
    )
    assert resp.status_code == 503


# --------------------------------------------------------------------------- #
# Write-only keys.
# --------------------------------------------------------------------------- #


async def test_no_response_body_ever_carries_key_material(
    client, auth, provider_key_set, monkeypatch
) -> None:
    async def fake_validate(layer, provider, api_key, params, client=None):
        return "valid", "checked"

    monkeypatch.setattr(
        "hailhq.api.routes.internal.provider_config.validate_provider_key",
        fake_validate,
    )
    secret = "sk-super-secret-ABCD"
    bodies = [
        (
            await client.put(
                "/providers/stt",
                json={"provider": "deepgram", "api_key": secret},
                headers=auth,
            )
        ).text,
        (await client.get("/providers", headers=auth)).text,
        (
            await client.post(
                "/providers/stt/activate", json={"provider": "deepgram"}, headers=auth
            )
        ).text,
        (await client.post("/providers/stt/validate", json={}, headers=auth)).text,
    ]
    for body in bodies:
        assert secret not in body
        assert "sk-super-secret" not in body
        assert "api_key" not in body
        assert "encrypted_api_key" not in body
    # …but the last4 tail is exposed, by design.
    assert "ABCD" in bodies[0]
