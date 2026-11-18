"""HTTP tools on agents: save, show, keep headers, rotate the secret, and
refuse private URLs."""

from __future__ import annotations

import json
import uuid

import pytest
from hailhq.api.routes import agents as agents_routes
from hailhq.core.config import settings
from hailhq.core.models import Agent, AgentHttpTool
from hailhq.core.provider_config import provider_cipher
from hailhq.core.secret_cipher import generate_key
from hailhq.core.url_guard import NOT_PUBLIC
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import insert_org_and_key


@pytest.fixture()
async def org(async_session: AsyncSession):
    org_id, _key, plain = await insert_org_and_key(async_session)
    return org_id, {"Authorization": f"Bearer {plain}"}


@pytest.fixture(autouse=True)
def secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())


@pytest.fixture()
def public_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Treat every host as public; record the URLs the route checked."""
    checked: list[str] = []

    def fake(url: str) -> str:
        checked.append(url)
        return url

    monkeypatch.setattr(agents_routes, "assert_public_https_url", fake)
    return checked


def _tool(**over):
    base = {
        "name": "lookup_order",
        "description": "Find an order by its number.",
        "url": "https://api.example.com/orders",
        "parameters": {
            "type": "object",
            "properties": {"order_number": {"type": "string"}},
            "required": ["order_number"],
        },
        "headers": {"Authorization": "Bearer customer-token"},
        "wait_message": "One moment.",
    }
    base.update(over)
    return base


async def _create(client, headers, **over):
    body = {"name": "Desk", "system_prompt": "Help.", **over}
    return await client.post("/v1/agents", headers=headers, json=body)


async def _rows(session: AsyncSession, agent_id: str) -> list[AgentHttpTool]:
    return list(
        (
            await session.execute(
                select(AgentHttpTool)
                .where(AgentHttpTool.agent_id == uuid.UUID(agent_id))
                .order_by(AgentHttpTool.position)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def test_create_returns_the_secret_once_and_hides_header_values(
    client, org, public_dns, async_session
):
    _, headers = org
    resp = await _create(client, headers, http_tools=[_tool()])
    assert resp.status_code == 201, resp.text
    agent = resp.json()
    secret = agent["http_tool_secret"]
    assert secret.startswith("hts_")
    (tool,) = agent["http_tools"]
    assert tool["header_names"] == ["Authorization"]
    assert "customer-token" not in resp.text
    assert tool["timeout_seconds"] == 5
    assert tool["wait_message"] == "One moment."
    assert public_dns == ["https://api.example.com/orders"]

    got = await client.get(f"/v1/agents/{agent['id']}", headers=headers)
    assert got.json()["http_tool_secret"] is None
    assert got.json()["http_tools"] == agent["http_tools"]
    assert "customer-token" not in got.text

    # Stored encrypted.
    (row,) = await _rows(async_session, agent["id"])
    assert "customer-token" not in (row.headers_encrypted or "")
    cipher = provider_cipher()
    assert json.loads(cipher.decrypt(row.headers_encrypted)) == {
        "Authorization": "Bearer customer-token"
    }
    stored = await async_session.scalar(
        select(Agent.http_tool_secret_encrypted).where(
            Agent.id == uuid.UUID(agent["id"])
        )
    )
    assert cipher.decrypt(stored) == secret


async def test_agent_without_tools_has_no_secret(client, org):
    _, headers = org
    resp = await _create(client, headers)
    assert resp.status_code == 201
    assert resp.json()["http_tools"] == []
    assert resp.json()["http_tool_secret"] is None


async def test_patch_keeps_headers_left_out_and_clears_with_empty(
    client, org, public_dns, async_session
):
    _, headers = org
    agent = (await _create(client, headers, http_tools=[_tool()])).json()
    url = f"/v1/agents/{agent['id']}"

    # The list as GET shows it, sent back: headers are kept, no new secret.
    shown = agent["http_tools"][0]
    resend = {k: v for k, v in shown.items() if k != "header_names"}
    resend["description"] = "Look up an order."
    resp = await client.patch(url, headers=headers, json={"http_tools": [resend]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["http_tool_secret"] is None
    assert resp.json()["http_tools"][0]["header_names"] == ["Authorization"]
    (row,) = await _rows(async_session, agent["id"])
    assert row.description == "Look up an order."
    assert json.loads(provider_cipher().decrypt(row.headers_encrypted)) == {
        "Authorization": "Bearer customer-token"
    }

    resp = await client.patch(
        url, headers=headers, json={"http_tools": [{**resend, "headers": {}}]}
    )
    assert resp.json()["http_tools"][0]["header_names"] == []

    # null leaves the list; [] removes every tool.
    resp = await client.patch(url, headers=headers, json={"name": "Desk 2"})
    assert len(resp.json()["http_tools"]) == 1
    resp = await client.patch(url, headers=headers, json={"http_tools": []})
    assert resp.json()["http_tools"] == []
    assert await _rows(async_session, agent["id"]) == []


async def test_first_tools_on_patch_create_the_secret(client, org, public_dns):
    _, headers = org
    agent = (await _create(client, headers)).json()
    resp = await client.patch(
        f"/v1/agents/{agent['id']}", headers=headers, json={"http_tools": [_tool()]}
    )
    assert resp.status_code == 200
    assert resp.json()["http_tool_secret"].startswith("hts_")


async def test_rotate_tool_secret(client, org, public_dns, async_session):
    _, headers = org
    agent = (await _create(client, headers, http_tools=[_tool()])).json()
    resp = await client.post(
        f"/v1/agents/{agent['id']}/rotate-tool-secret", headers=headers
    )
    assert resp.status_code == 200
    new = resp.json()["http_tool_secret"]
    assert new.startswith("hts_") and new != agent["http_tool_secret"]
    stored = await async_session.scalar(
        select(Agent.http_tool_secret_encrypted)
        .where(Agent.id == uuid.UUID(agent["id"]))
        .execution_options(populate_existing=True)
    )
    assert provider_cipher().decrypt(stored) == new

    other = await client.post(
        f"/v1/agents/{uuid.uuid4()}/rotate-tool-secret", headers=headers
    )
    assert other.status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/orders",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.8/orders",
        "https://[::1]/orders",
        "https://127.1/orders",
    ],
)
async def test_private_url_is_refused_on_save(client, org, url):
    _, headers = org
    resp = await _create(client, headers, http_tools=[_tool(url=url)])
    assert resp.status_code == 422, resp.text
    assert NOT_PUBLIC in resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "http_tools", 0, "url"]


async def test_bad_tools_are_refused(client, org, public_dns):
    _, headers = org
    for tool in (
        _tool(name="send_sms"),
        _tool(url="http://api.example.com/orders"),
        _tool(headers={"X-Hail-Signature": "forged"}),
        _tool(timeout_seconds=30),
    ):
        resp = await _create(client, headers, http_tools=[tool])
        assert resp.status_code == 422, tool
    many = [_tool(name=f"tool_{i}") for i in range(11)]
    assert (await _create(client, headers, http_tools=many)).status_code == 422


async def test_tools_need_the_secret_key(client, org, public_dns, monkeypatch):
    _, headers = org
    monkeypatch.setattr(settings, "hail_provider_secret_key", "")
    resp = await _create(client, headers, http_tools=[_tool()])
    assert resp.status_code == 503
    assert "HAIL_PROVIDER_SECRET_KEY" in resp.text
    # Nothing was saved.
    listed = await client.get("/v1/agents", headers=headers)
    assert listed.json()["items"] == []
    # An agent without tools still saves.
    assert (await _create(client, headers)).status_code == 201


async def test_delete_removes_the_tools(client, org, public_dns, async_session):
    _, headers = org
    agent = (await _create(client, headers, http_tools=[_tool()])).json()
    resp = await client.delete(f"/v1/agents/{agent['id']}", headers=headers)
    assert resp.status_code == 204
    assert await _rows(async_session, agent["id"]) == []


async def test_saved_headers_stay_only_on_the_same_origin(
    client, org, public_dns, async_session
):
    """Headers left out are kept only while scheme, host and port stay the
    same: a new host must not receive the saved secrets."""
    _, headers = org
    agent = (await _create(client, headers, http_tools=[_tool()])).json()
    url = f"/v1/agents/{agent['id']}"
    no_headers = {k: v for k, v in _tool().items() if k != "headers"}

    # Same origin, new path and port spelled out: kept.
    same = {**no_headers, "url": "https://API.example.com:443/v2/orders"}
    resp = await client.patch(url, headers=headers, json={"http_tools": [same]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["http_tools"][0]["header_names"] == ["Authorization"]

    # New host, headers left out: refused, nothing changes.
    for moved_url in (
        "https://attacker.example.net/orders",
        "https://api.example.com:8443/orders",
    ):
        moved = {**no_headers, "url": moved_url}
        resp = await client.patch(url, headers=headers, json={"http_tools": [moved]})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"][0]["loc"] == ["body", "http_tools", 0, "headers"]
        assert "Send headers again" in resp.text
    (row,) = await _rows(async_session, agent["id"])
    assert row.url == "https://API.example.com:443/v2/orders"
    assert json.loads(provider_cipher().decrypt(row.headers_encrypted)) == {
        "Authorization": "Bearer customer-token"
    }

    # New host with headers given: accepted, the new values are stored.
    moved = {
        **no_headers,
        "url": "https://orders.example.org/lookup",
        "headers": {"X-Api-Key": "new-key"},
    }
    resp = await client.patch(url, headers=headers, json={"http_tools": [moved]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["http_tools"][0]["header_names"] == ["X-Api-Key"]
    (row,) = await _rows(async_session, agent["id"])
    assert json.loads(provider_cipher().decrypt(row.headers_encrypted)) == {
        "X-Api-Key": "new-key"
    }

    # A tool with no saved headers may move freely.
    plain = {**no_headers, "url": "https://orders.example.org/lookup", "headers": {}}
    await client.patch(url, headers=headers, json={"http_tools": [plain]})
    elsewhere = {**no_headers, "url": "https://other.example.com/x"}
    resp = await client.patch(url, headers=headers, json={"http_tools": [elsewhere]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["http_tools"][0]["header_names"] == []


async def test_an_idna_host_and_its_ascii_form_are_one_origin(client, org, public_dns):
    """faß.example and xn--fa-hia.example are the same server: the saved
    headers stay."""
    _, headers = org
    agent = (
        await _create(
            client, headers, http_tools=[_tool(url="https://faß.example/orders")]
        )
    ).json()
    no_headers = {k: v for k, v in _tool().items() if k != "headers"}
    same = {**no_headers, "url": "https://xn--fa-hia.example:443/orders"}
    resp = await client.patch(
        f"/v1/agents/{agent['id']}", headers=headers, json={"http_tools": [same]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["http_tools"][0]["header_names"] == ["Authorization"]


async def test_unresolvable_host_is_a_422(client, org):
    _, headers = org
    resp = await _create(
        client, headers, http_tools=[_tool(url="https://" + "a" * 64 + ".com/x")]
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "http_tools", 0, "url"]
