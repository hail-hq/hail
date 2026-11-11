from types import SimpleNamespace

import pytest
from hailhq.core.browser_voice import session_charge_cents, trial_remaining_ms


def call(*, used=None, seconds=300):
    return SimpleNamespace(
        metadata_={
            "browser_trial": True,
            **({"browser_used_ms": used} if used is not None else {}),
        },
        max_duration_seconds=seconds,
    )


def test_trial_reservation_is_shared_and_does_not_reset_on_reconnect():
    assert trial_remaining_ms([]) == 300_000
    assert trial_remaining_ms([call()]) == 0
    assert trial_remaining_ms([call(used=24_001)]) == 275_999
    assert trial_remaining_ms([call(used=24_001), call(seconds=275)]) == 999
    assert trial_remaining_ms([call(used=900_000)]) == 0


def test_paid_price_is_clamped_to_reserved_budget_and_rounded_up_to_tenth_cent():
    assert str(session_charge_cents(1, "10", 60)) == "0.1"
    assert str(session_charge_cents(30_000, "10", 60)) == "5.0"
    assert str(session_charge_cents(90_000, "10", 60)) == "10.0"
    assert str(session_charge_cents(0, "10", 60)) == "0.0"


def test_browser_budget_tracks_elapsed_voice_and_each_message_channel():
    from datetime import datetime, timedelta, timezone

    from hailhq.core.browser_voice import browser_budget

    now = datetime.now(timezone.utc)
    prior = call(used=12_000)
    prior.status = "completed"
    prior.metadata_["browser_actions"] = [{"channel": "sms"}]
    live = call(seconds=48)
    live.status = "in_progress"
    live.answered_at = now - timedelta(seconds=8)
    live.metadata_["browser_actions"] = [{"channel": "email"}, {"channel": "email"}]
    budget = browser_budget([prior, live], now=now)
    assert budget["voice"] == {"used": 20_000, "limit": 300_000}
    assert budget["sms"] == {"used": 1, "limit": 3}
    assert budget["email"] == {"used": 2, "limit": 3}


def test_browser_budget_does_not_count_setup_time_or_other_paid_sessions():
    from hailhq.core.browser_voice import browser_budget

    setup = call()
    setup.status = "requested"
    setup.answered_at = None
    paid = call(used=20_000)
    paid.metadata_["browser_trial"] = False
    assert browser_budget([setup, paid])["voice"] == {"used": 0, "limit": 300_000}


async def test_paid_budget_needs_no_workspace_history_query():
    from hailhq.core.browser_voice import read_browser_budget

    class NoHistoryQuery:
        async def execute(self, _statement):
            raise AssertionError("Paid budget must use its current session only")

    paid = call(used=5000)
    paid.metadata_["browser_trial"] = False
    paid.metadata_["browser_actions"] = [{"channel": "email"}]
    paid.status = "completed"
    budget = await read_browser_budget(NoHistoryQuery(), None, paid)
    assert budget["voice"]["used"] == 5000
    assert budget["email"]["used"] == 1


def test_trial_has_five_minutes_and_keeps_time_already_used():
    assert trial_remaining_ms([]) == 300_000
    assert trial_remaining_ms([call(used=60_000)]) == 240_000


@pytest.mark.asyncio
async def test_browser_observability_links_workspace_call_and_agent_without_contacts(
    monkeypatch,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from hailhq.core import telemetry, telemetry_identity
    from hailhq.core.browser_voice import bind_browser_context
    from opentelemetry import trace

    bind = AsyncMock()
    set_identity = Mock()
    tags = Mock()
    span = Mock()
    monkeypatch.setattr(telemetry, "telemetry_enabled", lambda: True)
    monkeypatch.setattr(telemetry, "bind_organization", bind)
    monkeypatch.setattr(
        telemetry_identity, "get_identity", lambda: {"organization_id": "workspace"}
    )
    monkeypatch.setattr(telemetry_identity, "set_identity", set_identity)
    monkeypatch.setattr(telemetry_identity, "tag_context", tags)
    monkeypatch.setattr(trace, "get_current_span", lambda: span)
    call = SimpleNamespace(organization_id="workspace", id="call", agent_id="agent")
    db = object()
    await bind_browser_context(db, call, actor_kind="agent")
    bind.assert_awaited_once_with(db, "workspace")
    set_identity.assert_called_once_with(
        {"organization_id": "workspace", "actor_kind": "agent"}
    )
    tags.assert_called_once_with(
        channel="voice", direction="inbound", call_id="call", agent_id="agent"
    )
    span.set_attribute.assert_called_once_with("hail.transport", "browser")
