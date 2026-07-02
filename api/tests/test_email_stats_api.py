"""GET /emails/stats — account-level deliverability aggregates."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import EmailEvent

from .conftest import insert_org_and_key
from .test_emails_api import _register_custom_verified, _send_email as _send

DAY1 = datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 6, 29, 11, 0, tzinfo=timezone.utc)


async def _seed(session: AsyncSession, org_id, email_id, kind, ts, payload=None):
    session.add(
        EmailEvent(
            email_id=email_id,
            organization_id=org_id,
            kind=kind,
            payload=payload or {},
            occurred_at=ts,
        )
    )


async def _send_email(client: httpx.AsyncClient, plain: str) -> str:
    """POST one outbound email; returns its id."""
    return (await _send(client, plain))["id"]


async def _sent_email_id(client: httpx.AsyncClient, plain: str) -> str:
    """Register a verified domain for this org, then send one email."""
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    return await _send_email(client, plain)


async def test_stats_totals_series_and_rates(
    client: httpx.AsyncClient, async_session: AsyncSession
) -> None:
    org_id, _, plain = await insert_org_and_key(async_session)
    await _register_custom_verified(
        client, {"Authorization": f"Bearer {plain}"}, domain="acme.com"
    )
    e1 = await _send_email(client, plain)
    e2 = await _send_email(client, plain)
    # Re-stamp the two synthetic sent events into the window under test.
    await async_session.execute(
        update(EmailEvent).where(EmailEvent.email_id == e1).values(occurred_at=DAY1)
    )
    await async_session.execute(
        update(EmailEvent).where(EmailEvent.email_id == e2).values(occurred_at=DAY2)
    )

    await _seed(async_session, org_id, e1, "delivered", DAY1)
    await _seed(async_session, org_id, e1, "opened", DAY1)
    await _seed(async_session, org_id, e1, "opened", DAY2)  # repeat open
    await _seed(async_session, org_id, e2, "bounced", DAY2, {"hard": True})
    await async_session.commit()

    r = await client.get(
        "/emails/stats",
        params={
            "from": "2026-06-28T00:00:00Z",
            "to": "2026-06-30T00:00:00Z",
            "bucket": "day",
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    t = body["totals"]
    assert t["sent"] == 2 and t["delivered"] == 1
    assert t["bounced"] == 1 and t["bounced_hard"] == 1
    assert t["opened"] == 2 and t["unique_opened"] == 1
    assert body["rates"]["delivery"] == 0.5
    assert body["rates"]["bounce"] == 0.5
    assert body["rates"]["open"] == 0.5
    assert len(body["series"]) == 2  # two zero-filled day buckets
    assert body["series"][0]["sent"] == 1
    assert "from" in body and "to" in body
    assert body["bucket"] == "day"


async def test_stats_non_utc_offset_bucket_alignment(
    client: httpx.AsyncClient, async_session: AsyncSession
) -> None:
    """A `from`/`to` expressed in a non-UTC offset must still align with the
    UTC buckets Postgres' date_trunc produces, or every row falls outside
    every `buckets.get(bucket_start)` lookup and totals silently zero out.
    """
    org_id, _, plain = await insert_org_and_key(async_session)
    await _register_custom_verified(
        client, {"Authorization": f"Bearer {plain}"}, domain="acme.com"
    )
    e1 = await _send_email(client, plain)
    await async_session.execute(
        update(EmailEvent).where(EmailEvent.email_id == e1).values(occurred_at=DAY1)
    )
    await async_session.commit()

    # 2026-06-28T02:00:00+02:00 == 2026-06-28T00:00:00Z
    r = await client.get(
        "/emails/stats",
        params={
            "from": "2026-06-28T02:00:00+02:00",
            "to": "2026-06-30T00:00:00Z",
            "bucket": "day",
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["sent"] == 1
    assert body["from"] == "2026-06-28T00:00:00Z"
    assert len(body["series"]) == 2
    assert body["series"][0]["bucket_start"] == "2026-06-28T00:00:00Z"
    assert body["series"][0]["sent"] == 1


async def test_stats_zero_sends_null_rates(
    client: httpx.AsyncClient, async_session: AsyncSession
) -> None:
    _, _, plain = await insert_org_and_key(async_session)
    r = await client.get("/emails/stats", headers={"Authorization": f"Bearer {plain}"})
    assert r.status_code == 200, r.text
    assert r.json()["rates"]["delivery"] is None


async def test_stats_bounds_validation(
    client: httpx.AsyncClient, async_session: AsyncSession
) -> None:
    _, _, plain = await insert_org_and_key(async_session)
    h = {"Authorization": f"Bearer {plain}"}
    # naive datetime (no offset) is rejected
    r = await client.get(
        "/emails/stats",
        params={"from": "2026-06-28T00:00:00", "to": "2026-06-30T00:00:00Z"},
        headers=h,
    )
    assert r.status_code == 422
    # from >= to
    r = await client.get(
        "/emails/stats",
        params={"from": "2026-06-30T00:00:00Z", "to": "2026-06-28T00:00:00Z"},
        headers=h,
    )
    assert r.status_code == 422
    # > 92 days
    r = await client.get(
        "/emails/stats",
        params={"from": "2026-01-01T00:00:00Z", "to": "2026-06-01T00:00:00Z"},
        headers=h,
    )
    assert r.status_code == 422
    # hour bucket on > 8 days
    r = await client.get(
        "/emails/stats",
        params={
            "from": "2026-06-01T00:00:00Z",
            "to": "2026-06-20T00:00:00Z",
            "bucket": "hour",
        },
        headers=h,
    )
    assert r.status_code == 422


async def test_stats_scoped_to_org(
    client: httpx.AsyncClient, async_session: AsyncSession
) -> None:
    _, _, plain_a = await insert_org_and_key(async_session)
    _, _, plain_b = await insert_org_and_key(async_session)
    await _sent_email_id(client, plain_a)
    await async_session.commit()
    r = await client.get(
        "/emails/stats", headers={"Authorization": f"Bearer {plain_b}"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["totals"]["sent"] == 0
