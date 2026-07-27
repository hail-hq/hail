"""Integration tests for the billing gate + account_credits ledger.

Covers:
  * the uniform 402 gate on POST /v1/calls (per-user apikey path)
  * the shared-key (`HAIL_API_KEY`) bypass path via the ``self-hosted`` sentinel org
  * `AccountCredit` CHECK constraints
  * OSS fallback when the auth backend's apikey table is absent
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from hailhq.api import deps as deps_module
from hailhq.api.deps import SELF_HOSTED_ORG_ID
from hailhq.core.models import AccountCredit
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import insert_org_and_key

_DEFAULT_BODY = {
    "to": "+14155559999",
    "system_prompt": "hi",
    "recipient_consent": True,
}


# --------------------------------------------------------------------------- #
# Per-user apikey path
# --------------------------------------------------------------------------- #


async def test_post_calls_402_per_user_key_zero_balance(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
) -> None:
    """Per-user key + zero balance returns 402, regardless of phone numbers."""
    _, _, plain = await insert_org_and_key(
        async_session,
        org_slug="broke",
        initial_credit_cents=0,
    )
    resp = await client.post(
        "/calls",
        json=_DEFAULT_BODY,
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 402
    assert "credits" in resp.json()["detail"].lower()


async def test_post_calls_succeeds_per_user_key_positive_balance(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    add_phone_number,
) -> None:
    """Per-user key + positive balance + active phone number → call queued."""
    org_id, _, plain = await insert_org_and_key(
        async_session,
        org_slug="flush",
        initial_credit_cents=10_000,
    )
    await add_phone_number(async_session, org_id, e164="+14155551234")

    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "system_prompt": "hi",
            "recipient_consent": True,
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201


# --------------------------------------------------------------------------- #
# Shared-key (HAIL_API_KEY) path
# --------------------------------------------------------------------------- #


async def test_master_key_resolves_to_self_hosted_sentinel(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    add_phone_number,
) -> None:
    """HAIL_API_KEY auth scopes calls to the ``self-hosted`` sentinel org id."""
    master = "test-master-secret"
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", master)
    await add_phone_number(async_session, SELF_HOSTED_ORG_ID, e164="+15555550000")

    resp = await client.post(
        "/calls",
        json={
            "to": "+15555550042",
            "system_prompt": "hi",
            "recipient_consent": True,
        },
        headers={"Authorization": f"Bearer {master}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    call_id = body["id"]
    row = (
        await async_session.execute(
            text("SELECT organization_id FROM calls WHERE id = :id"),
            {"id": call_id},
        )
    ).one()
    assert row[0] == SELF_HOSTED_ORG_ID


async def test_post_calls_succeeds_master_key_with_zero_balance(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    add_phone_number,
) -> None:
    """Shared-key (HAIL_API_KEY) requests skip the balance gate entirely.

    The ``self-hosted`` sentinel carries no credit row in production —
    billing is a cloud-only concern, driven by the website's private rater.
    Master-key auth lands on this sentinel and proceeds regardless of balance.
    """
    master = "test-master-secret-2"
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", master)
    await add_phone_number(async_session, SELF_HOSTED_ORG_ID, e164="+15555551111")

    resp = await client.post(
        "/calls",
        json={
            "to": "+15555552222",
            "system_prompt": "hi",
            "recipient_consent": True,
        },
        headers={"Authorization": f"Bearer {master}"},
    )
    assert resp.status_code == 201


# --------------------------------------------------------------------------- #
# CHECK constraints on the ledger itself
# --------------------------------------------------------------------------- #


async def test_account_credits_rejects_credit_with_negative_amount(
    async_session: AsyncSession,
) -> None:
    """A `credit` row with non-positive amount fails the DB CHECK."""
    org_id, _, _ = await insert_org_and_key(
        async_session, org_slug="bad-credit", initial_credit_cents=0
    )
    bogus = AccountCredit(
        organization_id=org_id,
        kind="credit",
        channel="credit",
        amount_cents=-100,
        source="test.bad",
    )
    async_session.add(bogus)
    with pytest.raises(IntegrityError):
        await async_session.commit()
    await async_session.rollback()


async def test_account_credits_rejects_debit_with_positive_amount(
    async_session: AsyncSession,
) -> None:
    """A `debit` row with positive amount fails the DB CHECK."""
    org_id, _, _ = await insert_org_and_key(
        async_session, org_slug="bad-debit", initial_credit_cents=0
    )
    bogus = AccountCredit(
        organization_id=org_id,
        kind="debit",
        channel="voice",
        amount_cents=100,  # debits must be negative
        source="test.bad",
    )
    async_session.add(bogus)
    with pytest.raises(IntegrityError):
        await async_session.commit()
    await async_session.rollback()


# --------------------------------------------------------------------------- #
# OSS fallback — auth backend's apikey table missing
# --------------------------------------------------------------------------- #


async def test_oss_missing_apikey_table_returns_401(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bearer that doesn't match HAIL_API_KEY 401s cleanly without the apikey table."""
    # Force the deps cache into the "no apikey table" state to simulate an OSS
    # deploy that never ran the website-owned migrations.
    monkeypatch.setattr(deps_module._caches, "apikey_table_present", False)
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "the-master")

    resp = await client.post(
        "/calls",
        json=_DEFAULT_BODY,
        headers={"Authorization": "Bearer not-the-master"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Fractional cents (0.1¢-precision ledger)
# --------------------------------------------------------------------------- #


async def test_account_credits_accepts_fractional_debit(
    async_session: AsyncSession,
) -> None:
    """A 0.2¢ email debit must round-trip exactly (NUMERIC(14,1) column)."""
    org_id, _, _ = await insert_org_and_key(
        async_session,
        org_slug="fractional",
        initial_credit_cents=500,
    )
    async_session.add(
        AccountCredit(
            organization_id=org_id,
            kind="debit",
            channel="email",
            amount_cents=Decimal("-0.2"),
            qty=1,
            ref="usage_event:test-fractional-1",
            source="usage_event",
        )
    )
    await async_session.commit()

    row = (
        await async_session.execute(
            text(
                "SELECT amount_cents FROM account_credits "
                "WHERE ref = 'usage_event:test-fractional-1'"
            )
        )
    ).scalar_one()
    assert Decimal(row) == Decimal("-0.2")


async def test_get_balance_cents_truncates_fractional_sum(
    async_session: AsyncSession,
) -> None:
    """500¢ credit − 0.2¢ debit = 499.8¢ → int 499 (conservative gate)."""
    from hailhq.core.billing import get_balance_cents

    org_id, _, _ = await insert_org_and_key(
        async_session,
        org_slug="fractional-sum",
        initial_credit_cents=500,
    )
    async_session.add(
        AccountCredit(
            organization_id=org_id,
            kind="debit",
            channel="email",
            amount_cents=Decimal("-0.2"),
            qty=1,
            ref="usage_event:test-fractional-2",
            source="usage_event",
        )
    )
    await async_session.commit()
    assert await get_balance_cents(async_session, org_id) == 499
