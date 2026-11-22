"""Replies are cut after two sentences; tool calls survive the cut."""

from types import SimpleNamespace

import pytest
from hailhq.voicebot.reply_limit import MAX_CHARS, ReplyLimiter, limit_reply

LONG = (
    "Sure, an electric car stores energy in a large battery. "
    "When you press the pedal, the motor turns the wheels. "
    "When you slow down, the motor charges the battery again. "
    "To refill, you plug it into a charger."
)
TWO = (
    "Sure, an electric car stores energy in a large battery. "
    "When you press the pedal, the motor turns the wheels."
)


def speak(text, size):
    limiter = ReplyLimiter()
    out = "".join(limiter.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + limiter.finish()


@pytest.mark.parametrize("size", [1, 3, 7, 1000])
def test_a_long_reply_is_cut_after_two_sentences_at_any_chunking(size):
    assert speak(LONG, size) == TWO


@pytest.mark.parametrize(
    "text",
    [
        "Yes.",
        "It costs 3.5 dollars. That is all.",
        "Sure. Okay. I booked the table for seven tonight, and they will text you.",
        "Visit www.example.com for details, e.g. the opening hours. Thanks!",
    ],
)
def test_short_replies_decimals_and_abbreviations_pass_whole(text):
    assert speak(text, 4) == text


def test_two_tiny_sentences_do_not_end_a_reply():
    text = "Sure. Okay. Here is the long part of the answer that you asked for. More."
    assert speak(text, 5) == text[: text.index(" More.")]


def test_a_run_on_reply_ends_at_its_next_sentence_end():
    first = "word " * (MAX_CHARS // 5) + "end."
    assert speak(first + " Second sentence here.", 9) == first


def test_full_stops_without_spaces_count():
    text = "\u3053\u3093\u306b\u3061\u306f\u3002" * 30
    spoken = speak(text, 4)
    assert 0 < len(spoken) < len(text) and spoken.endswith("\u3002")


async def test_tool_calls_after_the_cut_still_reach_the_session():
    def chunk(content=None, tool_calls=None):
        return SimpleNamespace(
            delta=SimpleNamespace(content=content, tool_calls=tool_calls or [])
        )

    async def stream():
        for piece in (LONG[:80], LONG[80:], " Goodbye for now."):
            yield chunk(piece)
        yield chunk(tool_calls=["end_call"])
        yield SimpleNamespace(delta=None, usage="tokens")

    seen = [c async for c in limit_reply(stream())]
    assert "".join(c.delta.content or "" for c in seen if c.delta) == TWO
    assert [c.delta.tool_calls for c in seen if c.delta and c.delta.tool_calls] == [
        ["end_call"]
    ]
    assert seen[-1].usage == "tokens"


async def test_plain_text_streams_are_limited_too():
    async def stream():
        for i in range(0, len(LONG), 11):
            yield LONG[i : i + 11]

    assert "".join([c async for c in limit_reply(stream())]) == TWO
