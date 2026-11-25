"""Public routes for connected phone systems: /v1/sip-connections."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from hailhq.api import connected_numbers
from hailhq.api.connected_numbers import CheckResult, ConnectedNumberError
from hailhq.core.config import settings
from hailhq.core.models import AuditLog, IdempotencyKey, PhoneNumber
from hailhq.core.secret_cipher import generate_key
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import insert_org_and_key

SIP_HOST = "example.sip.livekit.cloud"


@pytest.fixture(autouse=True)
def configured(monkeypatch, livekit_mock):
    monkeypatch.setattr(
        "hailhq.api.routes.sip_connections.OPEN_TO_ALL_WORKSPACES", True
    )
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())
    monkeypatch.setattr(settings, "livekit_sip_host", SIP_HOST)
    livekit_mock.create_pbx_inbound_trunk.return_value = "ST_in"
    livekit_mock.create_pbx_outbound_trunk.return_value = "ST_out"


def _auth(plain: str) -> dict:
    return {"Authorization": f"Bearer {plain}"}


async def _create(client: httpx.AsyncClient, plain: str, **over) -> dict:
    body = {"label": "Main office", "default_region": "US", **over}
    resp = await client.post("/sip-connections", json=body, headers=_auth(plain))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_connection_returns_password_once(
    client: httpx.AsyncClient, org_and_key: tuple
) -> None:
    _org, _key, plain = org_and_key
    body = await _create(client, plain, pbx_host="pbx.example.com")
    assert len(body["password"]) >= 24
    assert body["password_last4"] == body["password"][-4:]
    # A workspace sees only the start of the SIP server.
    assert body["sip_host"] == "examp••••••••••••"
    assert body["sip_host_masked"] is True
    assert SIP_HOST not in str(body)
    assert body["username"]
    assert (body["pbx_host"], body["pbx_port"], body["pbx_transport"]) == (
        "pbx.example.com",
        5060,
        "auto",
    )
    assert body["pbx_password_set"] is False


async def test_create_connection_password_only_in_create_response(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    _org, _key, plain = org_and_key
    resp = await client.post(
        "/sip-connections",
        json={"label": "Main office", "default_region": "US"},
        headers={**_auth(plain), "Idempotency-Key": str(uuid4())},
    )
    assert resp.status_code == 201
    password = resp.json()["password"]
    stored = await async_session.scalar(
        select(func.count()).select_from(IdempotencyKey)
    )
    assert stored == 0
    listed = await client.get("/sip-connections", headers=_auth(plain))
    one = await client.get(
        f"/sip-connections/{resp.json()['id']}", headers=_auth(plain)
    )
    for r in (listed, one):
        assert r.status_code == 200
        assert "password" not in r.text.replace("password_last4", "").replace(
            "pbx_password_set", ""
        )
        assert password not in r.text


async def test_list_connections_newest_first(
    client: httpx.AsyncClient, org_and_key: tuple
) -> None:
    _org, _key, plain = org_and_key
    first = await _create(client, plain, label="First")
    second = await _create(client, plain, label="Second")
    body = (await client.get("/sip-connections?limit=1", headers=_auth(plain))).json()
    assert [c["id"] for c in body["items"]] == [second["id"]]
    page2 = (
        await client.get(
            f"/sip-connections?limit=1&cursor={body['next_cursor']}",
            headers=_auth(plain),
        )
    ).json()
    assert [c["id"] for c in page2["items"]] == [first["id"]]


async def test_connections_are_org_scoped(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    _org, _key, plain = org_and_key
    conn = await _create(client, plain)
    _other, _k, other_plain = await insert_org_and_key(async_session, org_slug="b")
    h = _auth(other_plain)
    url = f"/sip-connections/{conn['id']}"
    assert (await client.get(url, headers=h)).status_code == 404
    assert (await client.patch(url, json={"label": "x"}, headers=h)).status_code == 404
    assert (await client.delete(url, headers=h)).status_code == 404
    assert (await client.post(f"{url}/rotate-password", headers=h)).status_code == 404
    assert (
        await client.post(f"{url}/numbers", json={"e164": "+14155550100"}, headers=h)
    ).status_code == 404
    listed = (await client.get("/sip-connections", headers=h)).json()
    assert listed["items"] == []


async def test_patch_clears_pbx_host(
    client: httpx.AsyncClient, org_and_key: tuple, livekit_mock: AsyncMock
) -> None:
    _org, _key, plain = org_and_key
    conn = await _create(client, plain, pbx_host="pbx.example.com")
    resp = await client.patch(
        f"/sip-connections/{conn['id']}",
        json={"pbx_host": None},
        headers=_auth(plain),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pbx_host"] is None
    livekit_mock.delete_sip_trunk.assert_awaited_with("ST_out")


async def test_patch_label_only(client: httpx.AsyncClient, org_and_key: tuple) -> None:
    _org, _key, plain = org_and_key
    conn = await _create(client, plain, pbx_host="pbx.example.com")
    resp = await client.patch(
        f"/sip-connections/{conn['id']}",
        json={"label": "Front desk"},
        headers=_auth(plain),
    )
    assert resp.json()["label"] == "Front desk"
    assert resp.json()["pbx_host"] == "pbx.example.com"


async def test_rotate_password_returns_new_password(
    client: httpx.AsyncClient, org_and_key: tuple
) -> None:
    _org, _key, plain = org_and_key
    conn = await _create(client, plain)
    resp = await client.post(
        f"/sip-connections/{conn['id']}/rotate-password", headers=_auth(plain)
    )
    assert resp.status_code == 200
    new = resp.json()["password"]
    assert new != conn["password"]
    got = (
        await client.get(f"/sip-connections/{conn['id']}", headers=_auth(plain))
    ).json()
    assert got["password_last4"] == new[-4:]


async def test_delete_connection_with_numbers_409(
    client: httpx.AsyncClient, org_and_key: tuple
) -> None:
    _org, _key, plain = org_and_key
    conn = await _create(client, plain)
    added = await client.post(
        f"/sip-connections/{conn['id']}/numbers",
        json={"e164": "+14155550100"},
        headers=_auth(plain),
    )
    assert added.status_code == 201, added.text
    assert added.json()["source"] == "connected"
    assert added.json()["provisioning_state"] == "unverified"
    resp = await client.delete(f"/sip-connections/{conn['id']}", headers=_auth(plain))
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Remove this connection's numbers first."


async def test_delete_connection(client: httpx.AsyncClient, org_and_key: tuple) -> None:
    _org, _key, plain = org_and_key
    conn = await _create(client, plain)
    url = f"/sip-connections/{conn['id']}"
    assert (await client.delete(url, headers=_auth(plain))).status_code == 204
    assert (await client.get(url, headers=_auth(plain))).status_code == 404


async def test_add_number_invalid_is_422(
    client: httpx.AsyncClient, org_and_key: tuple
) -> None:
    _org, _key, plain = org_and_key
    conn = await _create(client, plain)
    resp = await client.post(
        f"/sip-connections/{conn['id']}/numbers",
        json={"e164": "12"},
        headers=_auth(plain),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "e164"]


@pytest.mark.parametrize(
    "result,expected",
    [
        (
            CheckResult("active", None, None),
            {"state": "active", "reason": None},
        ),
        (
            CheckResult("unverified", "no_call_arrived", None),
            {"state": "unverified", "reason": "no_call_arrived"},
        ),
    ],
)
async def test_verify_returns_the_check_result(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
    result: CheckResult,
    expected: dict,
) -> None:
    _org, _key, plain = org_and_key
    conn = await _create(client, plain)
    number = (
        await client.post(
            f"/sip-connections/{conn['id']}/numbers",
            json={"e164": "+14155550100"},
            headers=_auth(plain),
        )
    ).json()
    check = AsyncMock(return_value=result)
    monkeypatch.setattr(
        "hailhq.api.routes.sip_connections.check_connected_number", check
    )
    resp = await client.post(
        f"/sip-connections/{conn['id']}/numbers/{number['id']}/verify",
        headers=_auth(plain),
    )
    assert resp.status_code == 200, resp.text
    assert {k: resp.json()[k] for k in ("state", "reason")} == expected


@pytest.mark.parametrize(
    "code,status",
    [("check_limit", 429), ("no_funds", 402), ("check_in_progress", 409)],
)
async def test_verify_refusals(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status: int,
) -> None:
    _org, _key, plain = org_and_key
    conn = await _create(client, plain)
    number = (
        await client.post(
            f"/sip-connections/{conn['id']}/numbers",
            json={"e164": "+14155550100"},
            headers=_auth(plain),
        )
    ).json()
    monkeypatch.setattr(
        "hailhq.api.routes.sip_connections.check_connected_number",
        AsyncMock(side_effect=ConnectedNumberError(code, None, code)),
    )
    resp = await client.post(
        f"/sip-connections/{conn['id']}/numbers/{number['id']}/verify",
        headers=_auth(plain),
    )
    assert resp.status_code == status
    assert resp.json()["detail"] != code  # customer copy, never the raw code


async def test_number_of_another_connection_is_404(
    client: httpx.AsyncClient, org_and_key: tuple
) -> None:
    _org, _key, plain = org_and_key
    a = await _create(client, plain)
    b = await _create(client, plain, label="Other")
    number = (
        await client.post(
            f"/sip-connections/{a['id']}/numbers",
            json={"e164": "+14155550100"},
            headers=_auth(plain),
        )
    ).json()
    resp = await client.delete(
        f"/sip-connections/{b['id']}/numbers/{number['id']}", headers=_auth(plain)
    )
    assert resp.status_code == 404


async def test_remove_number_route(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    _org, _key, plain = org_and_key
    conn = await _create(client, plain)
    number = (
        await client.post(
            f"/sip-connections/{conn['id']}/numbers",
            json={"e164": "+14155550100"},
            headers=_auth(plain),
        )
    ).json()
    resp = await client.delete(
        f"/sip-connections/{conn['id']}/numbers/{number['id']}", headers=_auth(plain)
    )
    assert resp.status_code == 204
    state = await async_session.scalar(
        select(PhoneNumber.provisioning_state)
        .where(PhoneNumber.id == number["id"])
        .execution_options(populate_existing=True)
    )
    assert state == "released"


async def test_routes_503_without_sip_host(
    client: httpx.AsyncClient, org_and_key: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _org, _key, plain = org_and_key
    monkeypatch.setattr(settings, "livekit_sip_host", "")
    resp = await client.post(
        "/sip-connections",
        json={"label": "Main office", "default_region": "US"},
        headers=_auth(plain),
    )
    assert resp.status_code == 503


def test_every_service_code_has_customer_copy() -> None:
    from hailhq.api.routes.sip_connections import _ERRORS

    codes = set(
        connected_numbers.ConnectedNumberError.__doc__.split("one of:")[1]
        .replace("\n", " ")
        .replace(".", "")
        .replace(" ", "")
        .split(",")
    )
    assert codes <= set(_ERRORS)


async def test_unreadable_password_does_not_break_the_list(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    from hailhq.core.models import SipConnection
    from sqlalchemy import update

    _org, _key, plain = org_and_key
    conn = await _create(client, plain)
    await async_session.execute(
        update(SipConnection)
        .where(SipConnection.id == conn["id"])
        .values(encrypted_inbound_password="not-a-fernet-token")
    )
    await async_session.commit()
    resp = await client.get("/sip-connections", headers=_auth(plain))
    assert resp.status_code == 200
    assert resp.json()["items"][0]["password_last4"] == ""


async def test_verify_a_removed_number_says_so(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    from sqlalchemy import update

    _org, _key, plain = org_and_key
    conn = await _create(client, plain)
    number = (
        await client.post(
            f"/sip-connections/{conn['id']}/numbers",
            json={"e164": "+14155550100"},
            headers=_auth(plain),
        )
    ).json()
    await async_session.execute(
        update(PhoneNumber)
        .where(PhoneNumber.id == number["id"])
        .values(provisioning_state="released")
    )
    await async_session.commit()
    resp = await client.post(
        f"/sip-connections/{conn['id']}/numbers/{number['id']}/verify",
        headers=_auth(plain),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "This number was removed from Hail. Add it again."


async def test_staff_only_hides_the_routes_from_api_keys(
    client: httpx.AsyncClient, org_and_key: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "hailhq.api.routes.sip_connections.OPEN_TO_ALL_WORKSPACES", False
    )
    _org, _key, plain = org_and_key
    assert (
        await client.get("/sip-connections", headers=_auth(plain))
    ).status_code == 404
    resp = await client.post(
        "/sip-connections",
        json={"label": "Main office", "default_region": "US"},
        headers=_auth(plain),
    )
    assert resp.status_code == 404


async def test_staff_only_lets_a_staff_console_session_in(
    client: httpx.AsyncClient, org_and_key: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hailhq.api.deps import Principal, get_current_principal
    from hailhq.api.main import app

    monkeypatch.setattr(
        "hailhq.api.routes.sip_connections.OPEN_TO_ALL_WORKSPACES", False
    )
    org, _key, _plain = org_and_key
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        auth_kind="jwt",
        api_key_id=None,
        user_id=None,
        organization_id=org,
        scopes=[],
        superadmin=True,
        console_session=True,
    )
    try:
        resp = await client.get("/sip-connections")
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
    assert resp.status_code == 200


ROUTES = [
    ("get", "/sip-connections"),
    ("post", "/sip-connections"),
    ("get", "/sip-connections/{c}"),
    ("patch", "/sip-connections/{c}"),
    ("post", "/sip-connections/{c}/rotate-password"),
    ("get", "/sip-connections/{c}/password"),
    ("delete", "/sip-connections/{c}"),
    ("post", "/sip-connections/{c}/numbers"),
    ("post", "/sip-connections/{c}/numbers/{n}/verify"),
    ("delete", "/sip-connections/{c}/numbers/{n}"),
]


@pytest.mark.parametrize("method,path", ROUTES)
async def test_staff_only_refuses_a_customer_console_session_everywhere(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    from hailhq.api.deps import Principal, get_current_principal
    from hailhq.api.main import app

    monkeypatch.setattr(
        "hailhq.api.routes.sip_connections.OPEN_TO_ALL_WORKSPACES", False
    )
    org, _key, _plain = org_and_key
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        auth_kind="jwt",
        api_key_id=None,
        user_id=None,
        organization_id=org,
        scopes=[],
        superadmin=False,
        console_session=True,
    )
    url = path.format(c=uuid4(), n=uuid4())
    try:
        resp = await client.request(method.upper(), url, json={})
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not Found"


async def test_staff_only_needs_a_jwt_even_with_the_staff_flag(
    client: httpx.AsyncClient, org_and_key: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hailhq.api.deps import Principal, get_current_principal
    from hailhq.api.main import app

    monkeypatch.setattr(
        "hailhq.api.routes.sip_connections.OPEN_TO_ALL_WORKSPACES", False
    )
    org, _key, _plain = org_and_key
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        auth_kind="shared",
        api_key_id=None,
        user_id=None,
        organization_id=org,
        scopes=[],
        superadmin=True,
    )
    try:
        resp = await client.get("/sip-connections")
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
    assert resp.status_code == 404


async def test_read_password_returns_the_current_one_and_is_audited(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    org, _key, plain = org_and_key
    conn = await _create(client, plain)
    resp = await client.get(
        f"/sip-connections/{conn['id']}/password", headers=_auth(plain)
    )
    assert resp.status_code == 200
    assert resp.json() == {"password": conn["password"]}
    assert resp.headers["cache-control"] == "no-store"
    rotated = (
        await client.post(
            f"/sip-connections/{conn['id']}/rotate-password", headers=_auth(plain)
        )
    ).json()["password"]
    again = await client.get(
        f"/sip-connections/{conn['id']}/password", headers=_auth(plain)
    )
    assert again.json() == {"password": rotated}
    logs = (
        (
            await async_session.execute(
                select(AuditLog).where(
                    AuditLog.organization_id == org,
                    AuditLog.action == "sip_connection.read_password",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(logs) == 2
    assert all(
        conn["password"] not in str(log.payload) and rotated not in str(log.payload)
        for log in logs
    )


async def test_password_responses_are_never_cached(
    client: httpx.AsyncClient, org_and_key: tuple
) -> None:
    _org, _key, plain = org_and_key
    resp = await client.post(
        "/sip-connections",
        json={"label": "Main office", "default_region": "US"},
        headers=_auth(plain),
    )
    assert resp.headers["cache-control"] == "no-store"
    rotated = await client.post(
        f"/sip-connections/{resp.json()['id']}/rotate-password", headers=_auth(plain)
    )
    assert rotated.headers["cache-control"] == "no-store"


async def test_unreadable_password_is_409_and_not_audited(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    from hailhq.core.models import SipConnection
    from sqlalchemy import update

    _org, _key, plain = org_and_key
    conn = await _create(client, plain)
    await async_session.execute(
        update(SipConnection)
        .where(SipConnection.id == conn["id"])
        .values(encrypted_inbound_password="not-a-fernet-token")
    )
    await async_session.commit()
    resp = await client.get(
        f"/sip-connections/{conn['id']}/password", headers=_auth(plain)
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "This password cannot be read. Make a new one."
    reads = await async_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "sip_connection.read_password")
    )
    assert reads == 0


async def test_no_password_without_its_audit_row(
    client: httpx.AsyncClient, org_and_key: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _org, _key, plain = org_and_key
    conn = await _create(client, plain)

    async def broken(*args, **kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(
        "hailhq.api.routes.sip_connections.record_password_read", broken
    )
    resp = await client.get(
        f"/sip-connections/{conn['id']}/password", headers=_auth(plain)
    )
    assert resp.status_code == 503
    assert conn["password"] not in resp.text


def _console(org, *, superadmin: bool, auth_kind: str = "jwt"):
    from hailhq.api.deps import Principal

    return lambda: Principal(
        auth_kind=auth_kind,
        api_key_id=None,
        user_id=None,
        organization_id=org,
        scopes=[],
        superadmin=superadmin,
        console_session=auth_kind == "jwt",
    )


async def test_every_workspace_reaches_the_routes_with_the_host_masked(
    client: httpx.AsyncClient, org_and_key: tuple
) -> None:
    from hailhq.api.deps import get_current_principal
    from hailhq.api.main import app

    org, _key, plain = org_and_key
    created = await _create(client, plain)
    # An API key and a customer console session both get the masked host,
    # in create, list, get and update.
    app.dependency_overrides[get_current_principal] = _console(org, superadmin=False)
    try:
        listed = (await client.get("/sip-connections")).json()["items"][0]
        one = (await client.get(f"/sip-connections/{created['id']}")).json()
        patched = (
            await client.patch(
                f"/sip-connections/{created['id']}", json={"label": "Front desk"}
            )
        ).json()
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
    for body in (created, listed, one, patched):
        assert body["sip_host"] == "examp••••••••••••"
        assert body["sip_host_masked"] is True
        assert SIP_HOST not in str(body)


@pytest.mark.parametrize("auth_kind,superadmin", [("jwt", True), ("shared", False)])
async def test_staff_and_a_self_hosted_operator_see_the_full_host(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    auth_kind: str,
    superadmin: bool,
) -> None:
    from hailhq.api.deps import get_current_principal
    from hailhq.api.main import app

    org, _key, plain = org_and_key
    created = await _create(client, plain)
    app.dependency_overrides[get_current_principal] = _console(
        org, superadmin=superadmin, auth_kind=auth_kind
    )
    try:
        one = (await client.get(f"/sip-connections/{created['id']}")).json()
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
    assert one["sip_host"] == SIP_HOST
    assert one["sip_host_masked"] is False


def test_the_routes_ship_open_to_every_workspace() -> None:
    # The autouse fixture patches the switch; this reads the shipped line.
    import inspect

    import hailhq.api.routes.sip_connections as routes

    source = inspect.getsource(routes)
    assert "\nOPEN_TO_ALL_WORKSPACES: Final = True\n" in source


async def test_a_short_host_is_shown_in_full_and_not_flagged(
    client: httpx.AsyncClient, org_and_key: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "livekit_sip_host", "sip.x")
    _org, _key, plain = org_and_key
    body = await _create(client, plain)
    assert (body["sip_host"], body["sip_host_masked"]) == ("sip.x", False)


def test_a_short_host_is_not_masked_into_nothing() -> None:
    from hailhq.api.routes.sip_connections import _masked_host

    assert _masked_host("abc") == "abc"
    assert _masked_host("abcdef.example") == "abcde…"


async def test_the_first_number_sets_the_connection_country(
    client: httpx.AsyncClient, org_and_key: tuple
) -> None:
    _org, _key, plain = org_and_key
    resp = await client.post(
        "/sip-connections", json={"label": "Main office"}, headers=_auth(plain)
    )
    assert resp.status_code == 201, resp.text
    conn = resp.json()
    for e164 in ("+442079460018", "+14155550100"):
        added = await client.post(
            f"/sip-connections/{conn['id']}/numbers",
            json={"e164": e164},
            headers=_auth(plain),
        )
        assert added.status_code == 201, added.text
    one = (
        await client.get(f"/sip-connections/{conn['id']}", headers=_auth(plain))
    ).json()
    # The first number (GB) decides; the second (US) does not change it.
    assert one["default_region"] == "GB"
