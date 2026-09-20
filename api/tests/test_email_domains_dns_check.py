"""Tests for GET /email-domains/{id}/dns-check.

Monkeypatches the four Task 3 DNS helpers (``resolve_zone_ns``,
``detect_dns_provider``, ``observe_record``, ``dmarc_present``) where the
route imports them, so these tests never touch real DNS.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from hailhq.api.routes import email_domains as email_domains_routes
from hailhq.core.dns_lookup import DnsLookupError, DnsProvider
from hailhq.core.models import EmailDomain
from sqlalchemy.ext.asyncio import AsyncSession

CLOUDFLARE = DnsProvider(
    id="cloudflare",
    name="Cloudflare",
    dns_url="https://dash.cloudflare.com/?to=/:account/:zone/dns/records",
    note="Set Proxy status to DNS only for every CNAME.",
)

_RECORDS = [
    {
        "type": "CNAME",
        "name": "t._domainkey.acme.com",
        "value": "t.dkim.amazonses.com",
        "priority": None,
    },
    {
        "type": "MX",
        "name": "acme.com",
        "value": "inbound-smtp.us-east-1.amazonaws.com",
        "priority": 10,
    },
]


@pytest.fixture()
def org_id(async_session: AsyncSession):
    from .conftest import insert_org_and_key

    async def _make():
        return await insert_org_and_key(async_session)

    return _make


@pytest.fixture()
async def headers(org_id):
    org, _, plain = await org_id()
    return org, {"Authorization": f"Bearer {plain}"}


async def _make_custom_domain(
    async_session: AsyncSession, organization_id: uuid.UUID, domain: str = "acme.com"
) -> EmailDomain:
    sd = EmailDomain(
        organization_id=organization_id,
        kind="custom",
        domain=domain,
        verification_status="pending",
        dns_records=[dict(r) for r in _RECORDS],
        mail_from_domain=f"send.{domain}",
        mail_from_status="pending",
        provider="ses",
        provider_resource_id=domain,
    )
    async_session.add(sd)
    await async_session.commit()
    await async_session.refresh(sd)
    return sd


async def _make_hail_mail_domain(
    async_session: AsyncSession, organization_id: uuid.UUID
) -> EmailDomain:
    sd = EmailDomain(
        organization_id=organization_id,
        kind="hail_mail",
        domain="alice+acme@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org="acme",
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    async_session.add(sd)
    await async_session.commit()
    await async_session.refresh(sd)
    return sd


def _patch_dns(
    monkeypatch: pytest.MonkeyPatch,
    *,
    zone: str = "acme.com",
    nameservers: list[str] | None = None,
    provider: DnsProvider | None = CLOUDFLARE,
    observed: dict[str, bool] | None = None,
    dmarc: bool | set[str] = False,
) -> None:
    """Wire up the four DNS helpers the route imports.

    ``dmarc`` is either a bool (``True`` → present at the zone apex only,
    matching the old single-name tests) or an explicit set of names that
    have a ``v=DMARC1`` record — the route now calls ``dmarc_present`` once
    for the sending domain and once for the zone (finding 5: RFC 7489
    §6.6.3 checks the sending domain before falling back to the
    organisational domain).
    """
    nameservers = nameservers if nameservers is not None else ["ns1.cloudflare.com"]
    observed = observed if observed is not None else {"CNAME": True, "MX": False}
    dmarc_names: set[str] = (
        dmarc if isinstance(dmarc, set) else ({zone} if dmarc else set())
    )

    async def _resolve_zone_ns(domain: str):
        return zone, nameservers

    def _detect_dns_provider(ns: list[str]):
        assert ns == nameservers
        return provider

    async def _observe_record(record: dict) -> bool:
        return observed.get(record["type"], False)

    async def _dmarc_present(name: str) -> bool:
        return name in dmarc_names

    monkeypatch.setattr(email_domains_routes, "resolve_zone_ns", _resolve_zone_ns)
    monkeypatch.setattr(
        email_domains_routes, "detect_dns_provider", _detect_dns_provider
    )
    monkeypatch.setattr(email_domains_routes, "observe_record", _observe_record)
    monkeypatch.setattr(email_domains_routes, "dmarc_present", _dmarc_present)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


async def test_dns_check_happy_path(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, hdrs = headers
    sd = await _make_custom_domain(async_session, org)
    _patch_dns(monkeypatch, dmarc=False)

    resp = await client.get(f"/email-domains/{sd.id}/dns-check", headers=hdrs)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["zone"] == "acme.com"
    assert body["dns_provider"] == {
        "id": "cloudflare",
        "name": "Cloudflare",
        "dns_url": "https://dash.cloudflare.com/?to=/:account/:zone/dns/records",
        "note": "Set Proxy status to DNS only for every CNAME.",
    }
    assert len(body["records"]) == 2
    by_type = {r["type"]: r for r in body["records"]}
    assert by_type["CNAME"]["observed"] is True
    assert by_type["MX"]["observed"] is False
    assert body["dmarc"] == {
        "present": False,
        "suggested": {
            "type": "TXT",
            "name": "_dmarc.acme.com",
            "value": "v=DMARC1; p=none;",
            "priority": None,
        },
    }
    assert body["lookup_ok"] is True


async def test_dns_check_no_known_provider(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, hdrs = headers
    sd = await _make_custom_domain(async_session, org)
    _patch_dns(monkeypatch, nameservers=["ns1.example-registrar.net"], provider=None)

    resp = await client.get(f"/email-domains/{sd.id}/dns-check", headers=hdrs)
    assert resp.status_code == 200, resp.text
    assert resp.json()["dns_provider"] is None


# --------------------------------------------------------------------------- #
# Org scoping
# --------------------------------------------------------------------------- #


async def test_dns_check_other_org_404s(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    org_id,
) -> None:
    org, _hdrs = headers
    sd = await _make_custom_domain(async_session, org)
    _patch_dns(monkeypatch)

    _other_org, _, other_plain = await org_id()
    resp = await client.get(
        f"/email-domains/{sd.id}/dns-check",
        headers={"Authorization": f"Bearer {other_plain}"},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# hail_mail rows: no DNS work at all
# --------------------------------------------------------------------------- #


async def test_dns_check_hail_mail_does_no_dns_lookups(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, hdrs = headers
    sd = await _make_hail_mail_domain(async_session, org)

    async def _boom(*args, **kwargs):
        raise AssertionError("hail_mail rows must not perform DNS lookups")

    monkeypatch.setattr(email_domains_routes, "resolve_zone_ns", _boom)
    monkeypatch.setattr(email_domains_routes, "detect_dns_provider", _boom)
    monkeypatch.setattr(email_domains_routes, "observe_record", _boom)
    monkeypatch.setattr(email_domains_routes, "dmarc_present", _boom)

    resp = await client.get(f"/email-domains/{sd.id}/dns-check", headers=hdrs)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "dns_provider": None,
        "zone": None,
        "records": [],
        "dmarc": {"present": False, "suggested": None},
        "lookup_ok": True,
    }


# --------------------------------------------------------------------------- #
# DMARC
# --------------------------------------------------------------------------- #


async def test_dns_check_dmarc_present_has_no_suggestion(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, hdrs = headers
    sd = await _make_custom_domain(async_session, org)
    _patch_dns(monkeypatch, dmarc=True)

    resp = await client.get(f"/email-domains/{sd.id}/dns-check", headers=hdrs)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dmarc"] == {"present": True, "suggested": None}
    assert body["lookup_ok"] is True


async def test_dns_check_dmarc_absent_suggests_p_none(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, hdrs = headers
    sd = await _make_custom_domain(async_session, org)
    _patch_dns(monkeypatch, dmarc=False)

    resp = await client.get(f"/email-domains/{sd.id}/dns-check", headers=hdrs)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dmarc"] == {
        "present": False,
        "suggested": {
            "type": "TXT",
            "name": "_dmarc.acme.com",
            "value": "v=DMARC1; p=none;",
            "priority": None,
        },
    }
    assert body["lookup_ok"] is True


async def test_dns_check_dmarc_present_only_at_sending_domain(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RFC 7489 §6.6.3: a receiver checks _dmarc.<sending domain> before
    falling back to the organisational domain. A record only at the
    sending domain (not the zone apex) must still count as present."""
    org, hdrs = headers
    sd = await _make_custom_domain(async_session, org, domain="mail.acme.com")
    _patch_dns(monkeypatch, zone="acme.com", dmarc={"mail.acme.com"})

    resp = await client.get(f"/email-domains/{sd.id}/dns-check", headers=hdrs)
    assert resp.status_code == 200, resp.text
    assert resp.json()["dmarc"] == {"present": True, "suggested": None}


