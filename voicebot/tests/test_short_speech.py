"""Short speech over the agent is dropped; insisting passes. No word lists."""

from types import SimpleNamespace

from hailhq.voicebot.short_speech import (
    INSIST_SECONDS,
    RECENT_SECONDS,
    SpeechLength,
    drop_short_speech,
)
from livekit.agents.stt import SpeechEventType


def test_a_short_sound_is_not_insisting_and_a_long_one_is():
    speech = SpeechLength()
    assert not speech.insisting(0.0)
    speech.on_state("speaking", 10.0)
    assert not speech.insisting(10.0 + INSIST_SECONDS - 0.1)
    assert speech.insisting(10.0 + INSIST_SECONDS)  # still talking


def test_a_transcript_that_arrives_after_the_speech_still_counts_its_length():
    short, long = SpeechLength(), SpeechLength()
    short.on_state("speaking", 0.0)
    short.on_state("listening", 0.4)
    long.on_state("speaking", 0.0)
    long.on_state("listening", INSIST_SECONDS + 0.5)
    assert not short.insisting(0.6)
    assert long.insisting(INSIST_SECONDS + 0.7)
    assert not long.insisting(INSIST_SECONDS + 0.5 + RECENT_SECONDS + 0.1)


async def test_only_text_is_dropped_and_only_when_asked():
    start = SimpleNamespace(type=SpeechEventType.START_OF_SPEECH)
    interim = SimpleNamespace(type=SpeechEventType.INTERIM_TRANSCRIPT)
    final = SimpleNamespace(type=SpeechEventType.FINAL_TRANSCRIPT)
    usage = SimpleNamespace(type=SpeechEventType.RECOGNITION_USAGE)

    async def source():
        for event in (start, interim, final, usage):
            yield event

    assert [e async for e in drop_short_speech(source(), lambda: True)] == [
        start,
        usage,
    ]
    assert len([e async for e in drop_short_speech(source(), lambda: False)]) == 4
