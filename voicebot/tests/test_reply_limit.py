"""A reply is spoken in short parts; the rest follows unless they speak."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from hailhq.voicebot import reply_limit
from hailhq.voicebot.reply_limit import (
    HARD_CHARS,
    HARD_SLACK,
    MAX_CHARS,
    ReplyContinuation,
    ReplyLimiter,
    limit_reply,
    split_reply,
)

from .test_recovery import Session

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


async def test_a_reply_that_calls_a_tool_is_spoken_whole():
    def chunk(content=None, tool_calls=None):
        return SimpleNamespace(
            delta=SimpleNamespace(content=content, tool_calls=tool_calls or [])
        )

    async def stream():
        for piece in (LONG[:80], LONG[80:]):
            yield chunk(piece)
        yield chunk(tool_calls=["end_call"])
        yield chunk(" Goodbye for now.")
        yield SimpleNamespace(delta=None, usage="tokens")

    kept = []
    seen = [c async for c in limit_reply(stream(), kept.append)]
    spoken = "".join(c.delta.content or "" for c in seen if c.delta)
    assert spoken == LONG + " Goodbye for now."
    assert [c.delta.tool_calls for c in seen if c.delta and c.delta.tool_calls] == [
        ["end_call"]
    ]
    assert seen[-1].usage == "tokens"
    assert kept == []  # nothing is continued after a tool call


async def test_plain_text_streams_are_limited_too():
    async def stream():
        for i in range(0, len(LONG), 11):
            yield LONG[i : i + 11]

    assert "".join([c async for c in limit_reply(stream())]) == TWO


REST = (
    "When you slow down, the motor charges the battery again. "
    "To refill, you plug it into a charger."
)


async def test_the_rest_of_a_cut_reply_is_handed_over():
    async def text():
        for i in range(0, len(LONG), 11):
            yield LONG[i : i + 11]

    kept = []
    assert "".join([c async for c in limit_reply(text(), kept.append)]) == TWO
    assert [k.strip() for k in kept] == [REST]


@pytest.mark.parametrize(
    "text",
    [
        (
            "Thanks for waiting. Your package was handed to the carrier and will "
            "reach St. Louis by Friday morning."
        ),
        "Danke. Ihr Termin ist am Dienstag, dem 24. November um 15 Uhr eingetragen.",
        "The office opens again tomorrow. Call us after 9 a.m. Eastern for the result.",
        "I spoke with the clinic today. Dr. Lee can see you on Monday or on Thursday.",
    ],
)
def test_abbreviations_and_dates_do_not_cut_a_sentence(text):
    assert split_reply(text) == (text, "")


def test_other_scripts_and_run_on_replies_are_cut_too():
    hindi = (
        "\u092f\u0939 \u090f\u0915 \u0932\u0902\u092c\u093e \u0935\u093e\u0915\u094d\u092f \u0939\u0948\u0964 "
        * 8
    )
    part, rest = split_reply(hindi)
    assert rest and part.endswith("\u0964")
    run_on = "and then the next item on the list " * 20
    part, rest = split_reply(run_on)
    assert rest and HARD_CHARS <= len(part) <= HARD_CHARS + HARD_SLACK
    unbroken = "x" * 1000
    part, rest = split_reply(unbroken)
    assert len(part) == HARD_CHARS + HARD_SLACK and rest


def test_split_reply_gives_the_next_part_and_what_is_left():
    assert split_reply(LONG) == (TWO, REST)
    assert split_reply("Yes.") == ("Yes.", "")


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(reply_limit, "PAUSE", 0.01)
    monkeypatch.setattr(reply_limit, "SETTLE", 0.01)


def continuing(can_continue=lambda: True):
    session = Session()
    session.user_state = "listening"
    session.say = Mock()
    return session, ReplyContinuation(session, can_continue=can_continue)


def said(role="assistant", interrupted=False):
    return {"item": SimpleNamespace(role=role, interrupted=interrupted)}


async def test_the_rest_is_spoken_after_a_pause_when_they_stay_quiet(quick):
    session, rest = continuing()
    rest.keep(REST + " " + REST)
    session.emit("conversation_item_added", **said())
    await asyncio.sleep(0.05)
    session.say.assert_called_once_with(REST)
    session.emit("conversation_item_added", **said())  # that part finished
    await asyncio.sleep(0.05)
    assert [c.args[0] for c in session.say.call_args_list] == [REST, REST]
    session.emit("conversation_item_added", **said())
    await asyncio.sleep(0.05)
    assert session.say.call_count == 2  # nothing is left


async def test_the_turn_being_answered_does_not_drop_the_rest(quick):
    # With early generation the SDK adds the user's turn after the reply text
    # exists, and before the first part is spoken.
    session, rest = continuing()
    rest.keep(REST)
    session.emit("conversation_item_added", **said(role="user"))
    session.emit("conversation_item_added", **said())
    await asyncio.sleep(0.05)
    session.say.assert_called_once_with(REST)


@pytest.mark.parametrize("how", ["turn", "words", "interrupted", "not allowed"])
async def test_the_rest_is_dropped_when_they_speak_or_the_call_moved_on(quick, how):
    session, rest = continuing(can_continue=lambda: how != "not allowed")
    rest.keep(REST)
    session.emit("conversation_item_added", **said(interrupted=how == "interrupted"))
    if how == "turn":
        session.emit("conversation_item_added", **said(role="user"))
    if how == "words":
        session.emit("user_input_transcribed", transcript="wait")
    await asyncio.sleep(0.05)
    session.say.assert_not_called()
    assert rest.rest == ""


async def test_a_sound_without_words_only_delays_the_rest(quick):
    session, rest = continuing()
    rest.keep(REST)
    session.user_state = "speaking"
    session.emit("conversation_item_added", **said())
    await asyncio.sleep(0.05)
    session.say.assert_not_called()
    session.user_state = "listening"
    await asyncio.sleep(0.2)
    session.say.assert_called_once_with(REST)


async def test_an_okay_during_a_part_does_not_drop_the_rest(quick):
    session, rest = continuing()
    rest.keep(REST)
    session.emit("user_input_transcribed", transcript="okay")  # while it spoke
    session.emit("conversation_item_added", **said())
    await asyncio.sleep(0.05)
    session.say.assert_called_once_with(REST)
