"""The API's handover hold covers the voicebot's pickup check."""

from hailhq.core.call_allowance import HANDOVER_PICKUP_SECONDS
from hailhq.voicebot.agent import HANDOVER_SETTLE_SECONDS, HANDOVER_SILENCE_SECONDS


def test_pickup_check_fits_the_held_window():
    # Both lines run during the check, before the handover limit starts.
    assert HANDOVER_SILENCE_SECONDS + HANDOVER_SETTLE_SECONDS <= HANDOVER_PICKUP_SECONDS
