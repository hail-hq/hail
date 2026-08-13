"""Integration tests for /email-domains routes."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from hailhq.core.config import settings
from hailhq.core.hail_mail import org_prefix_from_id
from hailhq.core.models import ApiKey, EmailDomain
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import insert_org_and_key

# --------------------------------------------------------------------------- #
# POST /email-domains
# --------------------------------------------------------------------------- #


async def test_post_custom_domain_returns_dkim_records(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "custom"
    assert body["domain"] == "acme.com"
    assert body["verification_status"] == "pending"
    # 3 from provider (1 DKIM CNAME + MAIL FROM MX + SPF TXT) + 1 receive MX
    assert len(body["dns_records"]) == 4
    email_mock.create_identity.assert_awaited_once_with("acme.com")


async def test_post_custom_returns_dns_records_with_mail_from(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    resp = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "dns_records" in body
    types = {r["type"] for r in body["dns_records"]}
    assert {"CNAME", "MX", "TXT"} <= types
    assert body["mail_from_domain"] == "send.acme.com"
    assert body["mail_from_status"] == "pending"


async def test_post_custom_domain_lowercases_and_validates(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "ACME.COM"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["domain"] == "acme.com"


async def test_post_custom_rejects_invalid_dns_name(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "not a domain"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422


async def test_post_custom_requires_domain(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "custom"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422
    assert "domain is required" in resp.text


async def test_post_hail_mail_uses_explicit_prefixes(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "alice",
            "local_prefix_org": "acme",
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "hail_mail"
    assert body["domain"] == "alice+acme@mail.hail.so"
    assert body["local_prefix_user"] == "alice"
    assert body["local_prefix_org"] == "acme"
    assert body["verification_status"] == "verified"
    assert body["dns_records"] == []


async def test_post_hail_mail_user_default_with_derived_org_prefix(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With only HAIL_MAIL_DEFAULT_USER_PREFIX set, the user prefix comes from
    the env default and the org prefix is derived per-org from the org id."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "admin")
    org_id, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "hail_mail"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    expected_org = org_prefix_from_id(org_id)
    assert body["local_prefix_user"] == "admin"
    assert body["local_prefix_org"] == expected_org
    assert body["domain"] == f"admin+{expected_org}@mail.hail.so"


async def test_post_hail_mail_uses_hail_mail_from_env(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HAIL_MAIL_FROM is the single-var shortcut for self-host setups."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_from", "alice+acme@mail.hail.so")
    # User default empty — HAIL_MAIL_FROM should win without it.
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "hail_mail"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["domain"] == "alice+acme@mail.hail.so"
    assert body["local_prefix_user"] == "alice"
    assert body["local_prefix_org"] == "acme"


async def test_post_hail_mail_from_overrides_default_prefix_vars(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HAIL_MAIL_FROM wins over HAIL_MAIL_DEFAULT_USER_PREFIX (highest env precedence)."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_from", "alice+acme@mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "ignored")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "hail_mail"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["domain"] == "alice+acme@mail.hail.so"


async def test_post_hail_mail_body_prefixes_override_hail_mail_from(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body prefixes win over HAIL_MAIL_FROM (highest precedence overall)."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_from", "alice+acme@mail.hail.so")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "bob",
            "local_prefix_org": "beta",
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["domain"] == "bob+beta@mail.hail.so"


async def test_post_hail_mail_from_rejects_domain_mismatch(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HAIL_MAIL_FROM's domain must match HAIL_MAIL_BASE_DOMAIN."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_from", "alice+acme@wrong-domain.com")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "hail_mail"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 503
    assert "HAIL_MAIL_BASE_DOMAIN" in resp.text


async def test_post_hail_mail_from_rejects_missing_plus(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HAIL_MAIL_FROM must contain ``+`` between the user and org prefixes."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_from", "admin@mail.hail.so")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "hail_mail"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 503
    assert "<user>+<org>" in resp.text


async def test_post_hail_mail_from_rejects_invalid_prefix(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HAIL_MAIL_FROM prefixes must match the local-prefix regex."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    # Uppercase + special chars — invalid even after no lowercasing in env parse.
    monkeypatch.setattr(settings, "hail_mail_from", "Alice!+acme@mail.hail.so")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "hail_mail"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 503


async def test_post_hail_mail_503_when_prefix_missing_and_no_default(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "hail_mail"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    # The org prefix is always derivable now, so only the user prefix can be
    # missing — that still yields a 503.
    assert resp.status_code == 503
    assert "local_prefix_user" in resp.text


async def test_post_hail_mail_rejects_invalid_prefix(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "Alice!",  # invalid: uppercase + '!'
            "local_prefix_org": "acme",
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422


async def test_post_hail_mail_rejects_domain_in_body(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "hail_mail", "domain": "acme.com"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422


async def test_post_hail_mail_503_when_base_domain_unset(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "alice",
            "local_prefix_org": "acme",
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 503
    assert "HAIL_MAIL_BASE_DOMAIN" in resp.text


async def test_post_custom_rejects_prefix_fields(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={
            "kind": "custom",
            "domain": "acme.com",
            "local_prefix_user": "alice",
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422


async def test_patch_hail_mail_updates_prefixes_and_domain(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "u1",
            "local_prefix_org": "o1",
        },
        headers=headers,
    )
    domain_id = created.json()["id"]

    resp = await client.patch(
        f"/email-domains/{domain_id}",
        json={"local_prefix_user": "alice", "local_prefix_org": "acme"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["domain"] == "alice+acme@mail.hail.so"
    assert body["local_prefix_user"] == "alice"
    assert body["local_prefix_org"] == "acme"


async def test_patch_hail_mail_partial_update_keeps_other_prefix(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "alice",
            "local_prefix_org": "acme",
        },
        headers=headers,
    )
    domain_id = created.json()["id"]

    resp = await client.patch(
        f"/email-domains/{domain_id}",
        json={"local_prefix_user": "bob"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["domain"] == "bob+acme@mail.hail.so"


async def test_patch_on_custom_domain_returns_422(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    resp = await client.patch(
        f"/email-domains/{created.json()['id']}",
        json={"local_prefix_user": "alice"},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "hail_mail" in resp.text


async def test_patch_unknown_returns_404(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    resp = await client.patch(
        "/email-domains/00000000-0000-0000-0000-000000000000",
        json={"local_prefix_user": "alice"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 404


async def test_post_duplicate_custom_domain_returns_409(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    r1 = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    assert r2.status_code == 409
    assert "already registered" in r2.text


async def test_post_custom_domain_duplicate_across_orgs_returns_409(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
    email_mock: AsyncMock,
) -> None:
    """Custom sender domains are globally unique — a second org attempting to
    register a domain already owned by org A must receive 409, not 201.
    Without the global partial index a second org could ride org A's SES
    verification and intercept their inbound mail."""
    _, _, plain_a = org_and_key
    _, _, plain_b = await insert_org_and_key(async_session)

    r1 = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers={"Authorization": f"Bearer {plain_a}"},
    )
    assert r1.status_code == 201, r1.text

    r2 = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers={"Authorization": f"Bearer {plain_b}"},
    )
    assert r2.status_code == 409, r2.text
    assert "already registered" in r2.text


async def test_post_hail_mail_default_org_prefix_is_per_org_not_shared(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two orgs that both omit prefixes must NOT collide on a shared default.

    Reproduces the multi-tenant bug: the org prefix used to come from a
    deploy-wide constant (HAIL_MAIL_DEFAULT_ORG_PREFIX), so every org
    resolved to the same ``admin+selfhost@…`` address — the first org claimed
    it and every later org hit the global-unique index (409). The org prefix
    is now derived per-org from the organization id.
    """
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "admin")
    org_a, _, plain_a = org_and_key
    org_b, _, plain_b = await insert_org_and_key(async_session)
    body = {"kind": "hail_mail"}

    r1 = await client.post(
        "/email-domains", json=body, headers={"Authorization": f"Bearer {plain_a}"}
    )
    r2 = await client.post(
        "/email-domains", json=body, headers={"Authorization": f"Bearer {plain_b}"}
    )
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    d1, d2 = r1.json()["domain"], r2.json()["domain"]
    assert d1 != d2, "two orgs must not share one hail-mail address"
    assert d1 == f"admin+{org_prefix_from_id(org_a)}@mail.hail.so"
    assert d2 == f"admin+{org_prefix_from_id(org_b)}@mail.hail.so"


