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
    # own "/internal" prefix would be a real path if the dual-mount loop is too
    # broad. hailhq/api/routes/internal/ses_events.py only registers
    # POST /internal/ses-events. If the dual
    # mount loop wrongly included the internal routers, GET /v1/internal/
    # ses-events would resolve to a route that only accepts POST and 405;
    # since it isn't mounted at all under /v1, it 404s instead.
    resp = await client.get("/v1/internal/ses-events")
    assert resp.status_code == 404


async def test_non_customer_and_unmatched_paths_are_not_marked_deprecated(
    client: httpx.AsyncClient,
) -> None:
    # /openapi.json, /docs, /redoc and /healthz have no /v1 twin, and an
    # unmatched path matched no route: advertising a "/v1/..." successor for
    # them would point agents at a URL that does not exist.
    for path in ("/healthz", "/openapi.json", "/docs", "/redoc", "/nope", "/v1"):
        resp = await client.get(path)
        assert "deprecation" not in resp.headers, path
        assert "link" not in resp.headers, path


async def test_non_latin1_legacy_path_is_404_not_500(client: httpx.AsyncClient) -> None:
    # The successor Link is built from the decoded path; header values are
    # latin-1 encoded, so a non-latin-1 character used to raise
    # UnicodeEncodeError and turn a 404 into a 500.
    resp = await client.get("/%E2%82%AC")
    assert resp.status_code == 404


async def test_matched_legacy_route_with_non_latin1_segment_gets_encoded_link(
    client: httpx.AsyncClient, org_and_key: tuple[uuid.UUID, ApiKey, str]
) -> None:
    # /calls/{call_id} matches, so the Deprecation middleware runs on this
    # response (unlike an unmatched path, which the APIRoute guard skips).
    # The decoded path holds "€"; the Link must carry it percent-encoded
    # instead of failing the latin-1 header encode with a 500.
    _, _, plain_key = org_and_key
    resp = await client.get(
        "/calls/%E2%82%AC", headers={"Authorization": f"Bearer {plain_key}"}
    )
    assert resp.status_code != 500
    assert resp.headers["deprecation"] == "true"
    assert resp.headers["link"] == '</v1/calls/%E2%82%AC>; rel="successor-version"'


async def test_successor_link_is_percent_encoded_and_keeps_the_query(
    client: httpx.AsyncClient, org_and_key: tuple[uuid.UUID, ApiKey, str]
) -> None:
    _, _, plain_key = org_and_key
    resp = await client.get(
        "/calls?limit=5", headers={"Authorization": f"Bearer {plain_key}"}
    )
    assert resp.status_code == 200
    assert resp.headers["link"] == '</v1/calls?limit=5>; rel="successor-version"'