async def test_dns_check_dmarc_present_only_at_zone_apex(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, hdrs = headers
    sd = await _make_custom_domain(async_session, org, domain="mail.acme.com")
    _patch_dns(monkeypatch, zone="acme.com", dmarc={"acme.com"})

    resp = await client.get(f"/email-domains/{sd.id}/dns-check", headers=hdrs)
    assert resp.status_code == 200, resp.text
    assert resp.json()["dmarc"] == {"present": True, "suggested": None}


async def test_dns_check_dmarc_absent_at_both_names(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, hdrs = headers
    sd = await _make_custom_domain(async_session, org, domain="mail.acme.com")
    _patch_dns(monkeypatch, zone="acme.com", dmarc=set())

    resp = await client.get(f"/email-domains/{sd.id}/dns-check", headers=hdrs)
    assert resp.status_code == 200, resp.text
    assert resp.json()["dmarc"] == {
        "present": False,
        "suggested": {
            "type": "TXT",
            "name": "_dmarc.acme.com",
            "value": "v=DMARC1; p=none;",
            "priority": None,
        },
    }


# --------------------------------------------------------------------------- #
# No zone at all
# --------------------------------------------------------------------------- #


async def test_dns_check_no_zone_returns_nulls(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, hdrs = headers
    sd = await _make_custom_domain(async_session, org, domain="ghost.invalid")

    async def _resolve_zone_ns(domain: str):
        return "", []

    async def _observe_record(record: dict) -> bool:
        return False

    def _boom_detect(*args, **kwargs):
        raise AssertionError("no zone means no provider detection")

    async def _boom_dmarc(*args, **kwargs):
        raise AssertionError("no zone means no DMARC lookup")

    monkeypatch.setattr(email_domains_routes, "resolve_zone_ns", _resolve_zone_ns)
    monkeypatch.setattr(email_domains_routes, "observe_record", _observe_record)
    monkeypatch.setattr(email_domains_routes, "detect_dns_provider", _boom_detect)
    monkeypatch.setattr(email_domains_routes, "dmarc_present", _boom_dmarc)

    resp = await client.get(f"/email-domains/{sd.id}/dns-check", headers=hdrs)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["zone"] is None
    assert body["dns_provider"] is None
    assert body["dmarc"] == {"present": False, "suggested": None}
    # A completed walk that simply found no NS answer is not a lookup
    # failure — every lookup that ran, ran to completion.
    assert body["lookup_ok"] is True


# --------------------------------------------------------------------------- #
# Lookup failure / deadline: degraded shape, never a 5xx (findings 1-3)
# --------------------------------------------------------------------------- #


async def test_dns_check_doh_failure_returns_degraded_shape(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, hdrs = headers
    sd = await _make_custom_domain(async_session, org)

    async def _boom_zone(domain: str):
        raise DnsLookupError("boom")

    async def _boom_observe(record: dict) -> bool:
        raise DnsLookupError("boom")

    def _boom_detect(*args, **kwargs):
        raise AssertionError("a failed lookup must not reach provider detection")

    async def _boom_dmarc(*args, **kwargs):
        raise AssertionError("a failed lookup must not reach DMARC lookup")

    monkeypatch.setattr(email_domains_routes, "resolve_zone_ns", _boom_zone)
    monkeypatch.setattr(email_domains_routes, "observe_record", _boom_observe)
    monkeypatch.setattr(email_domains_routes, "detect_dns_provider", _boom_detect)
    monkeypatch.setattr(email_domains_routes, "dmarc_present", _boom_dmarc)

    resp = await client.get(f"/email-domains/{sd.id}/dns-check", headers=hdrs)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lookup_ok"] is False
    assert body["dns_provider"] is None
    assert body["zone"] is None
    assert all(r["observed"] is False for r in body["records"])
    assert len(body["records"]) == len(_RECORDS)
    # No _dmarc.<public suffix> suggestion off unreliable data.
    assert body["dmarc"] == {"present": False, "suggested": None}


async def test_dns_check_deadline_returns_degraded_shape(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, hdrs = headers
    sd = await _make_custom_domain(async_session, org)
    monkeypatch.setattr(email_domains_routes, "_DNS_CHECK_DEADLINE_S", 0.05)

    async def _slow_zone(domain: str):
        await asyncio.sleep(1)
        return "acme.com", ["ns1.cloudflare.com"]

    async def _slow_observe(record: dict) -> bool:
        await asyncio.sleep(1)
        return False

    monkeypatch.setattr(email_domains_routes, "resolve_zone_ns", _slow_zone)
    monkeypatch.setattr(email_domains_routes, "observe_record", _slow_observe)

    resp = await client.get(f"/email-domains/{sd.id}/dns-check", headers=hdrs)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lookup_ok"] is False
    assert body["dns_provider"] is None
    assert body["zone"] is None
    assert all(r["observed"] is False for r in body["records"])
    assert body["dmarc"] == {"present": False, "suggested": None}


async def test_dns_check_hail_mail_lookup_ok_true(
    client: httpx.AsyncClient,
    headers: tuple,
    async_session: AsyncSession,
) -> None:
    """hail_mail rows run no lookups at all, so lookup_ok is trivially true."""
    org, hdrs = headers
    sd = await _make_hail_mail_domain(async_session, org)

    resp = await client.get(f"/email-domains/{sd.id}/dns-check", headers=hdrs)
    assert resp.status_code == 200, resp.text
    assert resp.json()["lookup_ok"] is True
