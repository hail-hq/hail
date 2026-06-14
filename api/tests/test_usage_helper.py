"""write_usage_event appends a usage_events row and pings the rater."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from hailhq.api.usage import write_usage_event
from hailhq.core.models import UsageEvent


@pytest.mark.asyncio
async def test_write_usage_event_inserts_row_and_notifies(async_session):
    org_id = uuid.uuid4()
    with patch("hailhq.api.usage.notify_usage_event_recorded") as notify:
        await write_usage_event(
            organization_id=org_id,
            channel="email_inbound",
            units=1,
            ref="email_inbound:test-1",
        )
    rows = (
        (
            await async_session.execute(
                select(UsageEvent).where(UsageEvent.ref == "email_inbound:test-1")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].channel == "email_inbound"
    assert rows[0].units == 1
    notify.assert_called_once()
