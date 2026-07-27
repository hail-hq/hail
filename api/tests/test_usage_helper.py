"""write_usage_event appends a usage_events row and pings the rater."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from hailhq.api.main import _meter_forward_send
from hailhq.api.usage import write_usage_event
from hailhq.core.models import UsageEvent
from sqlalchemy import select


@pytest.mark.asyncio
async def test_write_usage_event_inserts_row_and_notifies(async_session):
    org_id = uuid.uuid4()
    with patch("hailhq.api.usage.notify_usage_event_recorded") as notify:
        await write_usage_event(
            organization_id=org_id,
            channel="email",
            units=1,
            ref="email:test-1",
        )
    rows = (
        (
            await async_session.execute(
                select(UsageEvent).where(UsageEvent.ref == "email:test-1")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].channel == "email"
    assert rows[0].units == 1
    notify.assert_called_once()


@pytest.mark.asyncio
async def test_meter_forward_send_bills_one_email_unit():
    """A delivered forward bills like an outbound email: 1 unit on the `email`
    channel, with a forward-distinct ref so the ledger keeps it separate from
    the inbound `email:{id}` event for the same conversation."""
    org_id = uuid.uuid4()
    forward_id = uuid.uuid4()
    with patch("hailhq.api.main.write_usage_event", new=AsyncMock()) as meter:
        await _meter_forward_send(organization_id=org_id, forward_email_id=forward_id)
    meter.assert_awaited_once_with(
        organization_id=org_id,
        channel="email",
        units=1,
        ref=f"email-forward:{forward_id}",
    )
