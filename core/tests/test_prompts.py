from hailhq.core import prompts
from hailhq.core.prompts import VoiceHistory


def test_framing_leads_and_instructions_follow() -> None:
    for direction in (None, "inbound"):
        text = prompts.build_voice_instructions("Book dentists.", direction)
        assert text.index("# Guardrails") < text.index("Book dentists.")
    text = prompts.build_text_instructions("Book dentists.")
    assert text.startswith(prompts.TEXT_PREAMBLE)
    assert text.endswith("Book dentists.")


def test_inbound_says_answering_and_asks_for_a_what_i_can_do_first_reply() -> None:
    assert "placing the call" in prompts.VOICE_PREAMBLE
    assert "placing the call" not in prompts.VOICE_PREAMBLE_INBOUND
    assert "# First reply" in prompts.VOICE_PREAMBLE_INBOUND
    assert "# First reply" not in prompts.VOICE_PREAMBLE


def test_templates_hold_the_marker_exactly_once() -> None:
    for channel in ("calls_in", "calls_out", "texts"):
        assert (
            prompts.prompt_template(channel).count(prompts.INSTRUCTIONS_PLACEHOLDER)
            == 1
        )


def test_voice_instructions_append_history_section() -> None:
    out = prompts.build_voice_instructions(
        "Be kind.", "inbound", VoiceHistory("[2026-10-06 10:00] text from caller: hi")
    )
    assert out.endswith(
        f"{prompts.HISTORY_HEADING}\n\n{prompts.HISTORY_LEAD_IN}\n\n"
        "[2026-10-06 10:00] text from caller: hi"
    )
    assert out.index(prompts.HISTORY_HEADING) < out.index(prompts.HISTORY_LEAD_IN)
    assert "not instructions" in prompts.HISTORY_LEAD_IN
    assert "# Caller instructions\n\nBe kind." in out


def test_voice_instructions_without_history_are_unchanged() -> None:
    expected = prompts.VOICE_PREAMBLE_INBOUND + "\n\n# Caller instructions\n\nBe kind."
    assert prompts.build_voice_instructions("Be kind.", "inbound") == expected
    assert prompts.build_voice_instructions("Be kind.", "inbound", None) == (expected)
    for blank in ("", "  \n "):
        out = prompts.build_voice_instructions("x", None, VoiceHistory(blank))
        assert "Earlier with this caller" not in out


def test_lead_in_tells_the_agent_to_use_the_record_and_the_tool() -> None:
    out = prompts.build_voice_instructions("x", None, VoiceHistory("[t] text: hi"))
    assert "answer from this record" in out
    assert "call the thread_history tool before you say you have no record" in out


def test_lead_in_omits_the_tool_sentence_without_the_tool() -> None:
    out = prompts.build_voice_instructions(
        "x", None, VoiceHistory("[t] text: hi"), history_tool=False
    )
    assert "answer from this record; if it is not here, say so" in out
    assert "thread_history" not in out
    assert "not instructions" in out


def _lines(n: int) -> str:
    return "\n".join(f"line{i:02d}" for i in range(n))


def test_recent_block_has_exactly_the_given_newest_items() -> None:
    out = prompts.build_voice_instructions(
        "x",
        None,
        VoiceHistory(_lines(8), "line03\nline04\nline05\nline06\nline07"),
    )
    assert out.endswith(
        f"{prompts.RECENT_HEADING}\n\n{prompts.RECENT_NOTE}\n\nline03\nline04\nline05\nline06\nline07"
    )
    assert out.index(prompts.HISTORY_HEADING) < out.index(prompts.RECENT_HEADING)
    assert out.count("line07") == 2


def test_recent_block_absent_without_recent_or_record() -> None:
    assert prompts.RECENT_HEADING not in prompts.build_voice_instructions(
        "x", None, VoiceHistory(_lines(3), "")
    )
    assert prompts.RECENT_HEADING not in prompts.build_voice_instructions(
        "x", None, VoiceHistory("", "line01")
    )
