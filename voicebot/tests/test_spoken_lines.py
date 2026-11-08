"""Fixed lines the voicebot speaks in the call's language."""

from hailhq.core.languages import SUPPORTED_LANGUAGES
from hailhq.voicebot.agent import (
    SOFT_CAP_END_REASON,
    soft_cap_announce_and_hangup,
)
from hailhq.voicebot.spoken_lines import SPOKEN_LINES, spoken_lines

from .test_agent import CALL_ID, FakeBridgeSession, FakeJobContext


def test_every_supported_language_has_every_line():
    assert SPOKEN_LINES.keys() == SUPPORTED_LANGUAGES.keys()
    for lines in SPOKEN_LINES.values():
        assert lines.soft_cap
        assert lines.llm_failure
        assert "{" not in lines.soft_cap
        assert "{" not in lines.llm_failure


def test_unknown_language_is_english():
    for language in (None, "xx", {"bad": "shape"}):
        assert spoken_lines(language) == SPOKEN_LINES["en"]


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
