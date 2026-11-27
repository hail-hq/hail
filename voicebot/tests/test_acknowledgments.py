"""Filler said over the agent is dropped; real words always pass."""

from types import SimpleNamespace

import pytest
from hailhq.voicebot.acknowledgments import drop_acknowledgments, only_acknowledgments
from livekit.agents.stt import SpeechEventType


@pytest.mark.parametrize(
    "text", ["Okay.", "Mhmm.", "Hello? Hello?", "Yeah, okay.", "uh-huh", "I see."]
)
def test_filler_is_only_acknowledgments(text):
    assert only_acknowledgments(text)


@pytest.mark.parametrize(
    "text", ["Stop.", "No.", "Yes.", "Wait.", "Okay, wait.", "What?", "", "42"]
)
def test_real_words_are_not(text):
    assert not only_acknowledgments(text)


def event(text, kind=SpeechEventType.FINAL_TRANSCRIPT):
    return SimpleNamespace(type=kind, alternatives=[SimpleNamespace(text=text)])


async def passed(events, speaking):
    async def source():
        for e in events:
            yield e

    return [e async for e in drop_acknowledgments(source(), lambda: speaking)]


async def test_filler_over_the_agent_is_dropped_and_real_words_pass():
    start = SimpleNamespace(type=SpeechEventType.START_OF_SPEECH, alternatives=[])
    events = [
        start,
        event("Okay.", SpeechEventType.INTERIM_TRANSCRIPT),
        event("Okay."),
        event("Stop."),
        event("Okay, wait a second."),
    ]
    out = await passed(events, speaking=True)
    assert out == [start, events[3], events[4]]


async def test_nothing_is_dropped_while_the_agent_is_silent():
    events = [event("Okay."), event("Mhmm.")]
    assert await passed(events, speaking=False) == events
