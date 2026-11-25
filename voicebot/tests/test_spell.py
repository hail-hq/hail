"""Long spelled numbers are read in groups."""

import pytest
from hailhq.voicebot.agent import SpeechSanitizingAgent
from hailhq.voicebot.spell import (
    digit_groups,
    group_spelled_digits,
    group_spelled_stream,
)


@pytest.mark.parametrize(
    "digits, groups",
    [
        ("1234", ["1234"]),
        ("12345", ["123", "45"]),
        ("123456", ["123", "456"]),
        ("1234567", ["123", "4567"]),
        ("12345678", ["123", "456", "78"]),
        ("2015550134", ["201", "555", "0134"]),
        ("44207946000", ["442", "079", "460", "00"]),
    ],
)
def test_groups_of_three_with_a_last_group_of_two_to_four(digits, groups):
    assert digit_groups(digits) == groups
    assert "".join(groups) == digits


def test_only_digit_runs_change():
    text = (
        "Call <spell>2015550134</spell>, code <spell>AB12CD</spell>, "
        "pin <spell>4821</spell>, or <spell>201 555 0134</spell>."
    )
    assert group_spelled_digits(text) == (
        "Call <spell>201</spell>, <spell>555</spell>, <spell>0134</spell>, "
        "code <spell>AB12CD</spell>, pin <spell>4821</spell>, "
        "or <spell>201 555 0134</spell>."
    )


async def _chunks(*parts):
    for part in parts:
        yield part


async def _collect(stream):
    return [chunk async for chunk in stream]


@pytest.mark.parametrize(
    "parts",
    [
        ["The number is <spell>2015550134</spell>."],
        ["The number is <sp", "ell>20155", "50134</spe", "ll>."],
        ["The number is <", "spell>2015550134<", "/spell>", "."],
    ],
)
async def test_tags_split_across_chunks_are_grouped(parts):
    out = await _collect(group_spelled_stream(_chunks(*parts)))
    assert "".join(out) == (
        "The number is <spell>201</spell>, <spell>555</spell>, " "<spell>0134</spell>."
    )


async def test_plain_text_is_not_held_back():
    out = await _collect(group_spelled_stream(_chunks("Sure, ", "one moment.")))
    assert out == ["Sure, ", "one moment."]


async def test_an_unclosed_tag_does_not_stall_speech():
    long_tail = "x" * 80
    out = await _collect(group_spelled_stream(_chunks("<spell>12", long_tail)))
    assert out == ["<spell>12" + long_tail]


async def test_the_agent_speaks_grouped_numbers(monkeypatch):
    seen = []

    def fake_tts_node(_agent, text, _settings):
        seen.append(text)

    from livekit.agents import Agent

    monkeypatch.setattr(Agent.default, "tts_node", fake_tts_node)
    agent = SpeechSanitizingAgent(instructions="test")
    await agent.tts_node(_chunks("Call <spell>2015550134</spell>."), None)
    out = await _collect(seen[-1])
    assert "".join(out) == (
        "Call <spell>201</spell>, <spell>555</spell>, <spell>0134</spell>."
    )
