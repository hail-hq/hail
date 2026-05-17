"""End-to-end client tests for the `/sender-domains` surface."""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
import respx

from hail import Client, SenderDomainCreate, SenderDomainPatch
from tests.conftest import make_sender_domain_response

# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #


@respx.mock
async def test_sender_domains_create_hail_mail(base_url: str, api_key: str) -> None:
    payload = make_sender_domain_response(kind="hail_mail")
    route = respx.post(f"{base_url}/sender-domains").mock(
        return_value=httpx.Response(201, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        sd = await c.sender_domains.create(
            kind="hail_mail",
            local_prefix_user="alice",
            local_prefix_org="acme",
        )
    assert sd.kind == "hail_mail"
    assert sd.domain == "alice+acme@mail.hail.so"

    body = json.loads(route.calls.last.request.content)
    assert body == {
        "kind": "hail_mail",
        "local_prefix_user": "alice",
        "local_prefix_org": "acme",
    }


@respx.mock
async def test_sender_domains_create_custom_returns_dkim(
    base_url: str, api_key: str
) -> None:
    dkim = [
        {"name": "sel1._domainkey.acme.com", "value": "sel1.dkim.amazonses.com"},
        {"name": "sel2._domainkey.acme.com", "value": "sel2.dkim.amazonses.com"},
        {"name": "sel3._domainkey.acme.com", "value": "sel3.dkim.amazonses.com"},
    ]
    payload = make_sender_domain_response(
        kind="custom",
        domain="acme.com",
        verification_status="pending",
        local_prefix_user=None,
        local_prefix_org=None,
        dkim_records=dkim,
    )
    respx.post(f"{base_url}/sender-domains").mock(
        return_value=httpx.Response(201, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        sd = await c.sender_domains.create(kind="custom", domain="acme.com")
    assert sd.kind == "custom"
    assert sd.verification_status == "pending"
    assert len(sd.dkim_records) == 3
    assert sd.dkim_records[0].name == "sel1._domainkey.acme.com"


@respx.mock
async def test_sender_domains_create_auto_idempotency_key(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/sender-domains").mock(
        return_value=httpx.Response(201, json=make_sender_domain_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.sender_domains.create(kind="hail_mail")
    raw = route.calls.last.request.headers["Idempotency-Key"]
    UUID(raw)


# --------------------------------------------------------------------------- #
# get / list
# --------------------------------------------------------------------------- #


@respx.mock
async def test_sender_domains_get(base_url: str, api_key: str) -> None:
    payload = make_sender_domain_response()
    respx.get(f"{base_url}/sender-domains/{payload['id']}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        sd = await c.sender_domains.get(payload["id"])
    assert str(sd.id) == payload["id"]


@respx.mock
async def test_sender_domains_list(base_url: str, api_key: str) -> None:
    items = [
        make_sender_domain_response(),
        make_sender_domain_response(kind="custom", domain="acme.com"),
    ]
    route = respx.get(f"{base_url}/sender-domains").mock(
        return_value=httpx.Response(200, json={"items": items, "next_cursor": None})
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        resp = await c.sender_domains.list(limit=25)
    assert len(resp.items) == 2
    assert route.calls.last.request.url.params["limit"] == "25"


# --------------------------------------------------------------------------- #
# verify / patch / delete
# --------------------------------------------------------------------------- #


@respx.mock
async def test_sender_domains_verify(base_url: str, api_key: str) -> None:
    payload = make_sender_domain_response(kind="custom", domain="acme.com")
    route = respx.post(f"{base_url}/sender-domains/{payload['id']}/verify").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        sd = await c.sender_domains.verify(payload["id"])
    assert sd.verification_status == "verified"
    assert route.calls.last.request.method == "POST"


@respx.mock
async def test_sender_domains_patch(base_url: str, api_key: str) -> None:
    payload = make_sender_domain_response(
        kind="hail_mail",
        domain="bob+acme@mail.hail.so",
        local_prefix_user="bob",
        local_prefix_org="acme",
    )
    route = respx.patch(f"{base_url}/sender-domains/{payload['id']}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        sd = await c.sender_domains.patch(payload["id"], local_prefix_user="bob")
    assert sd.domain == "bob+acme@mail.hail.so"
    body = json.loads(route.calls.last.request.content)
    assert body == {"local_prefix_user": "bob"}


@respx.mock
async def test_sender_domains_delete(base_url: str, api_key: str) -> None:
    domain_id = "11111111-1111-1111-1111-111111111111"
    route = respx.delete(f"{base_url}/sender-domains/{domain_id}").mock(
        return_value=httpx.Response(204)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        result = await c.sender_domains.delete(domain_id)
    assert result is None
    assert route.calls.last.request.method == "DELETE"


# --------------------------------------------------------------------------- #
# Model validation
# --------------------------------------------------------------------------- #


def test_sender_domain_create_custom_requires_domain() -> None:
    with pytest.raises(ValueError, match="domain is required"):
        SenderDomainCreate(kind="custom")


def test_sender_domain_create_hail_mail_rejects_domain() -> None:
    with pytest.raises(ValueError, match="must be omitted"):
        SenderDomainCreate(kind="hail_mail", domain="acme.com")


def test_sender_domain_create_custom_rejects_prefixes() -> None:
    with pytest.raises(ValueError, match="only valid"):
        SenderDomainCreate(kind="custom", domain="acme.com", local_prefix_user="alice")


def test_sender_domain_create_invalid_prefix() -> None:
    with pytest.raises(ValueError, match="lowercase"):
        SenderDomainCreate(kind="hail_mail", local_prefix_user="Alice!")


def test_sender_domain_patch_requires_at_least_one_field() -> None:
    with pytest.raises(ValueError, match="at least one"):
        SenderDomainPatch()


def test_sender_domain_patch_invalid_prefix() -> None:
    # Validator lowercases before regex-matching, so use a value that
    # still fails after lowercasing (space + '!' aren't in the charset).
    with pytest.raises(ValueError, match="lowercase"):
        SenderDomainPatch(local_prefix_user="not valid!")
