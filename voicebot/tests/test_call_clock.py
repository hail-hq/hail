"""Each call's instructions carry the date and time when the call started."""

from __future__ import annotations

from datetime import datetime, timezone

from hailhq.voicebot.agent import _call_clock, build_instructions


def test_build_instructions_puts_the_clock_before_the_language_line() -> None:
    out = build_instructions("Help.", "inbound", language="fr", clock="# Date and time")
    assert out.index("# Date and time") < out.index("Speak in French")


def test_call_clock_uses_the_dialed_number_as_the_business_on_inbound() -> None:
    meta = {"from_e164": "+12125550100", "to_e164": "+442079460000"}
    years = {str(datetime.now(timezone.utc).year)}
    inbound = _call_clock(meta, inbound=True)
    years.add(str(datetime.now(timezone.utc).year))
    assert inbound is not None
    assert "phone number is: " in inbound and "Europe/London)." in inbound
    assert "For the caller: " in inbound and "America/New_York" in inbound
    outbound = _call_clock(
        {"from_e164": "+442079460000", "to_e164": "+12125550100"}, inbound=False
    )
    assert outbound is not None and "For the person you called: " in outbound
    assert "The phone number you called is +1 212-555-0100." in outbound
    # The clock reads the real time: the year is today's.
    assert any(year in inbound for year in years)


def test_call_clock_never_fails_the_call() -> None:
    assert _call_clock({}, inbound=True) is not None
    assert _call_clock({"from_e164": "anonymous", "to_e164": None}, inbound=True)


def test_a_pool_number_is_not_the_business_location() -> None:
    meta = {"from_e164": "+12125550100", "to_e164": "+442079460000", "from_pool": True}
    out = _call_clock(meta, inbound=False)
    assert out is not None
    assert "phone number is: unknown. Go by UTC" in out
