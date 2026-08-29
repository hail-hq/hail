from __future__ import annotations

import uuid

import httpx
from hailhq.core.models import ApiKey


async def test_v1_prefix_reaches_whoami(
    client: httpx.AsyncClient, org_and_key: tuple[uuid.UUID, ApiKey, str]
) -> None:
    _, _, plain_key = org_and_key
    resp = await client.get(
        "/v1/whoami", headers={"Authorization": f"Bearer {plain_key}"}
    )
    assert resp.status_code == 200


async def test_unprefixed_path_still_works_and_is_marked_deprecated(
    client: httpx.AsyncClient, org_and_key: tuple[uuid.UUID, ApiKey, str]
) -> None:
    _, _, plain_key = org_and_key
    resp = await client.get("/whoami", headers={"Authorization": f"Bearer {plain_key}"})
    assert resp.status_code == 200
    assert resp.headers["deprecation"] == "true"
    assert 'rel="successor-version"' in resp.headers["link"]
    assert "/v1/whoami" in resp.headers["link"]


async def test_v1_path_is_not_marked_deprecated(
    client: httpx.AsyncClient, org_and_key: tuple[uuid.UUID, ApiKey, str]
) -> None:
    _, _, plain_key = org_and_key
    resp = await client.get(
        "/v1/whoami", headers={"Authorization": f"Bearer {plain_key}"}
    )
    assert "deprecation" not in resp.headers


def test_legacy_unprefixed_paths_are_not_in_the_openapi_schema() -> None:
    from hailhq.api.main import app

    schema = app.openapi()
    paths = schema["paths"]
    assert "/v1/whoami" in paths
    assert "/whoami" not in paths


async def test_internal_routes_are_not_dual_mounted(client: httpx.AsyncClient) -> None:
    # /internal/... must not also exist at /v1/internal/... — internal routers
    # were never versioned; a bare-string prefix match on "/v1" + internal's
    # own "/internal" prefix would be a real path if this task's loop is too
    # broad. hailhq/api/routes/internal/ses_events.py only registers
    # POST /internal/ses-events (no "healthz" sub-path exists). If the dual
    # mount loop wrongly included the internal routers, GET /v1/internal/
    # ses-events would resolve to a route that only accepts POST and 405;
    # since it isn't mounted at all under /v1, it 404s instead.
    resp = await client.get("/v1/internal/ses-events")
    assert resp.status_code == 404
