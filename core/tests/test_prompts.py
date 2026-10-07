from hailhq.core import prompts


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
        "Be kind.", "inbound", history="[2026-10-06 10:00] text from caller: hi"
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
    assert prompts.build_voice_instructions("Be kind.", "inbound", history=None) == (
        expected
    )
    for blank in ("", "  \n "):
        out = prompts.build_voice_instructions("x", None, history=blank)
        assert "Earlier with this caller" not in out
