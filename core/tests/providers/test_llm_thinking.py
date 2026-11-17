"""The thinking-off value each Anthropic model takes."""

from __future__ import annotations

from hailhq.core.providers.llm import thinking
from hailhq.core.providers.llm.thinking import thinking_off


def test_thinking_off_per_model() -> None:
    assert thinking_off("claude-sonnet-5-5") == {"type": "between_tools"}
    assert thinking_off("eu.anthropic.claude-sonnet-5-5") == {"type": "between_tools"}
    assert thinking_off("eu.anthropic.claude-haiku-5-5") == {"type": "disabled"}
    assert thinking_off("global.anthropic.claude-haiku-5-5-v1:0") == {
        "type": "disabled"
    }
    assert thinking_off("claude-opus-5-5") is None
    # Anthropic API ids can carry a date.
    assert thinking_off("claude-sonnet-5-5-20261001") == {"type": "between_tools"}
    assert thinking_off("eu.anthropic.claude-sonnet-5-5-20261001-v1:0") == {
        "type": "between_tools"
    }


def test_thinking_off_unknown_model_warns_and_disables(caplog) -> None:
    """An unknown id never stops a call: thinking disabled, one warning."""
    thinking._warned_models.clear()
    with caplog.at_level("WARNING", logger="hailhq.core.providers.llm.thinking"):
        # Not a near match: "sonnet-5-5" inside a longer id does not count.
        assert thinking_off("eu.anthropic.claude-sonnet-5-5-mini") == {
            "type": "disabled"
        }
        assert thinking_off("claude-sonnet-4-6") == {"type": "disabled"}
        assert thinking_off("claude-sonnet-4-6") == {"type": "disabled"}
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 2
    assert "claude-sonnet-5-5-mini" in warnings[0]
    assert "claude-sonnet-4-6" in warnings[1]
