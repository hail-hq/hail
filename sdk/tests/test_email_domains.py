"""End-to-end client tests for the `/email-domains` surface."""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
import respx

from pydantic import ValidationError

from hail import Client, EmailDomainCreate, EmailDomainPatch
from tests.conftest import make_email_domain_response

# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #


@respx.mock
async def test_email_domains_create_hail_mail(base_url: str, api_key: str) -> None:
    payload = make_email_domain_response(kind="hail_mail")
    route = respx.post(f"{base_url}/email-domains").mock(
        return_value=httpx.Response(201, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        sd = await c.email_domains.create(
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
async def test_email_domains_create_custom_returns_dkim(
    base_url: str, api_key: str
) -> None:
    dkim = [
        {"name": "sel1._domainkey.acme.com", "value": "sel1.dkim.amazonses.com"},
        {"name": "sel2._domainkey.acme.com", "value": "sel2.dkim.amazonses.com"},
        {"name": "sel3._domainkey.acme.com", "value": "sel3.dkim.amazonses.com"},
    ]
    payload = make_email_domain_response(
        kind="custom",
        domain="acme.com",
        verification_status="pending",
        local_prefix_user=None,
        local_prefix_org=None,
        dns_records=dkim,
    )
    respx.post(f"{base_url}/email-domains").mock(
        return_value=httpx.Response(201, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        sd = await c.email_domains.create(kind="custom", domain="acme.com")
    assert sd.kind == "custom"
    assert sd.verification_status == "pending"
    assert len(sd.dns_records) == 3
    assert sd.dns_records[0].name == "sel1._domainkey.acme.com"


@respx.mock
async def test_email_domains_create_auto_idempotency_key(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/email-domains").mock(
        return_value=httpx.Response(201, json=make_email_domain_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.email_domains.create(kind="hail_mail")
    raw = route.calls.last.request.headers["Idempotency-Key"]
    UUID(raw)


# --------------------------------------------------------------------------- #
# get / list
# --------------------------------------------------------------------------- #


@respx.mock
async def test_email_domains_get(base_url: str, api_key: str) -> None:
    payload = make_email_domain_response()
    respx.get(f"{base_url}/email-domains/{payload['id']}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        sd = await c.email_domains.get(payload["id"])
    assert str(sd.id) == payload["id"]


@respx.mock
async def test_email_domains_list(base_url: str, api_key: str) -> None:
    items = [
        make_email_domain_response(),
        make_email_domain_response(kind="custom", domain="acme.com"),
    ]
    route = respx.get(f"{base_url}/email-domains").mock(
        return_value=httpx.Response(200, json={"items": items, "next_cursor": None})
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        resp = await c.email_domains.list(limit=25)
    assert len(resp.items) == 2
    assert route.calls.last.request.url.params["limit"] == "25"


# --------------------------------------------------------------------------- #
# verify / patch / delete
# --------------------------------------------------------------------------- #


@respx.mock
async def test_email_domains_verify(base_url: str, api_key: str) -> None:
    payload = make_email_domain_response(kind="custom", domain="acme.com")
    route = respx.post(f"{base_url}/email-domains/{payload['id']}/verify").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        sd = await c.email_domains.verify(payload["id"])
    assert sd.verification_status == "verified"
    assert route.calls.last.request.method == "POST"


@respx.mock
async def test_email_domains_patch(base_url: str, api_key: str) -> None:
    payload = make_email_domain_response(
        kind="hail_mail",
        domain="bob+acme@mail.hail.so",
        local_prefix_user="bob",
        local_prefix_org="acme",
    )
    route = respx.patch(f"{base_url}/email-domains/{payload['id']}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        sd = await c.email_domains.patch(payload["id"], local_prefix_user="bob")
    assert sd.domain == "bob+acme@mail.hail.so"
    body = json.loads(route.calls.last.request.content)
    assert body == {"local_prefix_user": "bob"}


@respx.mock
async def test_email_domains_delete(base_url: str, api_key: str) -> None:
    domain_id = "11111111-1111-1111-1111-111111111111"
    route = respx.delete(f"{base_url}/email-domains/{domain_id}").mock(
        return_value=httpx.Response(204)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        result = await c.email_domains.delete(domain_id)
    assert result is None
    assert route.calls.last.request.method == "DELETE"


# --------------------------------------------------------------------------- #
# Model validation
# --------------------------------------------------------------------------- #


def test_email_domain_create_custom_requires_domain() -> None:
    with pytest.raises(ValueError, match="domain is required"):
        EmailDomainCreate(kind="custom")


def test_email_domain_create_hail_mail_rejects_domain() -> None:
    with pytest.raises(ValueError, match="must be omitted"):
        EmailDomainCreate(kind="hail_mail", domain="acme.com")


def test_email_domain_create_custom_rejects_prefixes() -> None:
    with pytest.raises(ValueError, match="only valid"):
        EmailDomainCreate(kind="custom", domain="acme.com", local_prefix_user="alice")


def test_email_domain_create_invalid_prefix() -> None:
    with pytest.raises(ValueError, match="lowercase"):
        EmailDomainCreate(kind="hail_mail", local_prefix_user="Alice!")


def test_email_domain_patch_requires_at_least_one_field() -> None:
    with pytest.raises(ValueError, match="at least one"):
        EmailDomainPatch()


def test_email_domain_patch_invalid_prefix() -> None:
    # Validator lowercases before regex-matching, so use a value that
    # still fails after lowercasing (space + '!' aren't in the charset).
    with pytest.raises(ValueError, match="lowercase"):
        EmailDomainPatch(local_prefix_user="not valid!")


# --------------------------------------------------------------------------- #
# Inbound fields — webhook_secret survives model_validate
# --------------------------------------------------------------------------- #


@respx.mock
async def test_patch_returns_webhook_secret(base_url: str, api_key: str) -> None:
    """PATCH response containing webhook_secret must not be dropped by the SDK.

    Previously EmailDomainResponse had extra="ignore" semantics (Pydantic's
    default is to ignore unknown fields); adding the field makes the
    once-only secret flow through to the caller.
    """
    domain_id = "22222222-2222-2222-2222-222222222222"
    payload = {
        **make_email_domain_response(domain_id=domain_id),
        "inbound_enabled": True,
        "webhook_url": "https://hooks.example.com/hail",
        "webhook_secret": "whd_abc123",
        "forward_to": None,
        "forward_rate_per_hour": None,
    }
    respx.patch(f"{base_url}/email-domains/{domain_id}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        dom = await c.email_domains.patch(
            domain_id, webhook_url="https://hooks.example.com/hail"
        )
    assert dom.webhook_secret == "whd_abc123"
    assert dom.inbound_enabled is True
    assert dom.webhook_url == "https://hooks.example.com/hail"
    assert dom.forward_to is None
    assert dom.forward_rate_per_hour is None


def test_email_domain_patch_accepts_inbound_fields() -> None:
    p = EmailDomainPatch(
        inbound_enabled=True,
        forward_to=["ops@example.com"],
        webhook_url="https://example.com/hook",
        forward_rate_per_hour=100,
    )
    assert p.inbound_enabled is True


def test_email_domain_patch_still_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError):
        EmailDomainPatch()


def test_email_domain_response_inbound_fields_default_to_safe_values() -> None:
    """Outbound/existing responses that omit inbound fields must still parse."""
    from hail.models import EmailDomainResponse
    from uuid import uuid4
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    dom = EmailDomainResponse.model_validate(
        {
            "id": str(uuid4()),
            "organization_id": str(uuid4()),
            "kind": "hail_mail",
            "domain": "alice+acme@mail.hail.so",
            "local_prefix_user": "alice",
            "local_prefix_org": "acme",
            "verification_status": "verified",
            "dns_records": [],
            "mail_from_domain": None,
            "mail_from_status": None,
            "provider": "ses",
            "verified_at": now.isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
    )
    assert dom.inbound_enabled is False
    assert dom.forward_to is None
    assert dom.webhook_url is None
    assert dom.webhook_secret is None
    assert dom.forward_rate_per_hour is None
