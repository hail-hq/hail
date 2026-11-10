from types import SimpleNamespace

from hailhq.core.browser_voice import trial_remaining_ms, session_charge_cents


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
