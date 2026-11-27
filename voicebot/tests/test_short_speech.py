"""Short speech over the agent is dropped; insisting passes. No word lists."""

from types import SimpleNamespace

from hailhq.voicebot.short_speech import (
    INSIST_SECONDS,
    RECENT_SECONDS,
    VAD_SILENCE_SECONDS,
    SpeechLength,
    drop_short_speech,
)
from livekit.agents.stt import SpeechEventType


def spoke(speech, start, seconds, over_agent=True):
    """One stretch of speech; the end is reported after the VAD's silence wait."""
    speech.on_state("speaking", start, agent_speaking=over_agent)
    end = start + seconds + VAD_SILENCE_SECONDS
    speech.on_state("listening", end)
    return end


def test_a_short_sound_over_the_agent_is_dropped_also_when_its_text_is_late():
    speech = SpeechLength()
    end = spoke(speech, 10.0, 0.6)
    assert speech.verdict(end) == "drop"
    assert speech.verdict(end + 1.0) == "drop"  # the agent may be silent by now
    assert speech.verdict(end + RECENT_SECONDS + 0.1) == "pass"


def test_talking_on_or_repeating_a_word_is_insisting():
    talking = SpeechLength()
    talking.on_state("speaking", 0.0, agent_speaking=True)
    assert talking.verdict(INSIST_SECONDS - 0.1) == "drop"
    assert talking.verdict(INSIST_SECONDS) == "insist"

    repeating = SpeechLength()
    end = 0.0
    for _ in range(3):
        end = spoke(repeating, end + 0.2, 0.4)
    assert repeating.verdict(end) == "insist"


def test_speech_into_a_silence_is_never_dropped():
    speech = SpeechLength()
    assert speech.verdict(0.0) == "pass"  # nothing heard yet
    speech.on_state("speaking", 5.0, agent_speaking=False)
    assert speech.verdict(5.2) == "pass"
    end = 5.4 + VAD_SILENCE_SECONDS
    speech.on_state("listening", end)
    assert speech.verdict(end + 0.5) == "pass"  # a short "yes" in a pause


def event(kind, text=""):
    return SimpleNamespace(type=kind, text=text)


async def run(events, verdicts):
    verdicts = iter(verdicts)

    async def source():
        for e in events:
            yield e

    return [e async for e in drop_short_speech(source(), lambda: next(verdicts))]


async def test_dropped_first_words_are_passed_on_once_they_insist():
    start = event(SpeechEventType.START_OF_SPEECH)
    no = event(SpeechEventType.FINAL_TRANSCRIPT, "No.")
    interim = event(SpeechEventType.INTERIM_TRANSCRIPT, "That is")
    rest = event(SpeechEventType.FINAL_TRANSCRIPT, "That is the wrong day.")
    out = await run([start, no, interim, rest], ["drop", "drop", "insist"])
    assert out == [start, no, rest]


async def test_dropped_words_are_forgotten_when_the_next_turn_is_unrelated():
    okay = event(SpeechEventType.FINAL_TRANSCRIPT, "Okay.")
    later = event(SpeechEventType.FINAL_TRANSCRIPT, "What time do you open?")
    assert await run([okay, later], ["drop", "pass"]) == [later]
