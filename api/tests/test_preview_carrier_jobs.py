"""A local preview must not rewrite shared carrier settings on startup."""

from unittest.mock import AsyncMock

import pytest
from hailhq.api import main
from hailhq.core.config import settings


@pytest.mark.asyncio
async def test_disabled_background_carrier_jobs_make_no_provider_changes(monkeypatch):
    monkeypatch.setattr(settings, "hail_background_carrier_jobs_enabled", False)
    reconcile = AsyncMock()
    release = AsyncMock()
    setup = AsyncMock()
    monkeypatch.setattr(main, "reconcile_pending_orders", reconcile)
    monkeypatch.setattr(main, "retry_unreleased_numbers", release)
    monkeypatch.setattr(main, "sync_sms_setup", setup)
    await main._order_reconciler_loop()
    await main._sms_setup_loop()
    reconcile.assert_not_awaited()
    release.assert_not_awaited()
    setup.assert_not_awaited()
