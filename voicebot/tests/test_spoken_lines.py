"""Fixed lines the voicebot speaks in the call's language."""

from hailhq.core.languages import SUPPORTED_LANGUAGES
from hailhq.voicebot.agent import (
    BYO_LLM_FAILURE_ANNOUNCEMENT,
    SOFT_CAP_ANNOUNCEMENT,
    SOFT_CAP_END_REASON,
    handover_intro,
    soft_cap_announce_and_hangup,
)
from hailhq.voicebot.spoken_lines import SPOKEN_LINES, spoken_lines

from .test_agent import CALL_ID, FakeBridgeSession, FakeJobContext


def test_every_supported_language_has_every_line():
    assert SPOKEN_LINES.keys() == SUPPORTED_LANGUAGES.keys()
    for lines in SPOKEN_LINES.values():
        assert "{" not in lines.soft_cap
        assert "{" not in lines.llm_failure
        assert lines.handover_intro.count("{name}") == 1
        assert "{reason}" not in lines.handover_intro
        assert lines.handover_intro_reason.count("{name}") == 1
        assert lines.handover_intro_reason.count("{reason}") == 1


def test_unknown_language_is_english():
    for language in (None, "xx", {"bad": "shape"}):
        assert spoken_lines(language) == SPOKEN_LINES["en"]
    assert SOFT_CAP_ANNOUNCEMENT == SPOKEN_LINES["en"].soft_cap
    assert BYO_LLM_FAILURE_ANNOUNCEMENT == SPOKEN_LINES["en"].llm_failure


def test_handover_intro_in_call_language():
    assert handover_intro("Sam (2)", "", "sv") == (
        "Hej Sam, jag har en uppringare på linjen. Jag kopplar dig nu."
    )
    assert handover_intro("Sam", "a {name} order", "sv") == (
        "Hej Sam, jag har en uppringare på linjen. "
        "Angivet skäl: a {name} order. Jag kopplar dig nu."
    )


async def test_soft_cap_speaks_call_language():
    ctx = FakeJobContext()
    session = FakeBridgeSession()
    await soft_cap_announce_and_hangup(
        ctx,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        CALL_ID,
        delay_seconds=0,
        language="fr",
    )
    assert session.said == [SPOKEN_LINES["fr"].soft_cap]
    assert ctx.shutdown_calls == [SOFT_CAP_END_REASON]
