from types import SimpleNamespace

from hailhq.core.browser_voice import session_charge_cents, trial_remaining_ms


def call(*, used=None, seconds=60):
    return SimpleNamespace(
        metadata_={
            "browser_trial": True,
            **({"browser_used_ms": used} if used is not None else {}),
        },
        max_duration_seconds=seconds,
    )


def test_trial_reservation_is_shared_and_does_not_reset_on_reconnect():
    assert trial_remaining_ms([]) == 60_000
    assert trial_remaining_ms([call()]) == 0
    assert trial_remaining_ms([call(used=24_001)]) == 35_999
    assert trial_remaining_ms([call(used=24_001), call(seconds=35)]) == 999
    assert trial_remaining_ms([call(used=90_000)]) == 0


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
    assert budget["voice"] == {"used": 20_000, "limit": 60_000}
    assert budget["sms"] == {"used": 1, "limit": 3}
    assert budget["email"] == {"used": 2, "limit": 3}


def test_browser_budget_does_not_count_setup_time_or_other_paid_sessions():
    from hailhq.core.browser_voice import browser_budget

    setup = call()
    setup.status = "requested"
    setup.answered_at = None
    paid = call(used=20_000)
    paid.metadata_["browser_trial"] = False
    assert browser_budget([setup, paid])["voice"] == {"used": 0, "limit": 60_000}


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