async def test_post_hail_mail_default_org_prefix_is_deterministic(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-provisioning the same org with no prefixes is idempotent — the
    derived org prefix is a stable function of the organization id, so a
    retry reproduces the same address rather than minting a second one."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "admin")
    _, _, plain = org_and_key
    body = {"kind": "hail_mail"}

    r1 = await client.post(
        "/email-domains", json=body, headers={"Authorization": f"Bearer {plain}"}
    )
    assert r1.status_code == 201, r1.text
    first = r1.json()["domain"]
    r2 = await client.post(
        "/email-domains", json=body, headers={"Authorization": f"Bearer {plain}"}
    )
    # Same org, same derived address → the second POST conflicts with itself
    # (409) rather than producing a different address.
    assert r2.status_code == 409, r2.text
    assert first.startswith("admin+")


async def test_post_hail_mail_duplicate_across_orgs_returns_409(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hail-mail address is globally unique — inbound routing matches on
    (local_prefix_user, local_prefix_org) with no org scoping, so org B
    registering org A's address would intercept org A's mail."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain_a = org_and_key
    _, _, plain_b = await insert_org_and_key(async_session)
    body = {
        "kind": "hail_mail",
        "local_prefix_user": "alice",
        "local_prefix_org": "acme",
    }
    r1 = await client.post(
        "/email-domains",
        json=body,
        headers={"Authorization": f"Bearer {plain_a}"},
    )
    assert r1.status_code == 201, r1.text
    r2 = await client.post(
        "/email-domains",
        json=body,
        headers={"Authorization": f"Bearer {plain_b}"},
    )
    assert r2.status_code == 409, r2.text
    assert "already registered" in r2.text


# --------------------------------------------------------------------------- #
# GET /email-domains
# --------------------------------------------------------------------------- #


async def test_list_email_domains_is_org_scoped(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "beta.com"},
        headers=headers,
    )

    resp = await client.get("/email-domains", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert {r["domain"] for r in items} == {"acme.com", "beta.com"}

    # Foreign org's keys should see none of these.
    _, _, other_plain = await insert_org_and_key(async_session)
    other = await client.get(
        "/email-domains", headers={"Authorization": f"Bearer {other_plain}"}
    )
    assert other.status_code == 200
    assert other.json()["items"] == []


# --------------------------------------------------------------------------- #
# GET /email-domains — default_from
#
# The field answers "what does a send with no `from` go out as?", so every
# case below is paired with the POST /emails behaviour it predicts.
# --------------------------------------------------------------------------- #


async def _verify(client: httpx.AsyncClient, headers: dict, domain: str) -> None:
    created = await client.post(
        "/email-domains", json={"kind": "custom", "domain": domain}, headers=headers
    )
    assert created.status_code == 201
    await client.post(f"/email-domains/{created.json()['id']}/verify", headers=headers)


async def test_default_from_is_the_single_verified_sender(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _verify(client, headers, "acme.com")

    resp = await client.get("/email-domains", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["default_from"] == "noreply@acme.com"


async def test_default_from_is_null_with_several_verified_senders(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    """Null mirrors the 422 a ``from``-less POST /emails now returns."""
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _verify(client, headers, "acme.com")
    await _verify(client, headers, "mail.acme.io")

    resp = await client.get("/email-domains", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["default_from"] is None


async def test_default_from_shows_the_unminted_hail_mail_address(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no rows at all the address exists nowhere else until a send."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "admin")
    monkeypatch.setattr(settings, "hail_mail_from", "")
    org_id, _, plain = org_and_key

    resp = await client.get(
        "/email-domains", headers={"Authorization": f"Bearer {plain}"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["default_from"] == f"admin+{org_prefix_from_id(org_id)}@mail.hail.so"


async def test_default_from_is_null_while_a_custom_domain_is_pending(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending row blocks auto-mint, so there is no default to report."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "admin")
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "pending.com"},
        headers=headers,
    )
    assert created.status_code == 201  # left unverified

    resp = await client.get("/email-domains", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["default_from"] is None


async def test_default_from_is_null_when_hail_mail_is_unconfigured(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured deployment must not 503 a plain list read."""
    monkeypatch.setattr(settings, "hail_mail_base_domain", "")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "")
    monkeypatch.setattr(settings, "hail_mail_from", "")
    _, _, plain = org_and_key

    resp = await client.get(
        "/email-domains", headers={"Authorization": f"Bearer {plain}"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["default_from"] is None


# --------------------------------------------------------------------------- #
# POST /email-domains/{id}/verify
# --------------------------------------------------------------------------- #


async def test_verify_flips_status_from_pending_to_verified(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hailhq.api.routes import email_domains as mod

    monkeypatch.setattr(mod, "resolve_mx", AsyncMock(return_value=[]))
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    assert created.status_code == 201
    domain_id = created.json()["id"]

    resp = await client.post(f"/email-domains/{domain_id}/verify", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["verification_status"] == "verified"
    assert body["verified_at"] is not None


async def test_verify_custom_domain_auto_enables_inbound(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hailhq.api.routes import email_domains as mod

    monkeypatch.setattr(mod, "resolve_mx", AsyncMock(return_value=[]))
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains", json={"kind": "custom", "domain": "acme.com"}, headers=headers
    )
    domain_id = created.json()["id"]
    # email_mock.get_identity is configured by the test fixture to return a
    # verified ProviderIdentity; mirror the existing verify test's setup.
    resp = await client.post(f"/email-domains/{domain_id}/verify", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verification_status"] == "verified"
    assert body["inbound_enabled"] is True


async def test_verify_hail_mail_is_a_noop(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
    email_mock: AsyncMock,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "alice",
            "local_prefix_org": "acme",
        },
        headers=headers,
    )
    domain_id = created.json()["id"]
    resp = await client.post(f"/email-domains/{domain_id}/verify", headers=headers)
    assert resp.status_code == 200
    # Should not have touched SES.
    email_mock.get_identity.assert_not_called()


async def test_verify_returns_404_when_identity_vanished(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    async_session: AsyncSession,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    domain_id = created.json()["id"]
    email_mock.get_identity.side_effect = LookupError("missing")
    resp = await client.post(f"/email-domains/{domain_id}/verify", headers=headers)
    assert resp.status_code == 404

    # And the row should be marked failed so subsequent sends fail fast.
    sd = (
        await async_session.execute(
            select(EmailDomain).where(EmailDomain.domain == "acme.com")
        )
    ).scalar_one()
    assert sd.verification_status == "failed"


# --------------------------------------------------------------------------- #
# DELETE /email-domains/{id}
# --------------------------------------------------------------------------- #


async def test_delete_custom_calls_provider_and_deletes_row(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    async_session: AsyncSession,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    domain_id = created.json()["id"]

    resp = await client.delete(f"/email-domains/{domain_id}", headers=headers)
    assert resp.status_code == 204
    email_mock.delete_identity.assert_awaited_once_with("acme.com")
    remaining = (await async_session.execute(select(EmailDomain))).scalars().all()
    assert remaining == []


async def test_delete_hail_mail_does_not_touch_provider(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
    email_mock: AsyncMock,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "alice",
            "local_prefix_org": "acme",
        },
        headers=headers,
    )
    domain_id = created.json()["id"]
    resp = await client.delete(f"/email-domains/{domain_id}", headers=headers)
    assert resp.status_code == 204
    email_mock.delete_identity.assert_not_called()


async def test_delete_unknown_returns_404(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    resp = await client.delete(
        "/email-domains/00000000-0000-0000-0000-000000000000",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 404


async def test_delete_with_linked_emails_returns_409_and_skips_provider(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    async_session: AsyncSession,
) -> None:
    """Sender with sent emails returns 409, leaves DB+SES untouched.

    ``emails.email_domain_id`` is ``ON DELETE RESTRICT`` — without the
    pre-check the commit would raise IntegrityError into the caller as a
    500, AND the SES identity would already be gone, leaving a row that
    points at nothing. Verify the pre-check fires first.
    """
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}

    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    domain_id = created.json()["id"]
    await async_session.execute(
        EmailDomain.__table__.update()
        .where(EmailDomain.id == uuid.UUID(domain_id))
        .values(verification_status="verified")
    )
    await async_session.commit()

    sent = await client.post(
        "/emails",
        json={
            "from": "noreply@acme.com",
            "to": ["dest@example.com"],
            "subject": "hi",
            "body_text": "hello",
            "recipient_consent": True,
        },
        headers=headers,
    )
    assert sent.status_code == 201, sent.text

    email_mock.delete_identity.reset_mock()

    resp = await client.delete(f"/email-domains/{domain_id}", headers=headers)
    assert resp.status_code == 409, resp.text
    assert "linked emails" in resp.json()["detail"]
    email_mock.delete_identity.assert_not_called()

    # Row is still there.
    still = (
        await async_session.execute(
            select(EmailDomain).where(EmailDomain.id == uuid.UUID(domain_id))
        )
    ).scalar_one_or_none()
    assert still is not None


async def test_delete_succeeds_even_if_provider_delete_fails(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    async_session: AsyncSession,
) -> None:
    """SES delete failure logs a warning but doesn't fail the request.

    The DB row is the source of truth; an orphaned SES identity is benign
    and operators can prune it manually. Reversed the order so a provider
    blip can't leave the DB pointing at a vanished identity (the old order
    deleted SES first and 502'd, leaking state both ways).
    """
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}

    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    domain_id = created.json()["id"]

    email_mock.delete_identity.side_effect = RuntimeError("ses unavailable")

    resp = await client.delete(f"/email-domains/{domain_id}", headers=headers)
    assert resp.status_code == 204
    email_mock.delete_identity.assert_awaited_once_with("acme.com")

    remaining = (await async_session.execute(select(EmailDomain))).scalars().all()
    assert remaining == []


# --------------------------------------------------------------------------- #
# Audit logging on mutations
# --------------------------------------------------------------------------- #


async def test_email_domain_mutations_write_audit_log(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every mutating handler writes one audit_log row.

    A managed-cloud admin needs to be able to answer "who renamed our
    hail-mail address?" / "who deleted the verified custom domain?" —
    so create, patch, verify, and delete all write a row.
    """
    from hailhq.core.models import AuditLog

    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}

    # create (hail_mail flavor)
    created = await client.post(
        "/email-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "alice",
            "local_prefix_org": "acme",
        },
        headers=headers,
    )
    assert created.status_code == 201
    domain_id = created.json()["id"]

    # patch
    patch = await client.patch(
        f"/email-domains/{domain_id}",
        json={"local_prefix_user": "bob"},
        headers=headers,
    )
    assert patch.status_code == 200

    # verify (hail_mail is a no-op server-side but still audit-logged)
    verify = await client.post(f"/email-domains/{domain_id}/verify", headers=headers)
    assert verify.status_code == 200

    # delete
    delete = await client.delete(f"/email-domains/{domain_id}", headers=headers)
    assert delete.status_code == 204

    actions = (
        (
            await async_session.execute(
                select(AuditLog.action)
                .where(AuditLog.resource_type == "email_domain")
                .order_by(AuditLog.occurred_at.asc())
            )
        )
        .scalars()
        .all()
    )
    # verify hail_mail short-circuits SES but still writes an audit row
    assert actions == [
        "email_domain.create",
        "email_domain.patch",
        "email_domain.verify",
        "email_domain.delete",
    ]


async def test_email_domain_custom_create_writes_audit_log(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
) -> None:
    """Custom-kind create also writes audit log (separate code path from hail_mail)."""
    from hailhq.core.models import AuditLog

    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}

    resp = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    assert resp.status_code == 201

    row = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "email_domain.create")
        )
    ).scalar_one()
    assert row.resource_type == "email_domain"
    assert row.payload["kind"] == "custom"
    assert row.payload["domain"] == "acme.com"


@pytest.mark.asyncio
async def test_patch_duplicate_prefix_returns_409(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    r1 = await client.post(
        "/email-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "a",
            "local_prefix_org": "acme",
        },
        headers=headers,
    )
    r2 = await client.post(
        "/email-domains",
        json={
            "kind": "hail_mail",
            "local_prefix_user": "b",
            "local_prefix_org": "acme",
        },
        headers=headers,
    )
    assert r1.status_code == 201 and r2.status_code == 201

    resp = await client.patch(
        f"/email-domains/{r2.json()['id']}",
        json={"local_prefix_user": "a"},  # collides with r1's address
        headers=headers,
    )
    assert resp.status_code == 409


async def test_duplicate_custom_domain_direct_insert_raises_integrity_error(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    async_session: AsyncSession,
) -> None:
    """Custom domains are globally unique at the DB level.  Since 0017,
    directly inserting a second kind='custom' row for the same domain (even
    under a different org_id) must raise IntegrityError.  This is the DB-layer
    proof of the security property: no cross-org SES identity sharing is
    possible for custom domains."""
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    assert created.status_code == 201, created.text

    # Attempting a direct DB insert for a different org must violate the
    # global partial unique index email_domains_custom_domain_global_uq.
    other_org_id = uuid.uuid4()
    async_session.add(
        EmailDomain(
            organization_id=other_org_id,
            kind="custom",
            domain="acme.com",
            verification_status="pending",
            dns_records=[],
            provider="ses",
        )
    )
    import pytest as _pytest

    with _pytest.raises(
        SAIntegrityError, match="email_domains_custom_domain_global_uq"
    ):
        await async_session.commit()


_ = ApiKey  # re-exposed for type hint in fixtures


# --------------------------------------------------------------------------- #
# GET /email-domains/check-domain
# --------------------------------------------------------------------------- #


async def test_check_domain_free_suggests_apex(
    client: httpx.AsyncClient, org_and_key: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hailhq.api.routes import email_domains as mod

    monkeypatch.setattr(mod, "resolve_mx", AsyncMock(return_value=[]))
    _, _, plain = org_and_key
    resp = await client.get(
        "/email-domains/check-domain",
        params={"domain": "acme.com"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["in_use"] is False
    assert body["suggested_domain"] == "acme.com"


async def test_check_domain_in_use_suggests_prefix(
    client: httpx.AsyncClient, org_and_key: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hailhq.api.routes import email_domains as mod

    monkeypatch.setattr(
        mod, "resolve_mx", AsyncMock(return_value=["aspmx.l.google.com"])
    )
    _, _, plain = org_and_key
    resp = await client.get(
        "/email-domains/check-domain",
        params={"domain": "acme.com"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    body = resp.json()
    assert body["in_use"] is True
    assert body["existing_mx"] == ["aspmx.l.google.com"]
    assert body["suggested_domain"] == "inbox.acme.com"


async def test_patch_enables_inbound_without_forward_to(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
    email_mock: AsyncMock,
) -> None:
    """Receiving can be turned on webhook-only — no forward_to required."""
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains", json={"kind": "custom", "domain": "acme.com"}, headers=headers
    )
    domain_id = created.json()["id"]
    resp = await client.patch(
        f"/email-domains/{domain_id}",
        json={"inbound_enabled": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["inbound_enabled"] is True


# --------------------------------------------------------------------------- #
# Task 5: receive MX in record list + receive_ready on verify
# --------------------------------------------------------------------------- #


async def test_custom_domain_record_list_includes_receive_mx(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "aws_region", "eu-west-1")
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    records = resp.json()["dns_records"]
    mx = [r for r in records if r["type"] == "MX" and r["name"] == "acme.com"]
    assert mx and mx[0]["value"] == "inbound-smtp.eu-west-1.amazonaws.com"
    assert mx[0]["priority"] == 10


async def test_verify_receive_ready_is_true_when_mx_matches(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hailhq.api.routes import email_domains as mod

    monkeypatch.setattr(settings, "aws_region", "eu-west-1")
    monkeypatch.setattr(
        mod,
        "resolve_mx",
        AsyncMock(return_value=["inbound-smtp.eu-west-1.amazonaws.com"]),
    )
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    domain_id = created.json()["id"]
    resp = await client.post(f"/email-domains/{domain_id}/verify", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["receive_ready"] is True


async def test_verify_receive_ready_is_none_when_dns_lookup_raises(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS failure after a successful verify must not 500 — degrades to None.

    The verified status is already committed when resolve_mx is called; a DoH
    timeout or provider outage must not retroactively surface as a 500 to the
    caller.  receive_ready should be None and verification_status 'verified'.
    """
    from hailhq.api.routes import email_domains as mod

    monkeypatch.setattr(
        mod, "resolve_mx", AsyncMock(side_effect=RuntimeError("dns down"))
    )
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    domain_id = created.json()["id"]

    resp = await client.post(f"/email-domains/{domain_id}/verify", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verification_status"] == "verified"
    assert body["receive_ready"] is None
