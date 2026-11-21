"""The daily database size alert."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hailhq.core import db_size_alert
from hailhq.core.db_size_alert import ALERT_FLAG, check_database_size
from hailhq.core.models import PlatformFlag


async def test_alert_once_a_day(async_session, monkeypatch) -> None:
    monkeypatch.setattr(db_size_alert, "ALERT_BYTES", 0)
    sent: list[tuple[str, str]] = []

    async def send(subject: str, body: str) -> None:
        sent.append((subject, body))

    day = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)
    assert await check_database_size(async_session, send, now=day)
    assert not await check_database_size(async_session, send, now=day)
    assert await check_database_size(async_session, send, now=day + timedelta(days=1))
    assert len(sent) == 2 and "MB" in sent[0][0]
    flag = await async_session.get(PlatformFlag, ALERT_FLAG)
    assert flag.value == "2026-11-22"


async def test_failed_send_is_tried_again(async_session, monkeypatch) -> None:
    monkeypatch.setattr(db_size_alert, "ALERT_BYTES", 0)

    async def broken(subject: str, body: str) -> None:
        raise ConnectionError("mail down")

    day = datetime(2026, 11, 21, tzinfo=timezone.utc)
    assert not await check_database_size(async_session, broken, now=day)
    assert await async_session.get(PlatformFlag, ALERT_FLAG) is None

    async def works(subject: str, body: str) -> None:
        return None

    assert await check_database_size(async_session, works, now=day)


@pytest.mark.parametrize("send", [None])
async def test_small_database_or_no_sender_sends_nothing(async_session, send):
    assert not await check_database_size(async_session, send)

    async def never(subject: str, body: str) -> None:
        raise AssertionError("must not send")

    # The test database is far below the real line.
    assert not await check_database_size(async_session, never)
